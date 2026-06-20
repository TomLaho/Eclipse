"""Notion Todos integration (Phase 3).

Pushes Eclipse open actions to a Notion database with deduplication via a
stable ``EclipseId`` computed from the note path + task text.

Public API:
    eclipse_id(action) -> str           # deterministic dedup key
    NotionTodos.from_secrets() -> NotionTodos | None
    NotionTodos.create_database(parent_page_id, title) -> str
    NotionTodos.existing_ids(db_id) -> set[str]
    NotionTodos.push_todo(db_id, action, status) -> bool
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, cast

from notion_client import Client

from eclipse.log import get_logger
from eclipse.review import OpenAction
from eclipse.secrets import load_secrets

log = get_logger("notion")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def eclipse_id(action: OpenAction) -> str:
    """Return the first 16 hex chars of SHA-256(stem|task) — stable dedup key."""
    raw = f"{action.note_path.stem}|{action.task}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class NotionTodos:
    """Wrapper around the Notion client scoped to the Todos workflow."""

    def __init__(self, client: Client) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_secrets(cls) -> NotionTodos | None:
        """Return a configured instance, or None if the access token is missing."""
        s = load_secrets()
        if not s.notion_access_token:
            return None
        # Pin the classic API version: this module targets the 2022-06-28 contract
        # (databases/{id}/query, database_id page parents). notion-client 3.x
        # defaults to 2025-09-03, which moved querying to data_sources/{id}/query
        # and breaks the request paths below.
        client: Client = Client(auth=s.notion_access_token, notion_version="2022-06-28")
        return cls(client)

    # ------------------------------------------------------------------
    # Database management
    # ------------------------------------------------------------------

    def create_database(
        self,
        parent_page_id: str,
        title: str = "Eclipse Todos",
    ) -> str:
        """Create the Todos database under *parent_page_id* and return its id."""
        result: dict[str, Any] = cast(
            dict[str, Any],
            self._client.databases.create(
                parent={"type": "page_id", "page_id": parent_page_id},
                title=[{"type": "text", "text": {"content": title}}],
                properties={
                    "Name": {"title": {}},
                    "Status": {
                        "select": {
                            "options": [
                                {"name": "Review", "color": "yellow"},
                                {"name": "Approved", "color": "green"},
                                {"name": "Done", "color": "gray"},
                            ]
                        }
                    },
                    "Owner": {"rich_text": {}},
                    "Due": {"date": {}},
                    "Client": {"rich_text": {}},
                    "Source": {"rich_text": {}},
                    "EclipseId": {"rich_text": {}},
                },
            ),
        )
        db_id: str = result["id"]
        return db_id

    # ------------------------------------------------------------------
    # Page operations
    # ------------------------------------------------------------------

    def _query_database(
        self,
        db_id: str,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Query a database via the raw REST path (notion-client v3 removed databases.query)."""
        body: dict[str, Any] = {"page_size": page_size}
        if start_cursor is not None:
            body["start_cursor"] = start_cursor
        return cast(
            dict[str, Any],
            self._client.request(
                path=f"databases/{db_id}/query",
                method="POST",
                body=body,
            ),
        )

    def existing_ids(self, db_id: str) -> set[str]:
        """Return the set of EclipseId values already present in *db_id*."""
        ids: set[str] = set()
        cursor: str | None = None

        while True:
            result = self._query_database(db_id, start_cursor=cursor)

            for page in result.get("results", []):
                props = page.get("properties", {})
                eid_prop = props.get("EclipseId", {})
                rich_text = eid_prop.get("rich_text", [])
                if rich_text:
                    ids.add(rich_text[0]["text"]["content"])

            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

        return ids

    def push_todo(
        self,
        db_id: str,
        action: OpenAction,
        status: str = "Review",
        existing: set[str] | None = None,
    ) -> bool:
        """Create a Notion page for *action* in *db_id*.

        Returns False without creating if the EclipseId is already present
        (dedup). Returns True on successful creation. Pass *existing* (from
        ``existing_ids``) when pushing in a loop to avoid one query per action;
        newly-created ids are added to it so same-run duplicates are caught too.
        """
        eid = eclipse_id(action)
        if existing is None:
            existing = self.existing_ids(db_id)
        if eid in existing:
            log.info("notion_todo_skip_duplicate", eclipse_id=eid, task=action.task)
            return False

        # Build page properties.
        properties: dict[str, Any] = {
            "Name": {"title": [{"type": "text", "text": {"content": action.task}}]},
            "Status": {"select": {"name": status}},
            "EclipseId": {"rich_text": [{"type": "text", "text": {"content": eid}}]},
            "Source": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"{action.meeting_title} ({action.meeting_date})"},
                    }
                ]
            },
        }

        if action.owner:
            properties["Owner"] = {
                "rich_text": [{"type": "text", "text": {"content": action.owner}}]
            }

        if action.client:
            properties["Client"] = {
                "rich_text": [{"type": "text", "text": {"content": action.client}}]
            }

        # Only set Due if the value looks like a valid ISO date (YYYY-MM-DD).
        if action.due and _ISO_DATE_RE.match(action.due):
            properties["Due"] = {"date": {"start": action.due}}

        self._client.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
        existing.add(eid)  # dedup later actions in the same run against this one
        log.info("notion_todo_pushed", eclipse_id=eid, task=action.task)
        return True
