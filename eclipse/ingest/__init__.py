"""Ingest: detect new audio in the inbox, de-duplicate, hand off to the pipeline."""

AUDIO_EXTS = frozenset({".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".mp4", ".webm", ".opus"})
