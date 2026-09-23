"""ptpimg.me has shut down and is no longer a valid image host (issue #443).

These tests build a config file on disk (mirroring how tests/conftest.py sets up the
shared test config) and load it through salmon.config._parse_config, so they exercise the
same path an end user's config goes through, without ever contacting a real host.
"""

from pathlib import Path

import pytest

from salmon.config import _parse_config

_DEFAULT_CONFIG = Path(__file__).parent.parent / "src" / "salmon" / "data" / "config.default.toml"


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _base_config(tmp_path: Path) -> str:
    music = tmp_path / "music"
    torrents = tmp_path / "torrents"
    music.mkdir()
    torrents.mkdir()
    text = _DEFAULT_CONFIG.read_text(encoding="utf-8")
    text = text.replace("download_directory = '.music'", f"download_directory = '{music.as_posix()}'")
    text = text.replace("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{torrents.as_posix()}'")
    return text


def test_shipped_config_validates_with_no_image_host_key_and_defaults_to_catbox(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _base_config(tmp_path))
    cfg = _parse_config(path)
    assert cfg.image.image_uploader == "catbox"
    assert cfg.image.cover_uploader == "catbox"
    assert cfg.image.specs_uploader == "catbox"


@pytest.mark.parametrize(
    "replacement",
    [
        'image_uploader = "ptpimg"',
        'cover_uploader = "ptpimg"',
        'specs_uploader = "ptpimg"',
    ],
)
def test_top_level_ptpimg_uploader_fails_with_a_clear_message(tmp_path: Path, replacement: str) -> None:
    field = replacement.split(" =")[0]
    text = _base_config(tmp_path).replace(f'{field} = "catbox"', replacement)
    path = _write_config(tmp_path, text)
    with pytest.raises(ValueError, match="ptpimg has shut down"):
        _parse_config(path)


def test_per_tracker_ptpimg_cover_uploader_fails_with_a_clear_message(tmp_path: Path) -> None:
    text = _base_config(tmp_path).replace(
        '# [image.red]\n# cover_uploader = "red"',
        '[image.red]\ncover_uploader = "ptpimg"',
    )
    path = _write_config(tmp_path, text)
    with pytest.raises(ValueError, match="ptpimg has shut down"):
        _parse_config(path)


def test_leftover_ptpimg_key_is_ignored_when_another_host_is_used(tmp_path: Path) -> None:
    text = _base_config(tmp_path) + "\nptpimg_key = 'leftover-key'\n"
    path = _write_config(tmp_path, text)
    cfg = _parse_config(path)
    assert cfg.image.image_uploader == "catbox"
