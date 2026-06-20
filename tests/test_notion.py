"""Tests for eclipse.notify.notion (Phase 3).

Network and real tokens are never required — all Notion API calls are
monkeypatched at the Client method level.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eclipse.notify.notion import NotionTodos, eclipse_id
from eclipse.review import OpenAction
from eclipse.secrets import Secrets

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _action(
    task: str = "Send proposal",
    owner: str | None = "Tom",
    due: str | None = "2026-06-20",
    client: str = "Acme",
    note_path: Path | None = None,
) -> OpenAction:
    return OpenAction(
        task=task,
        owner=owner,
        due=due,
        client=client,
        meeting_title="Q3 Review",
        meeting_date="2026-06-17",
        note_path=note_path or Path("vault/acme/2026-06-17-q3-review.md"),
    )


# ---------------------------------------------------------------------------
# eclipse_id tests
# ---------------------------------------------------------------------------


def test_eclipse_id_is_16_hex_chars() -> None:
    action = _action()
    eid = eclipse_id(action)
    assert len(eid) == 16
    assert all(c in "0123456789abcdef" for c in eid)


def test_eclipse_id_is_deterministic() -> None:
    a1 = _action()
    a2 = _action()
    assert eclipse_id(a1) == eclipse_id(a2)


def test_eclipse_id_differs_for_different_task() -> None:
    a1 = _action(task="Send proposal")
    a2 = _action(task="Review contract")
    assert eclipse_id(a1) != eclipse_id(a2)


def test_eclipse_id_differs_for_different_note_path() -> None:
    a1 = _action(note_path=Path("vault/acme/2026-06-17-meeting-a.md"))
    a2 = _action(note_path=Path("vault/acme/2026-06-17-meeting-b.md"))
    assert eclipse_id(a1) != eclipse_id(a2)


def test_eclipse_id_uses_stem_not_full_path() -> None:
    """Two paths in different directories but with the same stem → same id."""
    a1 = _action(note_path=Path("vault/acme/2026-06-17-mtg.md"))
    a2 = _action(note_path=Path("other/2026-06-17-mtg.md"))
    assert eclipse_id(a1) == eclipse_id(a2)


# ---------------------------------------------------------------------------
# from_secrets tests
# ---------------------------------------------------------------------------


def test_from_secrets_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eclipse.notify.notion.load_secrets",
        lambda: Secrets(notion_access_token=None),
    )
    assert NotionTodos.from_secrets() is None


def test_from_secrets_returns_instance_when_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eclipse.notify.notion.load_secrets",
        lambda: Secrets(notion_access_token="secret_abc"),
    )
    # Client constructor makes no network calls; patching the import is enough.
    import notion_client

    monkeypatch.setattr(notion_client, "Client", lambda **_kw: MagicMock())
    result = NotionTodos.from_secrets()
    assert isinstance(result, NotionTodos)


# ---------------------------------------------------------------------------
# push_todo dedup tests
# ---------------------------------------------------------------------------


def _make_todos(existing_eids: list[str] | None = None) -> NotionTodos:
    """Build a NotionTodos with a mocked underlying Notion client."""
    mock_client = MagicMock()

    # Fake query result pages carrying EclipseId values.
    pages = []
    for eid in existing_eids or []:
        pages.append({"properties": {"EclipseId": {"rich_text": [{"text": {"content": eid}}]}}})

    # notion.py queries via client.request(...) (notion-client v3 dropped
    # databases.query), so the stub must target request, not databases.query.
    mock_client.request.return_value = {
        "results": pages,
        "has_more": False,
    }
    mock_client.pages.create.return_value = {"id": "new-page-id"}

    todos = NotionTodos(mock_client)
    return todos


def test_push_todo_creates_page_for_new_action() -> None:
    todos = _make_todos(existing_eids=[])
    action = _action()
    result = todos.push_todo("db-123", action)
    assert result is True
    todos._client.pages.create.assert_called_once()


def test_push_todo_skips_duplicate() -> None:
    action = _action()
    eid = eclipse_id(action)
    todos = _make_todos(existing_eids=[eid])
    result = todos.push_todo("db-123", action)
    assert result is False
    todos._client.pages.create.assert_not_called()


def test_push_todo_iso_due_date_set_in_properties() -> None:
    todos = _make_todos(existing_eids=[])
    action = _action(due="2026-06-25")
    todos.push_todo("db-123", action)
    call_kwargs = todos._client.pages.create.call_args[1]
    props = call_kwargs["properties"]
    assert "Due" in props
    assert props["Due"]["date"]["start"] == "2026-06-25"


def test_push_todo_non_iso_due_date_not_set() -> None:
    """Free-text due dates like "Friday" must not crash and must not set Due."""
    todos = _make_todos(existing_eids=[])
    action = _action(due="Friday")
    todos.push_todo("db-123", action)
    call_kwargs = todos._client.pages.create.call_args[1]
    props = call_kwargs["properties"]
    assert "Due" not in props


def test_push_todo_none_due_date_not_set() -> None:
    todos = _make_todos(existing_eids=[])
    action = _action(due=None)
    todos.push_todo("db-123", action)
    call_kwargs = todos._client.pages.create.call_args[1]
    props = call_kwargs["properties"]
    assert "Due" not in props


def test_push_todo_status_passed_through() -> None:
    todos = _make_todos(existing_eids=[])
    action = _action()
    todos.push_todo("db-123", action, status="Approved")
    call_kwargs = todos._client.pages.create.call_args[1]
    props = call_kwargs["properties"]
    assert props["Status"]["select"]["name"] == "Approved"


def test_push_todo_sets_eclipse_id_in_properties() -> None:
    todos = _make_todos(existing_eids=[])
    action = _action()
    todos.push_todo("db-123", action)
    call_kwargs = todos._client.pages.create.call_args[1]
    props = call_kwargs["properties"]
    stored_eid = props["EclipseId"]["rich_text"][0]["text"]["content"]
    assert stored_eid == eclipse_id(action)


def test_push_todo_uses_prefetched_existing_without_querying() -> None:
    """When the caller passes `existing`, push_todo must not query Notion itself."""
    todos = _make_todos(existing_eids=[])
    action = _action()
    existing: set[str] = set()
    todos.push_todo("db-123", action, existing=existing)
    todos._client.request.assert_not_called()  # no per-action existing_ids query
    assert eclipse_id(action) in existing  # created id is added back for run-dedup


def test_push_todo_dedups_against_prefetched_set() -> None:
    todos = _make_todos(existing_eids=[])
    action = _action()
    existing = {eclipse_id(action)}
    result = todos.push_todo("db-123", action, existing=existing)
    assert result is False
    todos._client.pages.create.assert_not_called()
    todos._client.request.assert_not_called()
