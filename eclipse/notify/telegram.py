"""Telegram outbound notification client + meeting summary push (Phase 2).

Bot API is consumed via raw httpx (synchronous) — no additional library needed.
The base URL is ``https://api.telegram.org/bot<token>``.

Public contract used by the pipeline:
    notify_meeting(pm, me_aliases) -> None   # never raises
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from eclipse.log import get_logger
from eclipse.models import ProcessedMeeting
from eclipse.secrets import load_secrets

log = get_logger("telegram")

_API_BASE = "https://api.telegram.org/bot"
# File downloads use a different host path: /file/bot<token>/<file_path>.
_API_FILE_BASE = "https://api.telegram.org/file/bot"

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

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

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
    # File download (inbound audio)
    # ------------------------------------------------------------------

    def get_file_path(self, file_id: str) -> str:
        """Resolve a file_id to a downloadable file path (Telegram getFile).

        Raises ``httpx.HTTPStatusError`` when Telegram won't serve the file —
        notably a 400 ("file is too big") for anything over the 20 MB bot limit.
        """
        resp = self._http.post(f"{self._base}/getFile", json={"file_id": file_id})
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return str(data["result"]["file_path"])

    def download_file(self, file_path: str, dest: Path) -> None:
        """Stream a Telegram file to *dest* on disk."""
        url = f"{_API_FILE_BASE}{self._token}/{file_path}"
        with self._http.stream("GET", url, timeout=120.0) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)

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
            is_mine = bool(item.owner and item.owner.strip().lower() in lowered_aliases)
            marker = "👤 " if is_mine else "• "
            detail_str = f" — {_esc(item.detail)}" if item.detail else ""
            meta: list[str] = []
            if item.owner:
                meta.append(_esc(item.owner))
            if item.due:
                meta.append(f"due {_esc(item.due)}")
            meta_str = f" ({'; '.join(meta)})" if meta else ""
            # Key item bold, then a quick explanation, then owner/due.
            lines.append(f"{marker}<b>{_esc(item.task)}</b>{detail_str}{meta_str}")

    # Safety-net callout: what the second pass surfaced that the first read missed.
    # Only shown when enrichment actually ran (a fallback note has nothing to add).
    if pm.enriched:
        lines.append("")
        if ins.missed_items:
            lines.append("🔍 <b>You may have missed:</b>")
            for m in ins.missed_items:
                lines.append(f"• <i>{_esc(m)}</i>")
        else:
            lines.append("🔍 <i>Nothing extra flagged on review.</i>")

    return "\n".join(lines)


def notify_meeting(pm: ProcessedMeeting, me_aliases: list[str]) -> None:
    """Push a one-meeting summary + action items to Telegram.

    Degrades silently if credentials are not configured — never raises.
    """
    s = load_secrets()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        log.warning("telegram_skipped", reason="bot token or chat_id not configured")
        return

    text = _build_message(pm, me_aliases)
    with TelegramClient(s.telegram_bot_token, s.telegram_chat_id) as client:
        try:
            client.send_message(text)
        except Exception as exc:
            log.warning("telegram_send_failed", error=str(exc))


# ------------------------------------------------------------------
# Inbound: pull audio messages into the inbox (phone -> PC transfer)
# ------------------------------------------------------------------

# Audio/video container extensions Eclipse accepts when shared as a document.
_AUDIO_EXTS = {
    ".m4a",
    ".mp3",
    ".wav",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".flac",
    ".mp4",
    ".m4b",
    ".amr",
    ".3gp",
    ".webm",
    ".mkv",
}
# A YYYY-MM-DD-ish date already present in a filename (so we don't double-date).
_DATE_IN_NAME_RE = re.compile(r"20\d{2}[-_]?\d{2}[-_]?\d{2}")


@dataclass
class TelegramAudio:
    """An audio attachment found in a Telegram update."""

    file_id: str
    file_name: str
    message_id: int


@dataclass
class PullResult:
    """Outcome of a single ``pull_audio`` run."""

    saved: list[Path]
    skipped_too_big: list[str]


def _dated_name(name: str, msg_date: date) -> str:
    """Prefix *name* with the message date unless it already carries one.

    Lets the pipeline date the meeting from when it was sent rather than the
    download time, while preserving any explicit date in the original filename.
    """
    if _DATE_IN_NAME_RE.search(name):
        return name
    return f"{msg_date.isoformat()}-{name}"


def extract_audio(update: dict[str, Any]) -> TelegramAudio | None:
    """Return the audio attachment in *update*, or None if it carries none.

    Handles voice notes, audio files, audio/video documents, and videos.
    """
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return None

    message_id = int(msg.get("message_id", 0))
    msg_date = datetime.fromtimestamp(msg.get("date", 0)).date()

    audio = msg.get("audio")
    if audio:
        name = audio.get("file_name") or f"telegram-audio-{message_id}.m4a"
        return TelegramAudio(audio["file_id"], _dated_name(name, msg_date), message_id)

    voice = msg.get("voice")
    if voice:
        name = f"telegram-voice-{message_id}.ogg"
        return TelegramAudio(voice["file_id"], _dated_name(name, msg_date), message_id)

    doc = msg.get("document")
    if doc:
        name = doc.get("file_name", "")
        mime = doc.get("mime_type", "")
        if mime.startswith(("audio/", "video/")) or Path(name).suffix.lower() in _AUDIO_EXTS:
            name = name or f"telegram-doc-{message_id}.bin"
            return TelegramAudio(doc["file_id"], _dated_name(name, msg_date), message_id)

    video = msg.get("video")
    if video:
        name = video.get("file_name") or f"telegram-video-{message_id}.mp4"
        return TelegramAudio(video["file_id"], _dated_name(name, msg_date), message_id)

    return None


def _read_offset(state_path: Path) -> int | None:
    try:
        return int(state_path.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_offset(state_path: Path, offset: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(str(offset), encoding="utf-8")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while (cand := parent / f"{stem}-{n}{suffix}").exists():
        n += 1
    return cand


def pull_audio(client: TelegramClient, inbox: Path, state_path: Path) -> PullResult:
    """Download new audio messages from Telegram into *inbox*.

    Tracks the last consumed ``update_id`` in *state_path* so each message is
    pulled exactly once. Files Telegram refuses to serve (the bot API caps
    downloads at 20 MB) are reported in ``skipped_too_big`` rather than raising.
    A download that fails mid-stream leaves the offset un-advanced so the message
    is retried on the next run.
    """
    offset = _read_offset(state_path)
    updates = client.get_updates(offset=offset, timeout=0)

    saved: list[Path] = []
    skipped_too_big: list[str] = []
    last_offset: int | None = offset

    for update in updates:
        update_id = int(update["update_id"])
        audio = extract_audio(update)
        if audio is not None:
            try:
                file_path = client.get_file_path(audio.file_id)
            except httpx.HTTPStatusError as exc:
                # Retrying won't help (file stays too big), so treat as consumed.
                log.warning("telegram_file_unavailable", name=audio.file_name, error=str(exc))
                skipped_too_big.append(audio.file_name)
            else:
                inbox.mkdir(parents=True, exist_ok=True)
                # strip any path components a malicious filename could smuggle in
                dest = _unique_path(inbox / Path(audio.file_name).name)
                try:
                    client.download_file(file_path, dest)
                except Exception as exc:
                    dest.unlink(missing_ok=True)  # drop any partial file
                    log.warning("telegram_download_failed", name=audio.file_name, error=str(exc))
                    break  # leave offset un-advanced; retry this message next run
                log.info("telegram_audio_pulled", name=dest.name)
                saved.append(dest)

        last_offset = update_id + 1  # this update is fully handled

    if last_offset is not None and last_offset != offset:
        _write_offset(state_path, last_offset)

    return PullResult(saved=saved, skipped_too_big=skipped_too_big)
