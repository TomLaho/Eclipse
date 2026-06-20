"""Cross-meeting review: read the vault, roll up open actions, answer questions.

This is the "am I on top of everything" layer. Open-action extraction is
deterministic (it parses Markdown checkboxes, so ticking a box in Obsidian
updates the rollup); the LLM is only used for optional narrative and Q&A.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from eclipse.config import Config
from eclipse.enrich.llm import OllamaEnricher
from eclipse.log import get_logger

log = get_logger("review")

# folders inside the vault that are not meeting notes
_RESERVED = {"_audio", "_digests", "_todos", "_attachments"}

_OPEN_RE = re.compile(r"^\s*-\s*\[ \]\s*(.+?)\s*$")
_OWNER_RE = re.compile(r"\*\*(.+?)\*\*")
_DUE_RE = re.compile(r"due:\s*([^|]+)")


@dataclass
class OpenAction:
    task: str
    owner: str | None
    due: str | None
    client: str
    meeting_title: str
    meeting_date: str
    note_path: Path


@dataclass
class Note:
    path: Path
    meta: dict[str, object]
    body: str
    open_actions: list[OpenAction] = field(default_factory=list)

    @property
    def title(self) -> str:
        return str(self.meta.get("title", self.path.stem))

    @property
    def client(self) -> str:
        return str(self.meta.get("client", "General"))

    @property
    def date(self) -> str:
        return str(self.meta.get("date", ""))

    @property
    def summary(self) -> str:
        # first paragraph after a "> " summary callout, else first non-heading line
        for line in self.body.splitlines():
            stripped = line.lstrip("> ").strip()
            if stripped and not stripped.startswith(("#", "[!", "-", "_")):
                return stripped
        return ""


def _parse_open_actions(note_meta: dict[str, object], body: str, path: Path) -> list[OpenAction]:
    actions: list[OpenAction] = []
    for line in body.splitlines():
        m = _OPEN_RE.match(line)
        if not m:
            continue
        rest = m.group(1)
        task = rest.split(" | ")[0].strip()
        owner_m = _OWNER_RE.search(rest)
        due_m = _DUE_RE.search(rest)
        actions.append(
            OpenAction(
                task=task,
                owner=owner_m.group(1) if owner_m else None,
                due=due_m.group(1).strip() if due_m else None,
                client=str(note_meta.get("client", "General")),
                meeting_title=str(note_meta.get("title", path.stem)),
                meeting_date=str(note_meta.get("date", "")),
                note_path=path,
            )
        )
    return actions


def iter_notes(vault_dir: Path) -> Iterator[Note]:
    for md in sorted(vault_dir.rglob("*.md")):
        if any(part in _RESERVED for part in md.relative_to(vault_dir).parts[:-1]):
            continue
        post = frontmatter.load(md)
        note = Note(path=md, meta=dict(post.metadata), body=post.content)
        note.open_actions = _parse_open_actions(note.meta, post.content, md)
        yield note


def collect_open_actions(
    vault_dir: Path, me_aliases: list[str] | None = None, mine_only: bool = False
) -> list[OpenAction]:
    actions: list[OpenAction] = []
    for note in iter_notes(vault_dir):
        actions.extend(note.open_actions)
    if mine_only and me_aliases:
        lowered = {a.strip().lower() for a in me_aliases}
        actions = [a for a in actions if a.owner and a.owner.strip().lower() in lowered]
    # sort by date (oldest open items first = most likely to be slipping)
    return sorted(actions, key=lambda a: a.meeting_date)


# --- ask-your-meetings ----------------------------------------------------

_CORPUS_BUDGET = 12000


def build_corpus(vault_dir: Path, budget: int = _CORPUS_BUDGET) -> str:
    """Compact, newest-first digest of every meeting for retrieval-free Q&A."""
    blocks: list[str] = []
    notes = sorted(iter_notes(vault_dir), key=lambda n: n.date, reverse=True)
    used = 0
    for note in notes:
        open_lines = "\n".join(f"  - [ ] {a.task}" for a in note.open_actions)
        block = f"### {note.title} — {note.client} ({note.date})\n{note.summary}\n" + (
            f"Open actions:\n{open_lines}\n" if open_lines else ""
        )
        if used + len(block) > budget:
            break
        blocks.append(block)
        used += len(block)
    return "\n".join(blocks)


_ASK_SYSTEM = (
    "You answer questions using ONLY the provided meeting notes. "
    "Cite the meeting title and date for any claim. If the notes do not contain "
    "the answer, say so plainly. Be concise."
)


def answer_question(cfg: Config, question: str) -> str:
    enricher = OllamaEnricher(
        cfg.ollama_base_url,
        cfg.ollama_model,
        cfg.ollama_timeout_sec,
        context_profile=cfg.context_profile,
    )
    if not enricher.available():
        return "Local LLM (Ollama) is not reachable, so Q&A is unavailable. Run `ollama serve`."
    corpus = _retrieve_corpus(cfg, enricher, question)
    if not corpus.strip():
        return "No meetings in the vault yet."
    user = f"MEETING NOTES:\n{corpus}\n\nQUESTION: {question}"
    return enricher.chat(_ASK_SYSTEM, user)


def _retrieve_corpus(cfg: Config, enricher: OllamaEnricher, question: str) -> str:
    """Most-relevant chunks via embeddings when available; else every summary.

    Semantic retrieval scales to large vaults; the compact-summary corpus is the
    fallback when the embedding model isn't pulled or anything goes wrong.
    """
    if enricher.model_present(cfg.embed_model):
        try:
            from eclipse.search import EmbeddingIndex, semantic_corpus

            with EmbeddingIndex(cfg.embeddings_path) as index:
                index.refresh(cfg, enricher)
                qvec = enricher.embed([question], cfg.embed_model)[0]
                hits = index.search(qvec, cfg.ask_top_k)
            if hits:
                return semantic_corpus(hits)
        except Exception as exc:  # fall back rather than fail the question
            log.warning("semantic_search_failed", error=str(exc))
    return build_corpus(cfg.vault_dir)


# --- digest ("nothing missed") -------------------------------------------

_DIGEST_SYSTEM = (
    "You are a chief-of-staff. Given a list of open action items from meetings, "
    "write a 3-5 bullet briefing of what most needs attention: overdue or stale "
    "commitments, anything that looks like it is slipping, and recurring themes. "
    "Be specific and concise. Do not invent items."
)


def _action_line(a: OpenAction) -> str:
    meta = []
    if a.owner:
        meta.append(f"**{a.owner}**")
    if a.due:
        meta.append(f"due: {a.due}")
    meta.append(f"[[{a.note_path.stem}]]")
    meta.append(a.meeting_date)
    return f"- [ ] {a.task} | {' | '.join(meta)}"


def build_digest(cfg: Config, with_briefing: bool = True) -> str:
    from datetime import date as _date

    actions = collect_open_actions(cfg.vault_dir)
    mine = [
        a
        for a in actions
        if a.owner and a.owner.strip().lower() in {x.lower() for x in cfg.me_aliases}
    ]

    lines = [
        f"# Eclipse digest — {_date.today().isoformat()}",
        "",
        f"**{len(actions)} open action items** across the vault ({len(mine)} yours).",
        "",
    ]

    if with_briefing and actions:
        enricher = OllamaEnricher(
            cfg.ollama_base_url,
            cfg.ollama_model,
            cfg.ollama_timeout_sec,
            context_profile=cfg.context_profile,
        )
        if enricher.available():
            listing = "\n".join(
                f"- {a.task} (owner: {a.owner or '?'}, due: {a.due or '?'}, "
                f"meeting: {a.meeting_title} {a.meeting_date})"
                for a in actions
            )
            try:
                briefing = enricher.chat(_DIGEST_SYSTEM, listing)
                lines += ["## Briefing", "", briefing, ""]
            except Exception:
                pass

    by_client: dict[str, list[OpenAction]] = {}
    for a in actions:
        by_client.setdefault(a.client, []).append(a)

    lines += ["## Open action items", ""]
    if not actions:
        lines.append("_Nothing open. You're on top of everything._")
    for client in sorted(by_client):
        lines += [f"### {client}", ""]
        lines += [_action_line(a) for a in by_client[client]]
        lines.append("")

    return "\n".join(lines)


def write_digest(cfg: Config, with_briefing: bool = True, body: str | None = None) -> Path:
    from datetime import date as _date

    # Reuse a pre-built body when the caller already has one — building it again
    # re-runs the LLM briefing, which is a multi-minute call on a slow CPU box.
    if body is None:
        body = build_digest(cfg, with_briefing=with_briefing)
    out_dir = cfg.vault_dir / "_digests"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_date.today().isoformat()}-digest.md"
    path.write_text(body, encoding="utf-8")
    return path


def write_todo_draft(cfg: Config) -> tuple[Path, int]:
    """Draft *your* open commitments to a review file (the pre-Notion approval step)."""
    from datetime import date as _date

    mine = collect_open_actions(cfg.vault_dir, cfg.me_aliases, mine_only=True)
    out_dir = cfg.vault_dir / "_todos"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_date.today().isoformat()}-pending.md"
    lines = [
        f"# Your open commitments — {_date.today().isoformat()}",
        "",
        "Review and tick to approve. (Notion push lands in a later version.)",
        "",
    ]
    lines += [_action_line(a) for a in mine] or ["_None._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, len(mine)
