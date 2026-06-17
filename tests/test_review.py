from datetime import date
from pathlib import Path

import pytest

from eclipse import review
from eclipse.config import Config
from eclipse.models import ActionItem, MeetingInsights, ProcessedMeeting, TranscriptResult
from eclipse.vault.writer import write_note


def _seed(vault: Path) -> None:
    pm = ProcessedMeeting(
        source_name="a.m4a",
        file_hash="h",
        meeting_date=date(2026, 6, 10),
        transcript=TranscriptResult(text="t", duration_sec=60),
        insights=MeetingInsights(
            title="Kickoff",
            summary="Kickoff summary line.",
            client="Acme",
            action_items=[
                ActionItem(task="Send deck", owner="Tom", due="Friday"),
                ActionItem(task="Review budget", owner="Jane"),
            ],
        ),
    )
    write_note(vault, pm)


def test_collect_open_actions_and_mine_filter(tmp_path: Path) -> None:
    _seed(tmp_path)
    actions = review.collect_open_actions(tmp_path)
    assert len(actions) == 2
    mine = review.collect_open_actions(tmp_path, ["Tom"], mine_only=True)
    assert len(mine) == 1
    assert mine[0].owner == "Tom"
    assert mine[0].due == "Friday"


def test_build_corpus_contains_meeting(tmp_path: Path) -> None:
    _seed(tmp_path)
    corpus = review.build_corpus(tmp_path)
    assert "Kickoff" in corpus
    assert "Acme" in corpus


def test_build_digest_is_deterministic_without_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(review.OllamaEnricher, "available", lambda self: False)
    cfg = Config(vault_dir=tmp_path, me_aliases=["Tom"])
    cfg.resolve_paths()
    md = review.build_digest(cfg)
    assert "2 open action items" in md
    assert "Send deck" in md
    assert "### Acme" in md


def test_completed_items_are_not_open(tmp_path: Path) -> None:
    _seed(tmp_path)
    note = next(iter(review.iter_notes(tmp_path))).path
    note.write_text(
        note.read_text(encoding="utf-8").replace("- [ ] Send deck", "- [x] Send deck"),
        encoding="utf-8",
    )
    actions = review.collect_open_actions(tmp_path)
    assert len(actions) == 1
    assert actions[0].task == "Review budget"
