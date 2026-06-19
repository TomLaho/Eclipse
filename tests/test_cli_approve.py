"""Tests for the approve() helpers extracted in Round 3 (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eclipse.cli import _send_approval_requests
from eclipse.notify.notion import eclipse_id
from eclipse.review import OpenAction


class _FakeTelegram:
    """Records send_message calls and hands out incrementing message ids."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self._next_id = 100

    def send_message(self, text: str, reply_markup: dict[str, Any] | None = None) -> int:
        self.sent.append((text, reply_markup))
        self._next_id += 1
        return self._next_id


def _action(task: str) -> OpenAction:
    return OpenAction(
        task=task,
        owner="Tom",
        due=None,
        client="Acme",
        meeting_title="Kickoff",
        meeting_date="2026-06-10",
        note_path=Path("vault/acme/2026-06-10-kickoff.md"),
    )


def test_send_approval_requests_maps_eid_to_message_id() -> None:
    tg = _FakeTelegram()
    actions = [_action("Send deck"), _action("Review budget")]

    pending, eid_to_mid = _send_approval_requests(tg, actions)  # type: ignore[arg-type]

    assert len(pending) == 2
    assert len(tg.sent) == 2
    # Every eid resolves back to a message id that is in pending (the invariant
    # the polling loop depends on).
    for action in actions:
        mid = eid_to_mid[eclipse_id(action)]
        assert pending[mid].task == action.task
    # The inline keyboard must carry approve:/skip: callbacks for the action.
    first_markup = tg.sent[0][1]
    assert first_markup is not None
    buttons = first_markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith("approve:")
    assert buttons[1]["callback_data"].startswith("skip:")
