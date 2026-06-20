"""Semantic search over the vault via local embeddings (Ollama).

`ask` defaults to feeding the LLM a compact summary of every meeting. That's fine
for a small vault but does not scale. This module builds a SQLite index of embedded
note *chunks* so a question retrieves only the most relevant passages, regardless of
how many meetings there are. Indexing is incremental: a note is re-embedded only when
its content changes, and the whole index is rebuilt if the embedding model changes.

Vectors are stored as unit-length float32 blobs, so cosine similarity is a plain dot
product. For a vault of hundreds of meetings this is a few thousand short vectors —
an in-memory scan per query is well under a second, so no vector-DB dependency.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from array import array
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from eclipse.config import Config
from eclipse.enrich.llm import OllamaEnricher
from eclipse.log import get_logger
from eclipse.review import Note, iter_notes

log = get_logger("search")

_CHUNK_CHARS = 900
_EMBED_BATCH = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    note_path TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    note_hash TEXT NOT NULL,
    title     TEXT,
    date      TEXT,
    client    TEXT,
    text      TEXT NOT NULL,
    vector    BLOB NOT NULL,
    PRIMARY KEY (note_path, chunk_idx)
);
"""


@dataclass(frozen=True)
class Hit:
    text: str
    title: str
    date: str
    client: str
    score: float


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def chunk_note(note: Note) -> list[str]:
    """Split a note into retrieval units, each prefixed with the meeting header.

    The header keeps a retrieved passage self-describing (which meeting it came from)
    so the LLM can cite it without the rest of the note.
    """
    header = f"{note.title} | {note.client} | {note.date}"
    units: list[str] = []
    if note.summary:
        units.append(note.summary)

    paras = [p.strip() for p in re.split(r"\n\s*\n", note.body) if p.strip()]
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > _CHUNK_CHARS:
            units.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        units.append(buf)

    return [f"[{header}]\n{u}" for u in units]


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


class EmbeddingIndex:
    """SQLite-backed store of embedded note chunks."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def clear(self) -> None:
        self._conn.execute("DELETE FROM chunks")
        self._conn.commit()

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def refresh(self, cfg: Config, enricher: OllamaEnricher) -> tuple[int, int]:
        """Bring the index in line with the vault. Returns (notes_embedded, chunks).

        Incremental: unchanged notes are skipped; removed notes are pruned. The whole
        index is rebuilt if the embedding model changed (vectors aren't comparable
        across models).
        """
        if self._get_meta("embed_model") != cfg.embed_model:
            self.clear()
            self._set_meta("embed_model", cfg.embed_model)
            self._conn.commit()

        notes = list(iter_notes(cfg.vault_dir))
        on_disk = {str(n.path): n for n in notes}

        # all chunks of a note share one hash; aggregate so a partial prior write
        # can't yield two rows and a silently-wrong cache decision.
        stored_hash = {
            str(row["note_path"]): str(row["note_hash"])
            for row in self._conn.execute(
                "SELECT note_path, MAX(note_hash) AS note_hash FROM chunks GROUP BY note_path"
            )
        }

        embedded = 0
        # one transaction per note: a failed embed leaves that note's prior chunks
        # intact rather than deleting them and dying before the re-insert.
        for path in set(stored_hash) - set(on_disk):
            with self._conn:  # prune notes that were deleted or renamed
                self._conn.execute("DELETE FROM chunks WHERE note_path = ?", (path,))

        for path, note in on_disk.items():
            body_hash = _content_hash(note.body)
            if stored_hash.get(path) == body_hash:
                continue
            texts = chunk_note(note)
            if not texts:
                continue
            vectors = _embed_batched(enricher, texts, cfg.embed_model)
            rows = [
                (path, i, body_hash, note.title, note.date, note.client, t, _pack(_unit(v)))
                for i, (t, v) in enumerate(zip(texts, vectors, strict=True))
            ]
            with self._conn:  # delete + re-insert atomically
                self._conn.execute("DELETE FROM chunks WHERE note_path = ?", (path,))
                self._conn.executemany(
                    "INSERT INTO chunks "
                    "(note_path, chunk_idx, note_hash, title, date, client, text, vector) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            embedded += 1

        return embedded, self.count()

    def search(self, query_vec: list[float], k: int) -> list[Hit]:
        q = _unit(query_vec)
        dim = len(q)
        hits: list[Hit] = []
        for row in self._conn.execute("SELECT title, date, client, text, vector FROM chunks"):
            vec = _unpack(row["vector"])
            if len(vec) != dim:  # stale vector from a different embedding model
                continue
            score = sum(a * b for a, b in zip(vec, q, strict=True))
            hits.append(
                Hit(
                    text=str(row["text"]),
                    title=str(row["title"]),
                    date=str(row["date"]),
                    client=str(row["client"]),
                    score=score,
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EmbeddingIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _embed_batched(enricher: OllamaEnricher, texts: list[str], model: str) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        out.extend(enricher.embed(texts[i : i + _EMBED_BATCH], model))
    return out


def semantic_corpus(hits: list[Hit]) -> str:
    """Format retrieved chunks into a corpus block for the LLM."""
    return "\n\n".join(h.text for h in hits)
