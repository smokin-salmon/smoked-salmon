"""The optional {resolution} folder token: bit depth and sample rate, only when asked for."""

import pytest

from salmon import cfg
from salmon.tagger import foldername

METADATA = {
    "scene": False,
    "artists": [("Illy", "main")],
    "title": "journaling",
    "year": 2022,
    "source": "WEB",
    "format": "FLAC",
    "encoding": "24bit Lossless",
    "encoding_vbr": False,
}


def _stub_audio_info(monkeypatch, tracks):
    """Replace gather_audio_info with one that returns `tracks` and records whether it was called."""
    calls = []

    def fake_gather(path, sort_by_tracknumber=False):
        calls.append(path)
        return tracks

    monkeypatch.setattr(foldername, "gather_audio_info", fake_gather)
    return calls


@pytest.mark.parametrize(
    ("precision", "sample_rate", "expected"),
    [
        (24, 96000, "24-96"),
        (24, 44100, "24-44.1"),
        (16, 48000, "16-48"),
        (16, 44100, ""),
        (None, 44100, ""),
        (0, 44100, ""),
        (24, None, ""),
    ],
)
def test_resolution_of_a_uniform_release(monkeypatch, precision, sample_rate, expected) -> None:
    _stub_audio_info(monkeypatch, {"01.flac": {"precision": precision, "sample rate": sample_rate}})

    assert foldername._resolution("/music/album") == expected


def test_mixed_sample_rates_are_blank(monkeypatch) -> None:
    # A folder name must not claim a single resolution for a hybrid release.
    _stub_audio_info(
        monkeypatch,
        {
            "01.flac": {"precision": 24, "sample rate": 96000},
            "02.flac": {"precision": 24, "sample rate": 48000},
        },
    )

    assert foldername._resolution("/music/album") == ""


def test_mixed_bit_depths_are_blank(monkeypatch) -> None:
    _stub_audio_info(
        monkeypatch,
        {
            "01.flac": {"precision": 24, "sample rate": 96000},
            "02.flac": {"precision": 16, "sample rate": 96000},
        },
    )

    assert foldername._resolution("/music/album") == ""


WITH_TOKEN = "{artists} - {title} ({year}) [{source} FLAC {resolution}]"
DEFAULT_TEMPLATE = "{artists} - {title} ({year}) [{source} {format}]"
ESCAPED_TOKEN = "{artists} - {title} [{{resolution}}]"


def test_the_token_lands_in_the_folder_name_when_the_template_uses_it(monkeypatch, tmp_path) -> None:
    calls = _stub_audio_info(monkeypatch, {"01.flac": {"precision": 24, "sample rate": 96000}})
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", WITH_TOKEN)
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    renamed = foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB FLAC 24-96]")
    assert calls == [str(album)]


def test_a_blank_token_strips_cleanly(monkeypatch, tmp_path) -> None:
    # 16/44.1 renders no resolution, and the bracket around it disappears too.
    calls = _stub_audio_info(monkeypatch, {"01.flac": {"precision": 16, "sample rate": 44100}})
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", WITH_TOKEN)
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    renamed = foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB FLAC]")
    assert calls == [str(album)]


def test_the_default_template_is_unchanged_and_does_not_read_files(monkeypatch, tmp_path) -> None:
    calls = _stub_audio_info(monkeypatch, {"01.flac": {"precision": 24, "sample rate": 96000}})
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", DEFAULT_TEMPLATE)
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    renamed = foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB 24bit FLAC]")
    assert calls == []


def test_an_escaped_token_does_not_read_the_files(monkeypatch, tmp_path) -> None:
    # "{{resolution}}" is a literal "{resolution}" to str.format, not the token.
    calls = _stub_audio_info(monkeypatch, {"01.flac": {"precision": 24, "sample rate": 96000}})
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", ESCAPED_TOKEN)
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    renamed = foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling [{resolution}]")
    assert calls == []
