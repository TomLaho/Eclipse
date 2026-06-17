"""Thin wrapper around faster-whisper with a lazily-loaded model."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from eclipse.log import get_logger
from eclipse.models import Segment, TranscriptResult, Word

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = get_logger("transcribe")


class Transcriber:
    """Loads the Whisper model on first use and reuses it for the session."""

    def __init__(
        self,
        model: str = "medium.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = "en",
        beam_size: int = 5,
        initial_prompt: str | None = None,
        word_timestamps: bool = False,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt
        self.word_timestamps = word_timestamps
        self._model: WhisperModel | None = None

    def _load(self) -> WhisperModel:
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading_whisper", model=self.model_name, device=self.device)
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        model = self._load()
        log.info("transcribing", file=audio_path.name)
        raw_segments, info = model.transcribe(
            str(audio_path),
            language=self.language,
            vad_filter=True,
            beam_size=self.beam_size,
            initial_prompt=self.initial_prompt,
            word_timestamps=self.word_timestamps,
        )

        segments: list[Segment] = []
        for seg in raw_segments:  # generator: consume once, building text + timings
            words = [
                Word(start=float(w.start), end=float(w.end), word=w.word)
                for w in (getattr(seg, "words", None) or [])
            ]
            segments.append(
                Segment(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=seg.text.strip(),
                    words=words,
                )
            )

        text = " ".join(s.text for s in segments).strip()
        result = TranscriptResult(
            text=text,
            language=getattr(info, "language", self.language),
            duration_sec=float(getattr(info, "duration", 0.0)),
            segments=segments,
        )
        log.info("transcribed", file=audio_path.name, chars=len(text), seconds=result.duration_sec)
        return result

    def unload(self) -> None:
        """Release the Whisper model to free RAM (Phase-1b two-stage batch)."""
        if self._model is not None:
            log.info("unloading_whisper", model=self.model_name)
            self._model = None
            import gc

            gc.collect()

    def descriptor(self) -> str:
        return f"faster-whisper/{self.model_name}"
