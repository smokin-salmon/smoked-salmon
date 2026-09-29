"""FLAC tag warnings (an ID3 tag inside a FLAC, an uncompressed FLAC) are printed but never
block the upload."""

import anyio
import asyncclick as click

import salmon.uploader as uploader


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


async def _edit_metadata(path: str):
    metadata = {"scene": False, "genres": []}
    return await uploader.edit_metadata(path, {}, metadata, None, "WEB", {}, False, False, None, True)


def test_warnings_are_printed_but_the_upload_goes_on(monkeypatch, tmp_path, capsys) -> None:
    raw = 44100 * 16 * 2
    (tmp_path / "01.flac").write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 300)
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": raw}}
    _edit_metadata_stubs(monkeypatch, audio_info)
    monkeypatch.setattr(uploader.cfg.upload, "yes_all", True)

    path, metadata, tags, returned_info = anyio.run(_edit_metadata, str(tmp_path))

    assert path == str(tmp_path)
    out = click.unstyle(capsys.readouterr().out)
    assert "FLAC file contains an ID3 tag" in out
    assert "FLAC file looks uncompressed" in out
    assert "RED and OPS" in out


def test_a_clean_release_prints_no_tag_warnings(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "01.flac").write_bytes(b"fLaC" + b"\x00" * 300)
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": 900_000}}
    _edit_metadata_stubs(monkeypatch, audio_info)
    monkeypatch.setattr(uploader.cfg.upload, "yes_all", True)

    anyio.run(_edit_metadata, str(tmp_path))

    out = click.unstyle(capsys.readouterr().out)
    assert "Tag warnings" not in out
