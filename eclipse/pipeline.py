"""End-to-end processing of a single audio file."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from eclipse.config import Config
from eclipse.enrich.llm import OllamaEnricher
from eclipse.ingest.registry import Registry, hash_file
from eclipse.log import get_logger
from eclipse.models import ProcessedMeeting
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
            insights, enriched = self.enricher.enrich(transcript.text, mtg_date, path.name)

            audio_relpath = self._plan_audio_relpath(
                insights.client, insights.title, mtg_date, path
            )
            pm = ProcessedMeeting(
                source_name=path.name,
                file_hash=file_hash,
                meeting_date=mtg_date,
                transcript=transcript,
                insights=insights,
                audio_relpath=audio_relpath,
                transcribed_with=self.transcriber.descriptor(),
                enriched_with=self.enricher.descriptor() if enriched else "(fallback)",
                enriched=enriched,
            )

            note_path = write_note(self.cfg.vault_dir, pm)
            self._apply_retention(path, insights.client, insights.title, mtg_date)
            self.registry.record(file_hash, path.name, str(note_path), status="complete")
            log.info("written", file=path.name, note=note_path.name, enriched=enriched)
            return PipelineResult("written", path.name, note_path=note_path)

        except Exception as exc:
            log.error("processing_failed", file=path.name, error=str(exc))
            self._quarantine(path)
            self.registry.record(file_hash, path.name, None, status="error")
            return PipelineResult("error", path.name, error=str(exc))

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
