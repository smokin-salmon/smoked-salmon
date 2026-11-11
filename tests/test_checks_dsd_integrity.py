"""The integrity check of DSD files (#919): their headers against their size, DSD holding no checksum. A damaged one
stops the upload, and is never offered to sanitizing, which re-encodes only FLAC and MP3 files."""

import importlib

import anyio
import asyncclick as click
import pytest
from helpers.dsd import write_dff, write_dsf
from test_folderstructure_yes_all import _NoQuestions

from salmon import interaction
from salmon.checks.dsd import dsd_damage
from salmon.checks.integrity import check_integrity, handle_integrity_check, resolve_integrity_for_upload

# Not `from salmon.checks import integrity`: that is the `salmon check integrity` command, which hides the module.
integrity = importlib.import_module("salmon.checks.integrity")

TAGS = {"TIT2": "One"}


def _cut(path, keep: int):
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - keep] if keep > 0 else data[:-keep])
    return path


@pytest.mark.parametrize("write", [write_dsf, write_dff], ids=["dsf", "dff"])
@pytest.mark.parametrize("tags", [None, TAGS], ids=["untagged", "tagged"])
def test_a_whole_dsd_file_passes(tmp_path, write, tags) -> None:
    assert dsd_damage(str(write(tmp_path / f"01.{write.__name__[-3:]}", tags=tags))) is None


@pytest.mark.parametrize(
    ("write", "cut", "found"),
    [
        # A download that stopped in the samples, in the tag at the end, or in the headers.
        (write_dsf, 5000, "it is cut short"),
        (write_dsf, 10, "it is cut short"),
        (write_dsf, -40, "it is cut short"),
        (write_dff, 5000, "it is cut short"),
        (write_dff, 10, "it is cut short"),
        (write_dff, -30, "it is cut short"),
    ],
    ids=["dsf samples", "dsf tag", "dsf header", "dff samples", "dff tag", "dff header"],
)
def test_a_cut_dsd_file_is_damaged(tmp_path, write, cut: int, found: str) -> None:
    # A positive cut takes that many bytes off the end; a negative one keeps that many from the start.
    path = _cut(write(tmp_path / f"01.{write.__name__[-3:]}", tags=TAGS), cut)
    assert found in (dsd_damage(str(path)) or "")


def test_a_file_named_dsf_that_is_none_is_damaged(tmp_path) -> None:
    path = tmp_path / "01.dsf"
    path.write_bytes(b"fLaC" + bytes(100))
    assert dsd_damage(str(path)) == "it is neither a DSF nor a DSDIFF file"


def test_a_folder_of_dsd_files_is_checked_and_a_damaged_one_does_not_decode(tmp_path) -> None:
    write_dsf(tmp_path / "01.dsf", tags=TAGS)
    write_dff(tmp_path / "02.dff", tags=TAGS)
    assert anyio.run(check_integrity, str(tmp_path)) == integrity.IntegrityResult(True, checked=2)

    _cut(tmp_path / "02.dff", 5000)
    result = anyio.run(check_integrity, str(tmp_path))

    assert not result.passed
    assert result.decode_failures == ("02.dff",)
    assert result.details.startswith("02.dff: it is cut short")


def test_an_upload_stops_on_a_damaged_dsd_file_without_offering_to_sanitize(monkeypatch, tmp_path, capsys) -> None:
    write_dsf(tmp_path / "01.dsf", tags=TAGS)
    _cut(write_dsf(tmp_path / "02.dsf", tags=TAGS), 5000)

    async def sanitize(*_args, **_kwargs):
        raise AssertionError("sanitized")

    monkeypatch.setattr(integrity, "sanitize_and_verify", sanitize)
    with interaction.using(_NoQuestions()), pytest.raises(click.Abort):
        anyio.run(lambda: resolve_integrity_for_upload(str(tmp_path), scene=False, assume_yes=True))
    assert "it cannot repair a damaged DSD file (02.dsf). Aborting." in capsys.readouterr().out


def test_check_integrity_of_one_dsd_file_offers_no_sanitizing(tmp_path, capsys) -> None:
    path = _cut(write_dsf(tmp_path / "01.dsf", tags=TAGS), 5000)

    with interaction.using(_NoQuestions()):
        anyio.run(handle_integrity_check, str(path))
    assert "01.dsf: it is cut short" in capsys.readouterr().out


def test_sanitizing_leaves_dsd_files_alone(monkeypatch, tmp_path) -> None:
    write_dsf(tmp_path / "01.dsf", tags=TAGS)
    sanitized: list[str] = []

    async def sanitize_mp3(path, *_args):
        sanitized.append(path)
        return True

    monkeypatch.setattr(integrity, "_sanitize_mp3", sanitize_mp3)
    assert anyio.run(integrity.sanitize_integrity, str(tmp_path)) is True
    assert sanitized == []
