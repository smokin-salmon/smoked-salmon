"""Helpers of tests/test_config_compat.py that other test modules use too."""

from pathlib import Path
from typing import Any

import msgspec
import pytest

from salmon.config import _parse_config
from salmon.config.validations import Cfg


def _base(tmp_path: Path) -> dict[str, Any]:
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir(exist_ok=True)
    torrents.mkdir(exist_ok=True)
    return {
        "directory": {"download_directory": music.as_posix(), "dottorrents_dir": torrents.as_posix()},
        "tracker": {"red": {"session": "a-real-cookie"}},
    }


def _load(tmp_path: Path, raw: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> tuple[Cfg, list[str]]:
    """Load ``raw`` as a config file; return it and the warning lines printed."""
    path = tmp_path / "config.toml"
    path.write_bytes(msgspec.toml.encode(raw))
    capsys.readouterr()
    cfg = _parse_config(path)
    return cfg, [line for line in capsys.readouterr().err.splitlines() if line.strip()]
