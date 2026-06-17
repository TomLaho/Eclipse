"""Telegram outbound notification client + meeting summary push (Phase 2).

Bot API is consumed via raw httpx (synchronous) — no additional library needed.
The base URL is ``https://api.telegram.org/bot<token>``.

Public contract used by the pipeline:
    notify_meeting(pm, me_aliases) -> None   # never raises
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from eclipse.log import get_logger
from eclipse.models import ProcessedMeeting
from eclipse.secrets import load_secrets

log = get_logger("telegram")

_API_BASE = "https://api.telegram.org/bot"

# Characters that must be escaped for Telegram HTML parse mode.
_HTML_ESCAPE: dict[str, str] = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_HTML_RE = re.compile("[&<>]")


def _esc(text: str) -> str:
    """Escape ``&``, ``<``, ``>`` for Telegram HTML parse mode."""
    return _HTML_RE.sub(lambda m: _HTML_ESCAPE[m.group()], text)


class TelegramClient:
    """Thin synchronous wrapper around the Telegram Bot API."""

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._base = f"{_API_BASE}{token}"
        self._http = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------
    # Core API methods
    # ------------------------------------------------------------------

    def send_message(
        self,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        """Send *text* (HTML parse mode) to the configured chat.

        Returns the message_id of the sent message.
        """
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = self._http.post(f"{self._base}/sendMessage", json=payload)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return int(data["result"]["message_id"])

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        """Long-poll for new updates (Telegram getUpdates).

        Returns a list of raw update dicts.
        """
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = self._http.get(
            f"{self._base}/getUpdates",
            params=params,
            timeout=timeout + 5.0,  # slightly longer than the long-poll timeout
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        result: list[dict[str, Any]] = data.get("result", [])
        return result

    def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        """Acknowledge an inline-button press so the loading spinner clears."""
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        resp = self._http.post(f"{self._base}/answerCallbackQuery", json=payload)
        resp.raise_for_status()

    def edit_message_text(
        self,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit an already-sent message (removes inline buttons when markup is None)."""
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        else:
            # Passing an empty reply_markup removes the inline keyboard.
            payload["reply_markup"] = {}
        resp = self._http.post(f"{self._base}/editMessageText", json=payload)
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_secrets(cls) -> TelegramClient | None:
        """Return a configured client, or None if secrets are missing."""
        s = load_secrets()
        if not s.telegram_bot_token or not s.telegram_chat_id:
            return None
        return cls(s.telegram_bot_token, s.telegram_chat_id)


# ------------------------------------------------------------------
# Pipeline-facing function
# ------------------------------------------------------------------


def _build_message(pm: ProcessedMeeting, me_aliases: list[str]) -> str:
    """Render a meeting summary as an HTML-formatted Telegram message."""
    ins = pm.insights
    lowered_aliases = {a.strip().lower() for a in me_aliases}

    lines: list[str] = [
        f"<b>{_esc(ins.title)}</b>",
        f"Client: {_esc(ins.client)}  |  Date: {pm.meeting_date.isoformat()}",
        "",
        _esc(ins.summary),
    ]

    if ins.action_items:
        lines.append("")
        lines.append("<b>Action items:</b>")
        for item in ins.action_items:
            owner_str = f" ({_esc(item.owner)})" if item.owner else ""
            due_str = f" — due {_esc(item.due)}" if item.due else ""
            is_mine = bool(item.owner and item.owner.strip().lower() in lowered_aliases)
            marker = "👤 " if is_mine else "• "
            lines.append(f"{marker}<i>{_esc(item.task)}</i>{owner_str}{due_str}")

    return "\n".join(lines)


def notify_meeting(pm: ProcessedMeeting, me_aliases: list[str]) -> None:
    """Push a one-meeting summary + action items to Telegram.

    Degrades silently if credentials are not configured — never raises.
    """
    s = load_secrets()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        log.warning("telegram_skipped", reason="bot token or chat_id not configured")
        return

    client = TelegramClient(s.telegram_bot_token, s.telegram_chat_id)
    text = _build_message(pm, me_aliases)
    try:
        client.send_message(text)
    except Exception as exc:
        log.warning("telegram_send_failed", error=str(exc))
