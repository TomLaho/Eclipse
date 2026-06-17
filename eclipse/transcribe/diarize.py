"""Speaker diarization (Phase 4, optional).

Contract stub — the pipeline calls ``diarize_into`` after transcription when
``diarize`` is enabled, to label ``transcript.segments[*].speaker`` in place.
The full implementation (lazy pyannote import, HF token, graceful degradation)
is built in the Phase-4 pass and lives behind the ``diarization`` extra.
"""

from __future__ import annotations

from pathlib import Path

from eclipse.models import TranscriptResult


def diarize_into(path: Path, transcript: TranscriptResult, model: str) -> None:
    """Assign a speaker label to each transcript segment, in place."""
    raise NotImplementedError
