"""Thin wrapper around faster-whisper with a lazily-loaded model."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from eclipse.log import get_logger
from eclipse.models import TranscriptResult

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = get_logger("transcribe")


class Transcriber:
    """Loads the Whisper model on first use and reuses it for the session."""

    def __init__(
        self,
        model: str = "small.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = "en",
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
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
        segments, info = model.transcribe(
            str(audio_path),
            language=self.language,
            vad_filter=True,
            beam_size=1,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        result = TranscriptResult(
            text=text,
            language=getattr(info, "language", self.language),
            duration_sec=float(getattr(info, "duration", 0.0)),
        )
        log.info("transcribed", file=audio_path.name, chars=len(text), seconds=result.duration_sec)
        return result

    def descriptor(self) -> str:
        return f"faster-whisper/{self.model_name}"
