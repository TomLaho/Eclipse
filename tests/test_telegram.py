"""Tests for eclipse.notify.telegram (Phase 2).

Network and real tokens are never required — all API calls are monkeypatched.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from eclipse.models import ActionItem, MeetingInsights, ProcessedMeeting, TranscriptResult
from eclipse.notify.telegram import (
    TelegramClient,
    _build_message,
    _dated_name,
    _read_offset,
    _write_offset,
    extract_audio,
    notify_meeting,
    pull_audio,
)
from eclipse.secrets import Secrets


def _pm(
    title: str = "Q3 Kickoff",
    summary: str = "Discussed roadmap & budget.",
    client: str = "Acme",
    action_items: list[ActionItem] | None = None,
    meeting_date: date = date(2026, 6, 17),
    missed_items: list[str] | None = None,
    enriched: bool = True,
) -> ProcessedMeeting:
    return ProcessedMeeting(
        source_name="test.m4a",
        file_hash="abc123",
        meeting_date=meeting_date,
        transcript=TranscriptResult(text="transcript text", duration_sec=60.0),
        insights=MeetingInsights(
            title=title,
            summary=summary,
            client=client,
            action_items=action_items or [],
            missed_items=missed_items or [],
        ),
        enriched=enriched,
    )


# ---------------------------------------------------------------------------
# _build_message tests
# ---------------------------------------------------------------------------


def test_build_message_contains_title_and_summary() -> None:
    pm = _pm()
    text = _build_message(pm, me_aliases=["Tom"])
    assert "Q3 Kickoff" in text
    assert "Discussed roadmap &amp; budget." in text  # & is escaped


def test_build_message_contains_client_and_date() -> None:
    pm = _pm()
    text = _build_message(pm, me_aliases=[])
    assert "Acme" in text
    assert "2026-06-17" in text


def test_build_message_html_escaping() -> None:
    pm = _pm(title="<Alert> & test", summary="a < b > c & d")
    text = _build_message(pm, me_aliases=[])
    assert "<Alert>" not in text
    assert "&lt;Alert&gt;" in text
    assert "&amp;" in text
    assert "&gt;" in text


def test_build_message_owner_marker_for_me() -> None:
    items = [ActionItem(task="Send deck", owner="Tom", due="Friday")]
    pm = _pm(action_items=items)
    text = _build_message(pm, me_aliases=["Tom"])
    # Items owned by me get the 👤 marker.
    assert "👤" in text
    assert "Send deck" in text


def test_build_message_owner_marker_not_for_others() -> None:
    items = [ActionItem(task="Review contract", owner="Jane")]
    pm = _pm(action_items=items)
    text = _build_message(pm, me_aliases=["Tom"])
    assert "👤" not in text
    assert "Review contract" in text


def test_build_message_alias_matching_is_case_insensitive() -> None:
    items = [ActionItem(task="File report", owner="tom")]
    pm = _pm(action_items=items)
    text = _build_message(pm, me_aliases=["Tom"])
    assert "👤" in text


def test_build_message_no_action_items_section_when_empty() -> None:
    pm = _pm(action_items=[])
    text = _build_message(pm, me_aliases=["Tom"])
    assert "Action items" not in text


def test_build_message_lists_missed_items() -> None:
    pm = _pm(missed_items=["Get Jake & Dan sign-off by Monday (Tom)"])
    text = _build_message(pm, me_aliases=["Tom"])
    assert "You may have missed" in text
    assert "Get Jake &amp; Dan sign-off by Monday (Tom)" in text  # & escaped


def test_build_message_reassures_when_enriched_but_nothing_missed() -> None:
    pm = _pm(missed_items=[])  # enriched True by default
    text = _build_message(pm, me_aliases=["Tom"])
    assert "Nothing extra flagged" in text


def test_build_message_no_missed_section_when_not_enriched() -> None:
    pm = _pm(missed_items=[], enriched=False)
    text = _build_message(pm, me_aliases=["Tom"])
    assert "You may have missed" not in text
    assert "Nothing extra flagged" not in text


def test_build_message_action_item_due_shown() -> None:
    items = [ActionItem(task="Update slides", owner="Tom", due="2026-06-20")]
    pm = _pm(action_items=items)
    text = _build_message(pm, me_aliases=["Tom"])
    assert "2026-06-20" in text


def test_build_message_action_task_is_bold_with_detail() -> None:
    items = [
        ActionItem(
            task="Send the one-pager",
            owner="Tom",
            due="Friday",
            detail="needs comms sign-off first",
        )
    ]
    pm = _pm(action_items=items)
    text = _build_message(pm, me_aliases=["Tom"])
    assert "<b>Send the one-pager</b>" in text  # key item bolded
    assert "needs comms sign-off first" in text  # explanation shown
    assert "Tom" in text and "due Friday" in text  # owner/due as trailing meta


# ---------------------------------------------------------------------------
# from_secrets tests
# ---------------------------------------------------------------------------


def test_from_secrets_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eclipse.notify.telegram.load_secrets",
        lambda: Secrets(telegram_bot_token=None, telegram_chat_id=None),
    )
    client = TelegramClient.from_secrets()
    assert client is None


def test_from_secrets_returns_none_when_only_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eclipse.notify.telegram.load_secrets",
        lambda: Secrets(telegram_bot_token="tok", telegram_chat_id=None),
    )
    assert TelegramClient.from_secrets() is None


def test_from_secrets_returns_client_when_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eclipse.notify.telegram.load_secrets",
        lambda: Secrets(telegram_bot_token="tok123", telegram_chat_id="456"),
    )
    client = TelegramClient.from_secrets()
    assert isinstance(client, TelegramClient)


# ---------------------------------------------------------------------------
# notify_meeting tests (monkeypatched send_message)
# ---------------------------------------------------------------------------


def test_notify_meeting_calls_send_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_meeting should call send_message with HTML text."""
    sent: list[str] = []

    def fake_send(self: TelegramClient, text: str, **_kwargs: object) -> int:
        sent.append(text)
        return 1

    monkeypatch.setattr(
        "eclipse.notify.telegram.load_secrets",
        lambda: Secrets(telegram_bot_token="tok", telegram_chat_id="999"),
    )
    monkeypatch.setattr(TelegramClient, "send_message", fake_send)

    pm = _pm(
        action_items=[ActionItem(task="Send report", owner="Tom")],
    )
    notify_meeting(pm, me_aliases=["Tom"])

    assert len(sent) == 1
    assert "Q3 Kickoff" in sent[0]
    assert "👤" in sent[0]


def test_notify_meeting_silent_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_meeting must not raise when credentials are missing."""
    monkeypatch.setattr(
        "eclipse.notify.telegram.load_secrets",
        lambda: Secrets(telegram_bot_token=None, telegram_chat_id=None),
    )
    # Should complete without raising.
    notify_meeting(_pm(), me_aliases=["Tom"])


def test_notify_meeting_silent_on_send_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_meeting must not propagate exceptions from send_message."""

    def boom(self: TelegramClient, text: str, **_kwargs: object) -> int:
        raise RuntimeError("network error")

    monkeypatch.setattr(
        "eclipse.notify.telegram.load_secrets",
        lambda: Secrets(telegram_bot_token="tok", telegram_chat_id="999"),
    )
    monkeypatch.setattr(TelegramClient, "send_message", boom)

    # Must not raise.
    notify_meeting(_pm(), me_aliases=["Tom"])


# ---------------------------------------------------------------------------
# Inbound: extract_audio
# ---------------------------------------------------------------------------

# A fixed unix timestamp (2024-06-17) used to build update fixtures.
_TS = 1_718_600_000


def _msg(message_id: int = 1, **payload: object) -> dict[str, object]:
    return {"update_id": message_id, "message": {"message_id": message_id, "date": _TS, **payload}}


def test_extract_audio_handles_audio_file() -> None:
    a = extract_audio(_msg(audio={"file_id": "A1", "file_name": "standup.m4a"}))
    assert a is not None
    assert a.file_id == "A1"
    assert "standup.m4a" in a.file_name


def test_extract_audio_handles_voice_note() -> None:
    a = extract_audio(_msg(voice={"file_id": "V1"}))
    assert a is not None
    assert a.file_id == "V1"
    assert a.file_name.endswith(".ogg")


def test_extract_audio_document_by_mime() -> None:
    doc = {"file_id": "D1", "file_name": "rec", "mime_type": "audio/mpeg"}
    a = extract_audio(_msg(document=doc))
    assert a is not None
    assert a.file_id == "D1"


def test_extract_audio_document_by_extension() -> None:
    doc = {"file_id": "D2", "file_name": "rec.opus", "mime_type": "application/octet-stream"}
    a = extract_audio(_msg(document=doc))
    assert a is not None
    assert a.file_id == "D2"


def test_extract_audio_ignores_non_audio_document() -> None:
    doc = {"file_id": "D3", "file_name": "report.pdf", "mime_type": "application/pdf"}
    assert extract_audio(_msg(document=doc)) is None


def test_extract_audio_ignores_text_message() -> None:
    assert extract_audio(_msg(text="hello there")) is None


def test_dated_name_prefixes_when_no_date() -> None:
    assert _dated_name("standup.m4a", date(2026, 6, 18)) == "2026-06-18-standup.m4a"


def test_dated_name_left_alone_when_date_present() -> None:
    assert _dated_name("rec-2026-06-15.m4a", date(2026, 6, 18)) == "rec-2026-06-15.m4a"


# ---------------------------------------------------------------------------
# Offset persistence
# ---------------------------------------------------------------------------


def test_offset_roundtrip(tmp_path: Path) -> None:
    state = tmp_path / "data" / "telegram_offset"
    assert _read_offset(state) is None  # absent -> None
    _write_offset(state, 42)
    assert _read_offset(state) == 42


# ---------------------------------------------------------------------------
# pull_audio (fully monkeypatched client)
# ---------------------------------------------------------------------------


def test_pull_audio_downloads_and_advances_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TelegramClient("tok", "123")
    updates = [
        _msg(10, document={"file_id": "F1", "file_name": "meeting.m4a", "mime_type": "audio/mp4"}),
        _msg(11, text="not audio"),
    ]
    monkeypatch.setattr(client, "get_updates", lambda offset=None, timeout=30: updates)
    monkeypatch.setattr(client, "get_file_path", lambda fid: f"path/{fid}")

    def fake_dl(file_path: str, dest: Path) -> None:
        dest.write_bytes(b"audio-bytes")

    monkeypatch.setattr(client, "download_file", fake_dl)

    inbox = tmp_path / "inbox"
    state = tmp_path / "data" / "telegram_offset"
    result = pull_audio(client, inbox, state)

    assert len(result.saved) == 1
    assert result.saved[0].read_bytes() == b"audio-bytes"
    assert "meeting.m4a" in result.saved[0].name
    assert result.skipped_too_big == []
    assert _read_offset(state) == 12  # last update_id (11) + 1


def test_pull_audio_reports_too_big(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TelegramClient("tok", "123")
    updates = [_msg(5, audio={"file_id": "BIG", "file_name": "long.m4a"})]
    monkeypatch.setattr(client, "get_updates", lambda offset=None, timeout=30: updates)

    def too_big(file_id: str) -> str:
        request = httpx.Request("POST", "https://api.telegram.org/botX/getFile")
        response = httpx.Response(400, request=request, json={"description": "file is too big"})
        raise httpx.HTTPStatusError("file is too big", request=request, response=response)

    monkeypatch.setattr(client, "get_file_path", too_big)

    result = pull_audio(client, tmp_path / "inbox", tmp_path / "offset")

    assert result.saved == []
    assert len(result.skipped_too_big) == 1
    assert "long.m4a" in result.skipped_too_big[0]
    assert _read_offset(tmp_path / "offset") == 6  # consumed despite skip


def test_pull_audio_no_updates_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TelegramClient("tok", "123")
    monkeypatch.setattr(client, "get_updates", lambda offset=None, timeout=30: [])
    result = pull_audio(client, tmp_path / "inbox", tmp_path / "offset")
    assert result.saved == []
    assert result.skipped_too_big == []
    assert _read_offset(tmp_path / "offset") is None  # nothing consumed -> no write
