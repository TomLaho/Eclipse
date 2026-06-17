"""End-to-end processing of a single audio file."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from eclipse.config import Config
from eclipse.enrich.dates import resolve_action_dates
from eclipse.enrich.llm import OllamaEnricher
from eclipse.ingest.registry import Registry, hash_file
from eclipse.log import get_logger
from eclipse.models import ProcessedMeeting, TranscriptResult
from eclipse.transcribe.whisper import Transcriber
from eclipse.vault.writer import slugify, write_note

log = get_logger("pipeline")

_DATE_RE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


def meeting_date_for(path: Path) -> date:
    """Prefer a YYYY-MM-DD style date in the filename; fall back to file mtime."""
    m = _DATE_RE.search(path.name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while (cand := parent / f"{stem}-{n}{suffix}").exists():
        n += 1
    return cand


@dataclass
class PipelineResult:
    status: Literal["written", "skipped", "error"]
    source: str
    note_path: Path | None = None
    error: str | None = None


@dataclass
class _Transcribed:
    """A file that has been transcribed and is waiting for the enrich stage."""

    path: Path
    file_hash: str
    mtg_date: date
    transcript: TranscriptResult


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        transcriber: Transcriber,
        enricher: OllamaEnricher,
        registry: Registry,
    ) -> None:
        self.cfg = cfg
        self.transcriber = transcriber
        self.enricher = enricher
        self.registry = registry

    def process_file(self, path: Path) -> PipelineResult:
        """Transcribe and enrich a single file (peak RAM = whisper + llm)."""
        outcome = self._transcribe_stage(path)
        if isinstance(outcome, PipelineResult):
            return outcome
        return self._enrich_and_write(outcome)

    def process_batch(self, paths: list[Path]) -> list[PipelineResult]:
        """Two-stage batch: transcribe all → release Whisper → enrich all.

        On a low-RAM box this keeps peak memory at ``max(whisper, llm)`` instead
        of their sum, which is what lets a large Whisper model and a 7-8B insight
        model both fit (sequentially) in 7.8 GB.
        """
        results: list[PipelineResult] = []
        transcribed: list[_Transcribed] = []
        for path in paths:
            outcome = self._transcribe_stage(path)
            if isinstance(outcome, PipelineResult):
                results.append(outcome)
            else:
                transcribed.append(outcome)

        self.transcriber.unload()  # free Whisper before any LLM work

        results.extend(self._enrich_and_write(t) for t in transcribed)
        return results

    def _transcribe_stage(self, path: Path) -> _Transcribed | PipelineResult:
        """Stage 1: hash, dedupe, transcribe. Returns a result on skip/error."""
        try:
            file_hash = hash_file(path)
        except OSError as exc:
            return PipelineResult("error", path.name, error=str(exc))

        if self.registry.is_processed(file_hash):
            log.info("skip_duplicate", file=path.name)
            self._discard_from_inbox(path)
            return PipelineResult("skipped", path.name)

        try:
            mtg_date = meeting_date_for(path)
            transcript = self.transcriber.transcribe(path)
            self._diarize(path, transcript)
            return _Transcribed(path, file_hash, mtg_date, transcript)
        except Exception as exc:
            log.error("processing_failed", file=path.name, error=str(exc))
            self._quarantine(path)
            self.registry.record(file_hash, path.name, None, status="error")
            return PipelineResult("error", path.name, error=str(exc))

    def _enrich_and_write(self, t: _Transcribed) -> PipelineResult:
        """Stage 2: enrich the transcript, write the note, apply retention."""
        try:
            insights, enriched = self.enricher.enrich(
                t.transcript.text, t.mtg_date, t.path.name
            )
            resolve_action_dates(insights, t.mtg_date)

            audio_relpath = self._plan_audio_relpath(
                insights.client, insights.title, t.mtg_date, t.path
            )
            pm = ProcessedMeeting(
                source_name=t.path.name,
                file_hash=t.file_hash,
                meeting_date=t.mtg_date,
                transcript=t.transcript,
                insights=insights,
                audio_relpath=audio_relpath,
                transcribed_with=self.transcriber.descriptor(),
                enriched_with=self.enricher.descriptor() if enriched else "(fallback)",
                enriched=enriched,
            )

            note_path = write_note(self.cfg.vault_dir, pm)
            self._apply_retention(t.path, insights.client, insights.title, t.mtg_date)
            self.registry.record(t.file_hash, t.path.name, str(note_path), status="complete")
            log.info("written", file=t.path.name, note=note_path.name, enriched=enriched)
            self._notify(pm)
            return PipelineResult("written", t.path.name, note_path=note_path)

        except Exception as exc:
            log.error("processing_failed", file=t.path.name, error=str(exc))
            self._quarantine(t.path)
            self.registry.record(t.file_hash, t.path.name, None, status="error")
            return PipelineResult("error", t.path.name, error=str(exc))

    # --- optional stages (diarization, notifications) ---

    def _diarize(self, path: Path, transcript: TranscriptResult) -> None:
        """Label transcript segments with speakers, if diarization is enabled."""
        if not self.cfg.diarize:
            return
        from eclipse.transcribe.diarize import diarize_into

        diarize_into(path, transcript, self.cfg.diarize_model)

    def _notify(self, pm: ProcessedMeeting) -> None:
        """Push a meeting summary to Telegram, if enabled. Never fatal."""
        if not (self.cfg.telegram_enabled and self.cfg.telegram_on_process):
            return
        try:
            from eclipse.notify.telegram import notify_meeting

            notify_meeting(pm, self.cfg.me_aliases)
        except Exception as exc:  # notification must never break the pipeline
            log.warning("telegram_notify_failed", file=pm.source_name, error=str(exc))

    # --- audio retention helpers ---

    def _target_name(self, client: str, title: str, mtg_date: date, ext: str) -> str:
        return f"{mtg_date.isoformat()}-{slugify(title)}{ext}"

    def _plan_audio_relpath(
        self, client: str, title: str, mtg_date: date, path: Path
    ) -> str | None:
        if self.cfg.audio_retention != "keep":
            return None
        name = self._target_name(client, title, mtg_date, path.suffix.lower())
        target = _unique(self.cfg.audio_dir / name)
        try:
            return str(target.relative_to(self.cfg.vault_dir))
        except ValueError:
            return str(target)

    def _apply_retention(self, path: Path, client: str, title: str, mtg_date: date) -> None:
        policy = self.cfg.audio_retention
        if policy == "delete":
            path.unlink(missing_ok=True)
            return
        dest_dir = self.cfg.audio_dir if policy == "keep" else self.cfg.archive_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = self._target_name(client, title, mtg_date, path.suffix.lower())
        shutil.move(str(path), str(_unique(dest_dir / name)))

    def _discard_from_inbox(self, path: Path) -> None:
        """A duplicate re-appeared in the inbox; remove it so it doesn't linger."""
        try:
            if path.is_relative_to(self.cfg.inbox_dir):
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def _quarantine(self, path: Path) -> None:
        failed = self.cfg.archive_dir / "_failed"
        failed.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(_unique(failed / path.name)))
        except OSError as exc:
            log.warning("quarantine_failed", file=path.name, error=str(exc))
