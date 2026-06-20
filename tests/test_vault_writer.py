from datetime import date
from pathlib import Path

from eclipse.models import ActionItem, MeetingInsights, ProcessedMeeting, TranscriptResult
from eclipse.vault.writer import render_markdown, slugify, write_note


def _pm() -> ProcessedMeeting:
    return ProcessedMeeting(
        source_name="rec.m4a",
        file_hash="abc123",
        meeting_date=date(2026, 6, 17),
        transcript=TranscriptResult(text="The full transcript text.", duration_sec=600),
        insights=MeetingInsights(
            title="Acme Pricing",
            summary="Discussed pricing and next steps.",
            client="Acme Corp",
            attendees=["Tom", "Jane"],
            tags=["pricing"],
            decisions=["Approved the 10% discount"],
            action_items=[ActionItem(task="Send proposal", owner="Tom", due="Friday")],
            follow_ups=["Confirm with legal"],
        ),
        audio_relpath="_audio/2026-06-17-acme-pricing.m4a",
        transcribed_with="faster-whisper/small.en",
        enriched_with="llama3.2:3b",
    )


def test_render_has_frontmatter_and_checkbox() -> None:
    md = render_markdown(_pm())
    assert md.startswith("---")
    assert "title: Acme Pricing" in md
    assert "client: Acme Corp" in md
    assert "- [ ] Send proposal" in md
    assert "**Tom**" in md
    assert "due: Friday" in md
    assert "## Transcript" in md


def test_multiline_summary_stays_inside_callout() -> None:
    pm = _pm()
    pm.insights.summary = "First sentence.\nSecond line.\n\nFourth after a blank."
    md = render_markdown(pm)
    # Every summary line (including the blank one) must be quoted, or Obsidian
    # drops the tail out of the callout block.
    assert "> First sentence." in md
    assert "> Second line." in md
    assert "> Fourth after a blank." in md
    assert "\nSecond line." not in md  # i.e. never an unquoted continuation


def test_slugify() -> None:
    assert slugify("Acme Corp!! Pricing") == "acme-corp-pricing"
    assert slugify("   ") == "untitled"


def test_write_note_paths_and_uniqueness(tmp_path: Path) -> None:
    first = write_note(tmp_path, _pm())
    assert first.exists()
    assert first.parent.name == "acme-corp"
    assert first.name.startswith("2026-06-17-acme-pricing")
    second = write_note(tmp_path, _pm())
    assert second != first
