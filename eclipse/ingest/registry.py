"""SQLite registry of processed audio, keyed by content hash (idempotent ingest)."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    hash         TEXT PRIMARY KEY,
    source_name  TEXT NOT NULL,
    vault_path   TEXT,
    status       TEXT NOT NULL DEFAULT 'complete',
    processed_at TEXT NOT NULL
);
"""


def hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Return the SHA-256 of a file's contents."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk_size):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class Entry:
    hash: str
    source_name: str
    vault_path: str | None
    status: str
    processed_at: str


class Registry:
    """Tracks which audio files have already been processed."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def is_processed(self, file_hash: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM processed WHERE hash = ? AND status = 'complete'",
            (file_hash,),
        )
        return cur.fetchone() is not None

    def record(
        self,
        file_hash: str,
        source_name: str,
        vault_path: str | None,
        status: str = "complete",
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO processed "
            "(hash, source_name, vault_path, status, processed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                file_hash,
                source_name,
                vault_path,
                status,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def get(self, file_hash: str) -> Entry | None:
        row = self._conn.execute("SELECT * FROM processed WHERE hash = ?", (file_hash,)).fetchone()
        return _row_to_entry(row) if row else None

    def all(self) -> list[Entry]:
        rows = self._conn.execute("SELECT * FROM processed ORDER BY processed_at DESC").fetchall()
        return [_row_to_entry(r) for r in rows]

    def count(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM processed WHERE status = 'complete'"
            ).fetchone()[0]
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Registry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        hash=row["hash"],
        source_name=row["source_name"],
        vault_path=row["vault_path"],
        status=row["status"],
        processed_at=row["processed_at"],
    )
