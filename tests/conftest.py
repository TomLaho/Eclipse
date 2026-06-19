"""Shared test fixtures and safety guards."""

from __future__ import annotations

import pytest

from eclipse.secrets import Secrets


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee no test sends real Telegram/Notion traffic.

    ``Config`` reads the developer's ``eclipse.toml`` from the working directory,
    so a test that constructs one can pick up ``telegram_enabled = true`` and then
    invoke the real notifier. This stubs the credential loaders to empty so the
    notifier always short-circuits. Tests that need configured secrets override
    this with their own ``monkeypatch`` (applied after this fixture).
    """
    empty = Secrets(
        telegram_bot_token=None,
        telegram_chat_id=None,
        notion_access_token=None,
        notion_todos_db_id=None,
        hf_token=None,
    )
    for target in (
        "eclipse.notify.telegram.load_secrets",
        "eclipse.notify.notion.load_secrets",
    ):
        monkeypatch.setattr(target, lambda: empty, raising=False)
