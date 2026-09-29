"""FLAC tag issues (an ID3 tag stripped, an uncompressed FLAC warned about) are handled but never
block the upload."""

import struct

import anyio
import asyncclick as click
from mutagen.flac import FLAC

import salmon.uploader as uploader


def _write_flac(path, *, title: str | None = None) -> None:
    """A minimal but mutagen-readable FLAC: a STREAMINFO block, optionally tagged."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    if title is not None:
        tagged = FLAC(str(path))
        tagged["title"] = title
        tagged.save()


def _prepend_id3v2_header(path) -> None:
    header = b"ID3" + bytes([3, 0, 0]) + (0).to_bytes(4, "big")
    path.write_bytes(header + path.read_bytes())


def _edit_metadata_stubs(monkeypatch, audio_info: dict) -> None:
    async def returns(value=None):
        return value

    monkeypatch.setattr(uploader, "review_metadata_with_ai", lambda metadata, *a, **k: returns(metadata))
    monkeypatch.setattr(uploader, "tag_files", lambda *a, **k: None)
    monkeypatch.setattr(uploader, "check_tags", lambda *a, **k: returns({}))
    monkeypatch.setattr(uploader, "rename_folder", lambda path, *a, **k: path)
    monkeypatch.setattr(uploader, "rename_files", lambda *a, **k: None)
    monkeypatch.setattr(uploader, "check_folder_structure", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "gather_tags", lambda *a, **k: {})
    monkeypatch.setattr(uploader, "gather_audio_info", lambda *a, **k: audio_info)


async def _edit_metadata(path: str, *, scene: bool = False, recompress: bool = False):
    metadata = {"scene": scene, "genres": []}
    return await uploader.edit_metadata(path, {}, metadata, None, "WEB", {}, recompress, False, None, True)


def test_id3_is_stripped_and_uncompressed_is_warned_and_the_upload_goes_on(monkeypatch, tmp_path, capsys) -> None:
    raw = 44100 * 16 * 2
    path = tmp_path / "01.flac"
    _write_flac(path, title="Hello")
    _prepend_id3v2_header(path)
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": raw}}
    _edit_metadata_stubs(monkeypatch, audio_info)
    monkeypatch.setattr(uploader.cfg.upload, "yes_all", True)

    result_path, metadata, tags, returned_info = anyio.run(lambda: _edit_metadata(str(tmp_path)))

    assert result_path == str(tmp_path)
    out = click.unstyle(capsys.readouterr().out)
    assert "Removed an ID3 tag from 01.flac" in out
    assert "FLAC file looks uncompressed" in out
    assert "RED and OPS" in out
    assert FLAC(str(path))["title"] == ["Hello"], "stripping must not touch the rest of the tags"


def test_a_scene_release_is_only_warned_about_and_not_modified(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "01.flac"
    _write_flac(path, title="Hello")
    _prepend_id3v2_header(path)
    before = path.read_bytes()
    _edit_metadata_stubs(monkeypatch, {})
    monkeypatch.setattr(uploader.cfg.upload, "yes_all", True)

    anyio.run(lambda: _edit_metadata(str(tmp_path), scene=True))

    assert path.read_bytes() == before
    out = click.unstyle(capsys.readouterr().out)
    assert "FLAC file contains an ID3 tag" in out
    assert "Removed" not in out


def test_a_scene_release_with_recompress_still_warns_about_an_uncompressed_flac(monkeypatch, tmp_path, capsys) -> None:
    """recompress_path never runs for a scene release (it is guarded by `not scene`), so passing
    the raw -c flag into process_tag_issues would wrongly suppress the warning for a file that is
    never actually going to be recompressed."""
    raw = 44100 * 16 * 2
    path = tmp_path / "01.flac"
    _write_flac(path)
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": raw}}
    _edit_metadata_stubs(monkeypatch, audio_info)
    monkeypatch.setattr(uploader.cfg.upload, "yes_all", True)

    anyio.run(lambda: _edit_metadata(str(tmp_path), scene=True, recompress=True))

    out = click.unstyle(capsys.readouterr().out)
    assert "FLAC file looks uncompressed" in out


def test_a_clean_release_prints_no_tag_notes(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "01.flac"
    _write_flac(path)
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": 900_000}}
    _edit_metadata_stubs(monkeypatch, audio_info)
    monkeypatch.setattr(uploader.cfg.upload, "yes_all", True)

    anyio.run(lambda: _edit_metadata(str(tmp_path)))

    out = click.unstyle(capsys.readouterr().out)
    assert "Tag notes" not in out
