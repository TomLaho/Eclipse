from datetime import date
from pathlib import Path

from eclipse.config import Config
from eclipse.ingest.registry import Registry
from eclipse.models import ActionItem, MeetingInsights, ProcessedMeeting, TranscriptResult
from eclipse.pipeline import Pipeline, meeting_date_for, reenrich_note
from eclipse.vault.writer import write_note


class FakeTranscriber:
    def transcribe(self, path: Path) -> TranscriptResult:
        return TranscriptResult(text="hello world from the meeting", duration_sec=90.0)

    def descriptor(self) -> str:
        return "fake-whisper"


class FakeEnricher:
    def __init__(self, *, raise_: bool = False, enriched: bool = True) -> None:
        self.raise_ = raise_
        self.enriched = enriched

    def enrich(
        self, text: str, meeting_date: date, source_name: str
    ) -> tuple[MeetingInsights, bool]:
        if self.raise_:
            raise RuntimeError("boom")
        ins = MeetingInsights(
            title="Mtg",
            summary="s",
            client="Acme",
            action_items=[ActionItem(task="Do thing", owner="Tom")],
        )
        return ins, self.enriched

    def descriptor(self) -> str:
        return "fake-llm"


def _cfg(tmp_path: Path, retention: str = "keep") -> Config:
    c = Config(
        inbox_dir=tmp_path / "inbox",
        vault_dir=tmp_path / "vault",
        archive_dir=tmp_path / "archive",
        registry_path=tmp_path / "data" / "r.sqlite",
        audio_retention=retention,  # type: ignore[arg-type]
        telegram_enabled=False,  # don't inherit the dev's eclipse.toml in tests
    )
    c.resolve_paths()
    c.ensure_dirs()
    return c


def _drop(cfg: Config, name: str = "2026-06-17-rec.wav", data: bytes = b"RIFFfakeaudio") -> Path:
    f = cfg.inbox_dir / name
    f.write_bytes(data)
    return f


def test_meeting_date_from_filename(tmp_path: Path) -> None:
    f = tmp_path / "2026-06-17-call.wav"
    f.write_bytes(b"x")
    assert meeting_date_for(f) == date(2026, 6, 17)


def test_meeting_date_falls_back_to_mtime(tmp_path: Path) -> None:
    f = tmp_path / "call.wav"
    f.write_bytes(b"x")
    assert isinstance(meeting_date_for(f), date)


def test_process_writes_note_and_keeps_audio(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    f = _drop(cfg)
    with Registry(cfg.registry_path) as reg:
        result = Pipeline(cfg, FakeTranscriber(), FakeEnricher(), reg).process_file(f)
    assert result.status == "written"
    assert result.note_path is not None and result.note_path.exists()
    assert not f.exists()  # moved out of the inbox
    assert len(list(cfg.audio_dir.glob("*.wav"))) == 1


def test_duplicate_is_skipped_and_discarded(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with Registry(cfg.registry_path) as reg:
        pipe = Pipeline(cfg, FakeTranscriber(), FakeEnricher(), reg)
        assert pipe.process_file(_drop(cfg)).status == "written"
        dup = _drop(cfg, name="copy.wav")  # identical bytes -> identical hash
        assert pipe.process_file(dup).status == "skipped"
        assert not dup.exists()


def test_delete_retention_removes_audio(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, retention="delete")
    f = _drop(cfg)
    with Registry(cfg.registry_path) as reg:
        Pipeline(cfg, FakeTranscriber(), FakeEnricher(), reg).process_file(f)
    assert list(cfg.audio_dir.glob("*")) == []
    assert not f.exists()


def test_failure_is_quarantined(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    f = _drop(cfg)
    with Registry(cfg.registry_path) as reg:
        result = Pipeline(cfg, FakeTranscriber(), FakeEnricher(raise_=True), reg).process_file(f)
    assert result.status == "error"
    assert not f.exists()
    assert len(list((cfg.archive_dir / "_failed").glob("*"))) == 1


def test_reenrich_note_rewrites_from_transcript(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # Start from an unenriched note that still carries its transcript.
    pm = ProcessedMeeting(
        source_name="rec.m4a",
        file_hash="hash123",
        meeting_date=date(2026, 6, 17),
        transcript=TranscriptResult(text="we agreed Tom will send the deck", duration_sec=120.0),
        insights=MeetingInsights(
            title="Raw", summary="raw snippet", client="General", tags=["unenriched"]
        ),
        audio_relpath="_audio/2026-06-17-raw.m4a",
        transcribed_with="faster-whisper/small.en",
        enriched_with="(fallback)",
        enriched=False,
    )
    old_path = write_note(cfg.vault_dir, pm)
    assert old_path.exists()

    new_path, pm_out = reenrich_note(cfg, FakeEnricher(), old_path)  # type: ignore[arg-type]

    assert pm_out.enriched is True
    assert new_path.exists()
    assert not old_path.exists()  # client/title changed -> old note removed
    text = new_path.read_text(encoding="utf-8")
    assert "Do thing" in text  # action item from enrichment
    assert "we agreed Tom will send the deck" in text  # transcript preserved
    assert "status: complete" in text


def test_reenrich_overwrites_in_place_when_path_unchanged(tmp_path: Path) -> None:
    """Re-enriching a note whose title/client don't change must overwrite it,
    not leave a stale original and a new "-2" copy."""
    cfg = _cfg(tmp_path)
    # Seed a note whose title/client already match the FakeEnricher output (Mtg/Acme).
    pm = ProcessedMeeting(
        source_name="rec.m4a",
        file_hash="hash123",
        meeting_date=date(2026, 6, 17),
        transcript=TranscriptResult(text="we agreed Tom will send the deck", duration_sec=120.0),
        insights=MeetingInsights(title="Mtg", summary="old", client="Acme"),
        enriched=False,
    )
    old_path = write_note(cfg.vault_dir, pm)
    new_path, _ = reenrich_note(cfg, FakeEnricher(), old_path)  # type: ignore[arg-type]

    assert new_path == old_path  # same path, overwritten in place
    assert old_path.exists()
    assert "old" not in old_path.read_text(encoding="utf-8")  # content was replaced
    # exactly one file in the dir: no orphaned "-2" copy and no leftover ".tmp"
    assert list(old_path.parent.iterdir()) == [old_path]
