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

# Feed the whole transcript for a typical meeting (~35-60 min) so mid-conversation
# commitments/figures aren't dropped. Larger than this is head/tail trimmed to stay
# under the request timeout on a slow CPU.
_TRANSCRIPT_BUDGET = 16000
# Above this, map-reduce (chunk -> condense -> merge) instead of head/tail trimming.
# Set high deliberately: map-reduce fires N sequential LLM calls, and on a slow
# CPU box (~1-3 tok/s) each call can exceed the request timeout. Single-pass
# head/tail trimming keeps normal meetings (up to ~75 min) to one call; only
# genuinely huge transcripts fall back to map-reduce.
_MAPREDUCE_THRESHOLD = 30000
_CHUNK_SIZE = 6000
# Context window: 8192 holds a full ~16k-char transcript (~4k tokens) plus the
# system prompt and generated JSON. Prompt-eval cost scales with actual tokens,
# not this ceiling, so the larger window only matters for long meetings.
_NUM_CTX = 8192


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
        model: str = "llama3.2:3b",
        timeout: float = 600.0,
        two_pass: bool = True,
        context_profile: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.two_pass = two_pass
        self.context_profile = context_profile.strip()

    def available(self) -> bool:
        """True if the Ollama server responds and the chat model is present."""
        return self.model_present(self.model)

    def model_present(self, model: str) -> bool:
        """True if the Ollama server responds and ``model`` is pulled."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            resp.raise_for_status()
        except (httpx.HTTPError, OSError):
            return False
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        # tolerate "llama3.2:3b" vs "llama3.2:3b-instruct-q4_K_M" style suffixes
        base = model.split(":")[0]
        return any(n == model or n.startswith(base) for n in names)

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """Return an embedding vector per input text. Raises on transport error."""
        if not texts:
            return []
        payload = {"model": model, "input": texts}
        resp = httpx.post(f"{self.base_url}/api/embed", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return [[float(x) for x in vec] for vec in resp.json()["embeddings"]]

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

        chunks = [transcript[i : i + _CHUNK_SIZE] for i in range(0, len(transcript), _CHUNK_SIZE)]
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
        already = (
            "\n".join(
                [f"- action: {a.task}" for a in insights.action_items]
                + [f"- decision: {d}" for d in insights.decisions]
                + [f"- follow-up: {f}" for f in insights.follow_ups]
            )
            or "(nothing yet)"
        )
        prompt = SECOND_PASS_TEMPLATE.format(already=already, transcript=transcript)
        try:
            content = self._call(SECOND_PASS_SYSTEM, prompt)
            missed = MeetingInsights.model_validate_json(_coerce_missed(content))
        except (httpx.HTTPError, OSError, json.JSONDecodeError, ValidationError) as exc:
            log.warning("second_pass_skipped", error=str(exc))
            return

        added = _merge_unique(insights, missed)
        insights.missed_items.extend(added)
        log.info("second_pass_merged", surfaced=len(added))

    def _with_profile(self, system: str) -> str:
        """Prepend the user's standing context so every call is tailored to them."""
        if not self.context_profile:
            return system
        return (
            "STANDING CONTEXT about the user and their work (use it to interpret the "
            "transcript and weight what matters; do NOT treat it as facts to extract):\n"
            f"{self.context_profile}\n\n---\n\n{system}"
        )

    def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        """Free-form chat completion (used by ask/digest). Raises on transport error."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._with_profile(system)},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": temperature, "num_ctx": _NUM_CTX, "num_predict": 1000},
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
                {"role": "system", "content": self._with_profile(system)},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": "10m",
            "options": {"temperature": 0.1, "num_ctx": _NUM_CTX, "num_predict": 800},
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


def _merge_unique(into: MeetingInsights, extra: MeetingInsights) -> list[str]:
    """Append items from ``extra`` not already in ``into`` (case-insensitive).

    Returns a readable description of each item that was newly added, so callers
    can surface "what the first pass missed" separately.
    """
    added: list[str] = []

    have_actions = {a.task.strip().lower() for a in into.action_items}
    for a in extra.action_items:
        if a.task.strip() and a.task.strip().lower() not in have_actions:
            into.action_items.append(a)
            have_actions.add(a.task.strip().lower())
            added.append(f"{a.task.strip()} ({a.owner})" if a.owner else a.task.strip())

    new_decisions = _new_strings(into.decisions, extra.decisions)
    into.decisions.extend(new_decisions)
    added.extend(new_decisions)

    new_follow_ups = _new_strings(into.follow_ups, extra.follow_ups)
    into.follow_ups.extend(new_follow_ups)
    added.extend(new_follow_ups)

    return added


def _new_strings(existing: list[str], candidates: list[str]) -> list[str]:
    have = {s.strip().lower() for s in existing}
    out: list[str] = []
    for s in candidates:
        if s.strip() and s.strip().lower() not in have:
            out.append(s)
            have.add(s.strip().lower())
    return out
