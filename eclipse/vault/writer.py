"""Render a ProcessedMeeting into a Markdown note and file it under the vault."""

from __future__ import annotations

import re
from itertools import groupby
from pathlib import Path
from typing import Any

import frontmatter

from eclipse.models import ActionItem, ProcessedMeeting, Segment

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "untitled"


def _format_action(item: ActionItem) -> str:
    line = f"- [ ] {item.task}"
    if item.detail:
        line += f" — {item.detail}"
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


def _render_transcript_body(segments: list[Segment], fallback_text: str) -> str:
    """Return speaker-grouped transcript text when diarization data is present.

    If at least one segment carries a non-None speaker, consecutive segments from
    the same speaker are grouped and rendered as ``**SPEAKER_XX:** text...``.
    Otherwise the original flat ``fallback_text`` is returned unchanged.
    """
    if not any(s.speaker is not None for s in segments):
        return fallback_text

    lines: list[str] = []
    for speaker, group in groupby(segments, key=lambda s: s.speaker):
        combined = " ".join(seg.text.strip() for seg in group if seg.text.strip())
        if not combined:
            continue
        label = speaker if speaker is not None else "UNKNOWN"
        lines.append(f"**{label}:** {combined}")
    return "\n\n".join(lines)


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
    # Prefix every line so a multi-line summary stays inside the callout block.
    summary_lines = [f"> {ln}" for ln in (ins.summary.splitlines() or [""])]
    lines += ["> [!summary] Summary", *summary_lines, ""]

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
        transcript_body = _render_transcript_body(pm.transcript.segments, pm.transcript.text)
        lines += ["---", "", "## Transcript", "", transcript_body, ""]

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
