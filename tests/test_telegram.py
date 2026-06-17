"""Tests for eclipse.notify.telegram (Phase 2).

Network and real tokens are never required — all API calls are monkeypatched.
"""

from __future__ import annotations

from datetime import date

import pytest

from eclipse.models import ActionItem, MeetingInsights, ProcessedMeeting, TranscriptResult
from eclipse.notify.telegram import TelegramClient, _build_message, notify_meeting
from eclipse.secrets import Secrets


def _pm(
    title: str = "Q3 Kickoff",
    summary: str = "Discussed roadmap & budget.",
    client: str = "Acme",
    action_items: list[ActionItem] | None = None,
    meeting_date: date = date(2026, 6, 17),
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
        ),
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


def test_build_message_action_item_due_shown() -> None:
    items = [ActionItem(task="Update slides", owner="Tom", due="2026-06-20")]
    pm = _pm(action_items=items)
    text = _build_message(pm, me_aliases=["Tom"])
    assert "2026-06-20" in text


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
