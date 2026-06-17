"""Domain models shared across the pipeline."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    """A single follow-up extracted from a meeting."""

    task: str
    owner: str | None = None
    due: str | None = None  # free-text date as spoken, e.g. "Friday", "20/06"

    def is_mine(self, me_aliases: list[str]) -> bool:
        if self.owner is None:
            return False
        owner = self.owner.strip().lower()
        return any(owner == a.strip().lower() for a in me_aliases)


class MeetingInsights(BaseModel):
    """Structured understanding of a meeting, produced by the local LLM."""

    title: str
    summary: str
    client: str = "General"
    attendees: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)


class TranscriptResult(BaseModel):
    """Raw output of the transcription step."""

    text: str
    language: str | None = None
    duration_sec: float = 0.0


class ProcessedMeeting(BaseModel):
    """Everything needed to write a vault note for one audio file."""

    source_name: str  # original audio filename
    file_hash: str
    meeting_date: date
    transcript: TranscriptResult
    insights: MeetingInsights
    audio_relpath: str | None = None  # path to retained audio, relative to vault
    transcribed_with: str = ""
    enriched_with: str = ""
    enriched: bool = True
