"""Ollama-backed enrichment with structured output, retry, and a safe fallback."""

from __future__ import annotations

import json
import re
from datetime import date

import httpx
from pydantic import ValidationError

from eclipse.enrich.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from eclipse.log import get_logger
from eclipse.models import MeetingInsights

log = get_logger("enrich")

# Keep the prompt within a small local model's context window on a low-RAM machine.
_TRANSCRIPT_BUDGET = 9000


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

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

        prompt = USER_TEMPLATE.format(
            source_name=source_name,
            meeting_date=meeting_date.isoformat(),
            transcript=_trim(transcript),
        )

        for attempt in (1, 2):
            try:
                content = self._call(prompt)
                return MeetingInsights.model_validate_json(content), True
            except (httpx.HTTPError, OSError) as exc:
                log.warning("ollama_unreachable", error=str(exc))
                break  # server down: don't retry, fall back
            except (json.JSONDecodeError, ValidationError) as exc:
                log.warning("ollama_bad_json", attempt=attempt, error=str(exc))
                continue

        log.warning("enrichment_fallback", source=source_name)
        return self._fallback(transcript, source_name), False

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

    def _call(self, prompt: str) -> str:
        # format="json" (not a full schema grammar): far faster on CPU and still
        # reliably parseable; we validate the shape with pydantic + retry.
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": "10m",
            "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 800},
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
