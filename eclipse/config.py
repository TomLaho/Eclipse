"""Configuration loaded from ``eclipse.toml`` (with ``ECLIPSE_`` env overrides)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

RetentionPolicy = Literal["keep", "archive", "delete"]


class Config(BaseSettings):
    """All runtime settings. Edit ``eclipse.toml`` to change these."""

    model_config = SettingsConfigDict(
        toml_file="eclipse.toml",
        env_prefix="ECLIPSE_",
        extra="ignore",
    )

    # --- paths ---
    inbox_dir: Path = Path("inbox")
    vault_dir: Path = Path("vault")
    archive_dir: Path = Path("archive")
    registry_path: Path = Path("data/eclipse.sqlite")
    # where retained audio is copied (relative to vault unless absolute)
    audio_subdir: str = "_audio"

    # --- transcription (faster-whisper, CPU) ---
    # quality > speed: medium.en + beam 5 markedly improves names, numbers, accents.
    whisper_model: str = "medium.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str | None = "en"
    whisper_beam_size: int = 5
    # seeds Whisper's decoder so proper nouns are spelled right (client names, jargon)
    glossary: list[str] = Field(default_factory=list)
    whisper_initial_prompt: str | None = None
    # word-level timings (needed for diarization); modest extra cost
    whisper_word_timestamps: bool = False

    # --- enrichment (Ollama, local LLM) ---
    enrich: bool = True
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout_sec: float = 600.0
    # second LLM pass that re-reads the transcript for missed commitments
    two_pass_extraction: bool = True

    # --- behaviour ---
    audio_retention: RetentionPolicy = "keep"
    me_aliases: list[str] = Field(default_factory=lambda: ["me", "I", "Tom"])
    include_transcript_in_note: bool = True

    # --- notifications (Phase 2: Telegram) ---
    telegram_enabled: bool = False
    telegram_on_process: bool = True  # push a summary as each meeting is filed

    # --- diarization (Phase 4: optional, heavy) ---
    diarize: bool = False
    diarize_model: str = "pyannote/speaker-diarization-3.1"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # precedence: explicit init args > env vars > eclipse.toml > defaults
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
        )

    @property
    def audio_dir(self) -> Path:
        sub = Path(self.audio_subdir)
        return sub if sub.is_absolute() else self.vault_dir / sub

    @property
    def effective_initial_prompt(self) -> str | None:
        """Combine the glossary and any explicit prompt into Whisper's seed text."""
        parts: list[str] = []
        if self.glossary:
            parts.append("Terms that may come up: " + ", ".join(self.glossary) + ".")
        if self.whisper_initial_prompt:
            parts.append(self.whisper_initial_prompt)
        return " ".join(parts) or None

    def resolve_paths(self) -> None:
        """Expand ``~`` and make all directory paths absolute."""
        self.inbox_dir = self.inbox_dir.expanduser().resolve()
        self.vault_dir = self.vault_dir.expanduser().resolve()
        self.archive_dir = self.archive_dir.expanduser().resolve()
        self.registry_path = self.registry_path.expanduser().resolve()

    def ensure_dirs(self) -> None:
        """Create the directories Eclipse needs to operate."""
        for d in (self.inbox_dir, self.vault_dir, self.archive_dir, self.audio_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)


def load_config(**overrides: object) -> Config:
    cfg = Config(**overrides)  # type: ignore[arg-type]
    cfg.resolve_paths()
    return cfg
