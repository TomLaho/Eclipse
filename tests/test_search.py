from datetime import date
from pathlib import Path

from eclipse import search
from eclipse.config import Config
from eclipse.models import ActionItem, MeetingInsights, ProcessedMeeting, TranscriptResult
from eclipse.review import Note
from eclipse.vault.writer import write_note

# tiny deterministic "embedding": a bag-of-words count over a fixed vocabulary, so
# texts that share keywords get similar vectors and retrieval is predictable.
_VOCAB = ["budget", "deck", "kickoff", "acme", "beta", "launch", "hiring"]


class FakeEnricher:
    def __init__(self) -> None:
        self.calls = 0

    def model_present(self, model: str) -> bool:
        return True

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for t in texts:
            low = t.lower()
            out.append([float(low.count(word)) for word in _VOCAB])
        return out


def _seed(vault: Path, title: str, summary: str, client: str, task: str) -> None:
    pm = ProcessedMeeting(
        source_name=f"{title}.m4a",
        file_hash=title,
        meeting_date=date(2026, 6, 10),
        transcript=TranscriptResult(text=summary, duration_sec=60),
        insights=MeetingInsights(
            title=title,
            summary=summary,
            client=client,
            action_items=[ActionItem(task=task, owner="Tom")],
        ),
    )
    write_note(vault, pm)


def _cfg(tmp_path: Path) -> Config:
    return Config(
        vault_dir=tmp_path / "vault",
        embeddings_path=tmp_path / "emb.sqlite",
    )


def test_chunk_note_prefixes_header() -> None:
    note = Note(
        path=Path("x.md"),
        meta={"title": "Kickoff", "client": "Acme", "date": "2026-06-10"},
        body="> Summary line.\n\nSome body paragraph about budget.",
    )
    chunks = search.chunk_note(note)
    assert chunks
    assert all(c.startswith("[Kickoff | Acme | 2026-06-10]") for c in chunks)


def test_pack_unpack_roundtrip() -> None:
    import math

    vec = [0.1, -0.5, 2.0, 0.0]
    out = search._unpack(search._pack(vec))
    assert len(out) == len(vec)
    assert all(
        math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in zip(out, vec, strict=True)
    )


def test_refresh_is_incremental(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg.vault_dir, "Kickoff", "Kickoff for the beta launch.", "Acme", "Send deck")
    enricher = FakeEnricher()

    with search.EmbeddingIndex(cfg.embeddings_path) as idx:
        embedded, total = idx.refresh(cfg, enricher)  # type: ignore[arg-type]
        assert embedded == 1
        assert total > 0
        # second refresh: nothing changed, so no note is re-embedded
        embedded2, _ = idx.refresh(cfg, enricher)  # type: ignore[arg-type]
        assert embedded2 == 0


def test_search_ranks_relevant_chunk_first(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg.vault_dir, "Kickoff", "Kickoff for the beta launch.", "Acme", "Send deck")
    _seed(cfg.vault_dir, "Finance", "Reviewing the hiring budget.", "Globex", "Review budget")
    enricher = FakeEnricher()

    with search.EmbeddingIndex(cfg.embeddings_path) as idx:
        idx.refresh(cfg, enricher)  # type: ignore[arg-type]
        qvec = enricher.embed(["budget"], cfg.embed_model)[0]
        hits = idx.search(qvec, k=3)

    assert hits
    assert "budget" in hits[0].text.lower()
    assert hits[0].score >= hits[-1].score


def test_model_change_rebuilds_index(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg.vault_dir, "Kickoff", "Kickoff for the beta launch.", "Acme", "Send deck")
    enricher = FakeEnricher()

    with search.EmbeddingIndex(cfg.embeddings_path) as idx:
        idx.refresh(cfg, enricher)  # type: ignore[arg-type]
        first = idx.count()
        assert first > 0

    cfg2 = _cfg(tmp_path)
    cfg2.embed_model = "different-model"
    with search.EmbeddingIndex(cfg2.embeddings_path) as idx:
        embedded, total = idx.refresh(cfg2, enricher)  # type: ignore[arg-type]
        assert embedded == 1  # cleared and rebuilt
        assert total == first
