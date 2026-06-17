"""Telegram outbound + approval bot (Phase 2).

Contract stub — the pipeline calls ``notify_meeting`` after a note is filed. The
full implementation (Bot API client, digest push, inline approve/edit/skip loop)
is built in the Phase-2/3 pass.
"""

from __future__ import annotations

from eclipse.models import ProcessedMeeting


def notify_meeting(pm: ProcessedMeeting, me_aliases: list[str]) -> None:
    """Push a one-meeting summary + action items to Telegram."""
    raise NotImplementedError
