"""Helpers of tests/test_library_dirs.py that other test modules use too."""

import hashlib
import os
import struct
from pathlib import Path
from typing import Any

import anyio
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

import salmon.checks
import salmon.commands
import salmon.converter
import salmon.tagger
import salmon.tagger.foldername
import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.trackers.base import TagRules

RENAMED = "Artist - Album (2020) [WEB FLAC]"


def _write_flac(path: Path, **tags: str) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


def _album(folder: Path) -> Path:
    """A release that every step of an upload would change: an alias tag, and an oversized embedded picture."""
    (folder / "CD1").mkdir(parents=True)
    # "year" is an alias standardize_tags rewrites to "date", in place.
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist", year="2020")
    _write_flac(folder / "CD1" / "02 - two.flac", title="Two", artist="Artist", year="2020")
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = "image/jpeg"
    picture.data = b"x" * (1024 * 1024)
    tagged = FLAC(folder / "01 - one.flac")
    tagged.add_picture(picture)
    tagged.save()
    # A readable image: embedded pictures are stripped only when the folder's cover is one.
    Image.new("RGB", (1, 1)).save(folder / "cover.jpg", "JPEG")
    # Old mtimes, so a rewrite shows even within the filesystem's timestamp resolution.
    for entry in folder.rglob("*"):
        os.utime(entry, ns=(1_000_000_000, 1_000_000_000))
    return folder


def _snapshot(folder: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry under folder: a hash of its bytes (or "dir"), its mtime and its inode, by relative path."""
    return {
        str(entry.relative_to(folder)): (
            hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "dir",
            entry.stat().st_mtime_ns,
            entry.stat().st_ino,
        )
        for entry in sorted(folder.rglob("*"))
    }


def _inodes(folder: Path) -> set[int]:
    return {entry.stat().st_ino for entry in folder.rglob("*") if entry.is_file()}


class FakeSite:
    site_code = "RED"
    site_string = "RED"
    base_url = "https://tracker.test"
    TAG_RULES = TagRules()


def _returning(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _returning_async(result: Any = None):
    async def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


async def _retag(path: str, *_args: Any, **_kwargs: Any) -> None:
    """Stands in for tag_files: rewrites a tag in every FLAC of the folder it is given."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            if name.endswith(".flac"):
                tagged = FLAC(os.path.join(root, name))
                tagged["album"] = "Album"
                tagged.save()


async def _rename_files(path: str, *_args: Any, **_kwargs: Any) -> None:
    """Stands in for rename_files: renames the tracks of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            os.rename(os.path.join(path, name), os.path.join(path, name.replace(" - ", ". ")))


def _run_up(
    monkeypatch,
    album: Path,
    *,
    embed: bool | None = False,
    scene: bool = False,
    cover_url: str | None = None,
    **fakes: Any,
) -> tuple[Any, list[str], list[tuple[str, str]]]:
    """Run `salmon up ALBUM -g 5` with the real staging, tag standardizing, picture strip and folder rename.

    The seams that need audio tools, a network or a reviewer are stubbed, `fakes` replace more of them.
    Returns the result, the folder of every upload, and the (source, output) of every transcode.
    """
    uploads: list[str] = []
    transcodes: list[tuple[str, str]] = []

    async def upload_and_report(_site: Any, path: str, *_args: Any, **_kwargs: Any) -> tuple:
        uploads.append(path)
        return 21, 5, "/t.torrent", b"", "https://tracker.test/t"

    async def transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
        new_path = os.path.join(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
        os.makedirs(new_path)
        transcodes.append((path, new_path))
        return new_path

    class FakeUploadManager:
        async def execute_upload(self) -> None:
            pass

        def remove_local_copies(self, keep_because: str | None = None) -> None:
            pass

    rls_data = {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "CAT1",
    }
    metadata = {
        **rls_data,
        "source": "WEB",
        "year": 2020,
        "edition_title": None,
        "scene": False,
        "cover": cover_url,
        "genres": [],
    }
    for name, fake in {
        "confirm_group_upload": _returning_async({}),
        "gather_audio_info": _returning({}),
        "check_hybrid": _returning(False),
        "gather_tags": _returning({}),
        "construct_rls_data": _returning_async(rls_data),
        "mqa_test": _returning_async(),
        "check_spectrals": _returning_async((False, None)),
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "tag_files": _retag,
        "check_tags": _returning_async({}),
        "rename_files": _rename_files,
        "check_folder_structure": _returning_async(),
        "concat_track_data": _returning({"01. one.flac": {"sample rate": 44100}}),
        "resolve_cover_url": _returning_async((True, None)),
        "print_torrents": _returning_async({}),
        "recheck_edition": _same_group,
        "UploadManager": FakeUploadManager,
        "transcode_folder": transcode,
        "upload_and_report": upload_and_report,
        **fakes,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    # A config with yes_all on would answer the questions the run is meant to ask.
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(cfg.upload.requests, "check_requests", False)
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", False)
    if embed is not None:
        monkeypatch.setattr(cfg.image, "embed_missing_cover", embed)
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: FakeSite)

    async def run():
        return await CliRunner().invoke(
            salmon.uploader.up,
            [
                str(album),
                "-t",
                "RED",
                "-g",
                "5",
                "-s",
                "WEB",
                "-n",
                "-yyy",
                "--skip-integrity-check",
                "--skip-up",
                *(["--scene"] if scene else []),
            ],
            input="y\n",
        )

    return anyio.run(run), uploads, transcodes


def _metadata() -> dict[str, Any]:
    return {
        "scene": False,
        "artists": [("Artist", "main")],
        "title": "Album",
        "year": 2020,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
    }


def _run_tag(monkeypatch, album: Path) -> Any:
    """Run `salmon tag ALBUM` with the real staging, tag standardizing and folder rename, remove_source_dir on."""
    metadata = {**_metadata(), "cover": None}
    for name, fake in {
        "gather_tags": _returning({}),
        "gather_audio_info": _returning({}),
        "construct_rls_data": _returning_async({}),
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "tag_files": _retag,
        "download_cover_if_nonexistent": _returning_async(),
        "check_tags": _returning_async({}),
        "rename_files": _rename_files,
        "check_folder_structure": _returning_async(),
    }.items():
        monkeypatch.setattr(salmon.tagger, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    monkeypatch.setattr(cfg.upload, "yes_all", True)

    async def run():
        return await CliRunner().invoke(salmon.tagger.tag, [str(album), "-s", "WEB", "-n"])

    return anyio.run(run)


async def _same_group(_site, group_id, *_args):
    """Stands in for recheck_edition: the group picked or given is kept."""
    return group_id
