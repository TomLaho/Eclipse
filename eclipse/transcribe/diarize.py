"""Speaker diarization (Phase 4, optional).

pyannote.audio and torch are OPTIONAL heavy dependencies (``[diarization]`` extra).
This module MUST import cleanly without them installed — all heavy imports are lazy,
inside functions only.  If anything in the diarization path fails the function logs a
warning and returns, leaving ``segment.speaker`` as ``None``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eclipse.log import get_logger
from eclipse.models import Segment, TranscriptResult

log = get_logger("diarize")


# ---------------------------------------------------------------------------
# Pure, testable core — no ML imports
# ---------------------------------------------------------------------------


def assign_speakers(segments: list[Segment], turns: list[tuple[float, float, str]]) -> None:
    """Assign the best-matching speaker label to each segment, in place.

    For each segment the speaker is the turn whose time interval has the greatest
    temporal overlap with ``[segment.start, segment.end]``.  If a segment overlaps
    no turn at all its ``speaker`` is left as ``None``.

    Args:
        segments: List of transcript segments to label.  Mutated in place.
        turns: Speaker-diarization turns as ``(start, end, label)`` triples.
    """
    for seg in segments:
        best_label: str | None = None
        best_overlap: float = 0.0
        for t_start, t_end, label in turns:
            overlap = min(seg.end, t_end) - max(seg.start, t_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = label
        seg.speaker = best_label


# ---------------------------------------------------------------------------
# Pipeline entry-point (heavy path — lazy imports, fully degradable)
# ---------------------------------------------------------------------------


def diarize_into(path: Path, transcript: TranscriptResult, model: str) -> None:
    """Assign a speaker label to each transcript segment, in place.

    Gracefully degrades to a no-op (with a logged warning) when:
    - ``pyannote.audio`` / ``torch`` are not installed, or
    - No HuggingFace token is available, or
    - Any runtime error occurs during the diarization call.

    Args:
        path: Path to the audio file to diarize.
        transcript: The transcript whose segments will be labelled.
        model: HuggingFace model ID, e.g. ``"pyannote/speaker-diarization-3.1"``.
    """
    # 1. Lazy-import the heavy ML stack.
    # pyannote.* / torch.* have ignore_missing_imports=true in pyproject.toml, so mypy
    # treats them as Any when not installed — no type: ignore needed on these lines.
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError:
        log.warning(
            "diarization_skipped",
            reason=(
                "pyannote.audio / torch not installed; install with: uv sync --extra diarization"
            ),
        )
        return

    # 2. Retrieve the HuggingFace token — never print it.
    from eclipse.secrets import load_secrets

    secrets = load_secrets()
    if secrets.hf_token is None:
        log.warning(
            "diarization_skipped",
            reason="HF_TOKEN not set in .env; required to download the pyannote model",
        )
        return

    # 3. Run pyannote diarization and convert to (start, end, label) turns.
    try:
        log.info("diarizing", file=path.name, model=model)
        pipeline: Any = Pipeline.from_pretrained(model, use_auth_token=secrets.hf_token)
        # Move to CPU explicitly — this box has no GPU.
        pipeline.to(torch.device("cpu"))
        diarization: Any = pipeline(str(path))

        turns: list[tuple[float, float, str]] = [
            (turn.start, turn.end, speaker)
            for turn, _, speaker in diarization.itertracks(yield_label=True)
        ]
        log.info("diarization_done", file=path.name, turns=len(turns))

        # 4. Assign speakers to transcript segments.
        assign_speakers(transcript.segments, turns)
        labelled = sum(1 for s in transcript.segments if s.speaker is not None)
        log.info(
            "speakers_assigned",
            file=path.name,
            labelled=labelled,
            total=len(transcript.segments),
        )

    except Exception as exc:  # broad catch is intentional: degrade gracefully
        log.warning("diarization_failed", file=path.name, error=str(exc))
