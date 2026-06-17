from pathlib import Path

import pytest

from eclipse.config import Config, load_config


def test_audio_dir_is_under_vault_by_default() -> None:
    c = Config(vault_dir=Path("V"), audio_subdir="_audio")
    assert c.audio_dir == Path("V") / "_audio"


def test_toml_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "eclipse.toml").write_text(
        'whisper_model = "medium.en"\naudio_retention = "delete"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    c = load_config()
    assert c.whisper_model == "medium.en"
    assert c.audio_retention == "delete"


def test_init_args_beat_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "eclipse.toml").write_text('whisper_model = "medium.en"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    c = load_config(whisper_model="tiny.en")
    assert c.whisper_model == "tiny.en"
