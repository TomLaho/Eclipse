"""Deterministically resolve spoken due-dates to ISO dates.

Small local models are unreliable at date arithmetic ("Friday" -> which Friday?).
We let the LLM keep the spoken phrase in ``due`` and resolve it ourselves, relative
to the meeting date, so the result is reproducible and not a hallucinated guess.
"""

from __future__ import annotations

from datetime import date, datetime

from eclipse.log import get_logger
from eclipse.models import MeetingInsights

log = get_logger("dates")


def resolve_due(text: str | None, meeting_date: date) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` for a spoken date, or None if unparseable."""
    if not text or not text.strip():
        return None
    try:
        import dateparser
    except ImportError:  # dateparser is a declared dep; stay graceful if absent
        log.warning("dateparser_missing")
        return None

    base = datetime(meeting_date.year, meeting_date.month, meeting_date.day)
    parsed = dateparser.parse(
        text.strip(),
        settings={
            "RELATIVE_BASE": base,
            "PREFER_DATES_FROM": "future",  # "Friday" means the next one, not the last
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    return parsed.date().isoformat() if parsed is not None else None


def resolve_action_dates(insights: MeetingInsights, meeting_date: date) -> None:
    """Fill ``due_iso`` for every action item with a spoken ``due`` (in place)."""
    for item in insights.action_items:
        if item.due and not item.due_iso:
            item.due_iso = resolve_due(item.due, meeting_date)
