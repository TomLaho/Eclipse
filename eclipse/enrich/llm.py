"""Ollama-backed enrichment with structured output, retry, and a safe fallback."""

from __future__ import annotations

import json
import re
from datetime import date

import httpx
from pydantic import ValidationError

from eclipse.enrich.prompts import (
    MAP_SYSTEM,
    MAP_TEMPLATE,
    SECOND_PASS_SYSTEM,
    SECOND_PASS_TEMPLATE,
    SYSTEM_PROMPT,
    USER_TEMPLATE,
)
from eclipse.log import get_logger
from eclipse.models import MeetingInsights

log = get_logger("enrich")

# Keep the prompt within a small local model's context window on a low-RAM machine.
_TRANSCRIPT_BUDGET = 9000
# Above this, map-reduce (chunk -> condense -> merge) instead of head/tail trimming.
_MAPREDUCE_THRESHOLD = 14000
_CHUNK_SIZE = 6000


_LEADING_DATE = re.compile(r"^20\d{2}[-_]?\d{2}[-_]?\d{2}[-_ ]*")


def _title_from_filename(source_name: str) -> str:
    stem = source_name.rsplit(".", 1)[0]
    stem = _LEADING_DATE.sub("", stem).replace("_", " ").replace("-", " ").strip()
    return stem[:1].upper() + stem[1:] if stem else source_name


def _trim(transcript: str, budget: int = _TRANSCRIPT_BUDGET) -> str:
    if len(transcript) <= budget:
        return transcript
    head = transcript[: int(budget * 0.7)]
    tail = transcript[-int(budget * 0.3) :]
    return f"{head}\n...[transcript truncated for length]...\n{tail}"


class OllamaEnricher:
    """Calls a local Ollama model and validates its JSON into ``MeetingInsights``."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        timeout: float = 600.0,
        two_pass: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.two_pass = two_pass

    def available(self) -> bool:
        """True if the Ollama server responds and the model is present."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            resp.raise_for_status()
        except (httpx.HTTPError, OSError):
            return False
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        # tolerate "llama3.2:3b" vs "llama3.2:3b-instruct-q4_K_M" style suffixes
        base = self.model.split(":")[0]
        return any(n == self.model or n.startswith(base) for n in names)

    def enrich(
        self, transcript: str, meeting_date: date, source_name: str
    ) -> tuple[MeetingInsights, bool]:
        """Return (insights, enriched). ``enriched`` is False if we fell back."""
        if not transcript.strip():
            return self._fallback(transcript, source_name), False

        try:
            condensed = self._condense(transcript)
        except (httpx.HTTPError, OSError) as exc:
            log.warning("ollama_unreachable", error=str(exc))
            return self._fallback(transcript, source_name), False

        prompt = USER_TEMPLATE.format(
            source_name=source_name,
            meeting_date=meeting_date.isoformat(),
            transcript=condensed,
        )

        insights: MeetingInsights | None = None
        for attempt in (1, 2):
            try:
                content = self._call(SYSTEM_PROMPT, prompt)
                insights = MeetingInsights.model_validate_json(content)
                break
            except (httpx.HTTPError, OSError) as exc:
                log.warning("ollama_unreachable", error=str(exc))
                break  # server down: don't retry, fall back
            except (json.JSONDecodeError, ValidationError) as exc:
                log.warning("ollama_bad_json", attempt=attempt, error=str(exc))
                continue

        if insights is None:
            log.warning("enrichment_fallback", source=source_name)
            return self._fallback(transcript, source_name), False

        if self.two_pass:
            self._merge_missed(condensed, insights)
        return insights, True

    def _condense(self, transcript: str) -> str:
        """Fit the transcript to the model: trim if short, map-reduce if long."""
        if len(transcript) <= _TRANSCRIPT_BUDGET:
            return transcript
        if len(transcript) <= _MAPREDUCE_THRESHOLD:
            return _trim(transcript)

        chunks = [
            transcript[i : i + _CHUNK_SIZE] for i in range(0, len(transcript), _CHUNK_SIZE)
        ]
        log.info("mapreduce_condense", chars=len(transcript), chunks=len(chunks))
        condensed_parts = [
            self.chat(
                MAP_SYSTEM,
                MAP_TEMPLATE.format(part=i, total=len(chunks), chunk=chunk),
            )
            for i, chunk in enumerate(chunks, start=1)
        ]
        merged = "\n\n".join(condensed_parts)
        # a very long meeting can still overflow after one pass; trim the result
        return _trim(merged)

    def _merge_missed(self, transcript: str, insights: MeetingInsights) -> None:
        """Second pass: ask what was missed and merge new items in place."""
        already = "\n".join(
            [f"- action: {a.task}" for a in insights.action_items]
            + [f"- decision: {d}" for d in insights.decisions]
            + [f"- follow-up: {f}" for f in insights.follow_ups]
        ) or "(nothing yet)"
        prompt = SECOND_PASS_TEMPLATE.format(already=already, transcript=transcript)
        try:
            content = self._call(SECOND_PASS_SYSTEM, prompt)
            missed = MeetingInsights.model_validate_json(_coerce_missed(content))
        except (httpx.HTTPError, OSError, json.JSONDecodeError, ValidationError) as exc:
            log.warning("second_pass_skipped", error=str(exc))
            return

        _merge_unique(insights, missed)
        log.info(
            "second_pass_merged",
            added_actions=len(missed.action_items),
            added_decisions=len(missed.decisions),
        )

    def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        """Free-form chat completion (used by ask/digest). Raises on transport error."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": temperature, "num_ctx": 8192, "num_predict": 1000},
        }
        resp = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return str(resp.json()["message"]["content"])

    def _call(self, system: str, prompt: str) -> str:
        # format="json" (not a full schema grammar): far faster on CPU and still
        # reliably parseable; we validate the shape with pydantic + retry.
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": "10m",
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 800},
        }
        resp = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return str(resp.json()["message"]["content"])

    def _fallback(self, transcript: str, source_name: str) -> MeetingInsights:
        """Minimal insights so a note is still written when the LLM is unavailable."""
        snippet = transcript.strip().replace("\n", " ")[:280]
        return MeetingInsights(
            title=_title_from_filename(source_name),
            summary=snippet or "(no speech detected)",
            client="General",
            tags=["unenriched"],
        )

    def descriptor(self) -> str:
        return self.model


def _coerce_missed(content: str) -> str:
    """The second pass omits title/summary; add placeholders so it validates."""
    data = json.loads(content)
    data.setdefault("title", "")
    data.setdefault("summary", "")
    return json.dumps(data)


def _merge_unique(into: MeetingInsights, extra: MeetingInsights) -> None:
    """Append items from ``extra`` that aren't already in ``into`` (case-insensitive)."""
    have_actions = {a.task.strip().lower() for a in into.action_items}
    for a in extra.action_items:
        if a.task.strip() and a.task.strip().lower() not in have_actions:
            into.action_items.append(a)
            have_actions.add(a.task.strip().lower())

    into.decisions.extend(_new_strings(into.decisions, extra.decisions))
    into.follow_ups.extend(_new_strings(into.follow_ups, extra.follow_ups))


def _new_strings(existing: list[str], candidates: list[str]) -> list[str]:
    have = {s.strip().lower() for s in existing}
    out: list[str] = []
    for s in candidates:
        if s.strip() and s.strip().lower() not in have:
            out.append(s)
            have.add(s.strip().lower())
    return out
