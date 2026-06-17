"""Render a ProcessedMeeting into a Markdown note and file it under the vault."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import frontmatter

from eclipse.models import ActionItem, ProcessedMeeting

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "untitled"


def _format_action(item: ActionItem) -> str:
    line = f"- [ ] {item.task}"
    meta = []
    if item.owner:
        meta.append(f"**{item.owner}**")
    if item.due_iso:
        # resolved ISO date first (machine-friendly), spoken phrase in parens
        meta.append(f"due: {item.due_iso} ({item.due})" if item.due else f"due: {item.due_iso}")
    elif item.due:
        meta.append(f"due: {item.due}")
    if meta:
        line += " | " + " | ".join(meta)
    return line


def render_markdown(pm: ProcessedMeeting) -> str:
    ins = pm.insights
    duration_min = round(pm.transcript.duration_sec / 60.0, 1)

    meta: dict[str, Any] = {
        "title": ins.title,
        "date": pm.meeting_date.isoformat(),
        "client": ins.client,
        "attendees": ins.attendees,
        "tags": ins.tags,
        "duration_min": duration_min,
        "source_audio": pm.audio_relpath,
        "transcribed_with": pm.transcribed_with,
        "enriched_with": pm.enriched_with,
        "status": "complete" if pm.enriched else "unenriched",
        "eclipse_hash": pm.file_hash,
    }

    lines: list[str] = [f"# {ins.title}", ""]
    lines += ["> [!summary] Summary", f"> {ins.summary}", ""]

    lines += ["## Action items", ""]
    if ins.action_items:
        lines += [_format_action(a) for a in ins.action_items]
    else:
        lines.append("_None captured._")
    lines.append("")

    if ins.decisions:
        lines += ["## Decisions", ""] + [f"- {d}" for d in ins.decisions] + [""]

    if ins.follow_ups:
        lines += ["## Follow-ups", ""] + [f"- {f}" for f in ins.follow_ups] + [""]

    if ins.attendees:
        lines += ["## Attendees", ""] + [f"- {a}" for a in ins.attendees] + [""]

    if pm.transcript.text:
        lines += ["---", "", "## Transcript", "", pm.transcript.text, ""]

    post = frontmatter.Post("\n".join(lines))
    post.metadata = meta
    return frontmatter.dumps(post)


def write_note(vault_dir: Path, pm: ProcessedMeeting) -> Path:
    """Write the note under ``vault/<client>/<date>-<slug>.md`` and return its path."""
    client_dir = vault_dir / slugify(pm.insights.client)
    client_dir.mkdir(parents=True, exist_ok=True)

    base = f"{pm.meeting_date.isoformat()}-{slugify(pm.insights.title)}"
    path = client_dir / f"{base}.md"
    n = 2
    while path.exists():
        path = client_dir / f"{base}-{n}.md"
        n += 1

    path.write_text(render_markdown(pm), encoding="utf-8")
    return path
