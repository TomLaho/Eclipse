"""Credentials, loaded from ``.env`` (git-ignored).

Kept separate from :class:`eclipse.config.Config`: behaviour lives in ``eclipse.toml``
(safe to commit as an example), secrets live here and never touch git. Field names map
case-insensitively to the upper-case ``.env`` keys (e.g. ``TELEGRAM_BOT_TOKEN``).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    notion_access_token: str | None = None
    notion_todos_db_id: str | None = None
    hf_token: str | None = None


def load_secrets() -> Secrets:
    return Secrets()
