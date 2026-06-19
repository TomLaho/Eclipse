from datetime import date

from eclipse.enrich.dates import resolve_action_dates, resolve_due
from eclipse.enrich.llm import _merge_unique
from eclipse.models import ActionItem, MeetingInsights


def test_resolve_due_relative_to_meeting() -> None:
    # meeting on a Wednesday; "Friday" should resolve to the next Friday (future)
    meeting = date(2026, 6, 17)  # Wednesday
    assert resolve_due("Friday", meeting) == "2026-06-19"


def test_resolve_due_handles_explicit_date() -> None:
    assert resolve_due("20 June 2026", date(2026, 6, 17)) == "2026-06-20"


def test_resolve_due_none_for_garbage() -> None:
    assert resolve_due("sometime-ish", date(2026, 6, 17)) is None
    assert resolve_due(None, date(2026, 6, 17)) is None


def test_resolve_action_dates_fills_due_iso() -> None:
    ins = MeetingInsights(
        title="t",
        summary="s",
        action_items=[ActionItem(task="send deck", due="tomorrow")],
    )
    resolve_action_dates(ins, date(2026, 6, 17))
    assert ins.action_items[0].due_iso == "2026-06-18"


def test_merge_unique_dedups_case_insensitively() -> None:
    base = MeetingInsights(
        title="t",
        summary="s",
        action_items=[ActionItem(task="Send deck")],
        decisions=["Use option A"],
    )
    extra = MeetingInsights(
        title="",
        summary="",
        action_items=[ActionItem(task="send DECK"), ActionItem(task="Book venue")],
        decisions=["Use option A", "Defer hiring"],
        follow_ups=["Confirm budget"],
    )
    added = _merge_unique(base, extra)
    assert [a.task for a in base.action_items] == ["Send deck", "Book venue"]
    assert base.decisions == ["Use option A", "Defer hiring"]
    assert base.follow_ups == ["Confirm budget"]
    # The returned delta is exactly what was newly surfaced (drives "may have missed").
    assert added == ["Book venue", "Defer hiring", "Confirm budget"]


def test_merge_unique_includes_owner_in_delta() -> None:
    base = MeetingInsights(title="t", summary="s")
    extra = MeetingInsights(
        title="", summary="", action_items=[ActionItem(task="Send report", owner="Tom")]
    )
    added = _merge_unique(base, extra)
    assert added == ["Send report (Tom)"]
