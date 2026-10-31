"""Sanitizing is checked again afterwards, and the upload stops on a file that still does not decode."""

import asyncio
import io
import os
import subprocess
from importlib import import_module
from pathlib import Path

import anyio
import asyncclick as click
import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image
from test_uploader_shortened_conversions import _write_flac  # pyright: ignore[reportMissingImports]

import salmon.uploader as uploader
from salmon import cfg
from salmon.errors import UploadError
from salmon.tagger import cover as cover_module

# salmon.checks.__init__ defines a click command also named `integrity`, which shadows the
# submodule on the package object, so importlib is used to get the module itself.
integrity = import_module("salmon.checks.integrity")

# flac 1.5.0's real output for each state a file can be in.
FLAC_OUTPUT = {
    "clean": b"%s: ok                    \n",
    "md5_unset": b"%s: WARNING, cannot check MD5 signature since it was unset in the STREAMINFO\n"
    b"ok                    \n",
    "truncated": b"%s: *** Got error code 0:FLAC__STREAM_DECODER_ERROR_STATUS_LOST_SYNC after processing 77824 "
    b"samples\n\n\n%s: ERROR during decoding\n        state = FLAC__STREAM_DECODER_END_OF_STREAM\n",
}


class FakeFlac:
    """flac and metaflac for files whose state is given by name, on real files in a folder.

    Re-encoding sets the MD5 of a file that decodes. A truncated file fails to re-encode, and flac deletes
    the output it started, as the real one does.
    """

    def __init__(self, folder, states: dict[str, str]) -> None:
        self.folder = folder
        self.states = dict(states)
        self.tested: list[str] = []
        self.metaflac_fails: set[str] = set()
        for name in states:
            (folder / name).write_bytes(b"fLaC " + name.encode())

    async def run_process(self, commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if commands[0] == "metaflac":
            failed = os.path.basename(commands[-1]) in self.metaflac_fails
            return subprocess.CompletedProcess(commands, int(failed), b"", b"")
        if commands[:2] == ["flac", "-wt"]:
            name = os.path.basename(commands[2])
            self.tested.append(name)
            output = FLAC_OUTPUT[self.states[name]]
            stderr = output % ((name.encode(),) * output.count(b"%s"))
            return subprocess.CompletedProcess(commands, int(self.states[name] != "clean"), b"", stderr)
        # flac -<level> <file>.corrupted -o <file>
        source, output_path = commands[2], commands[4]
        name = os.path.basename(output_path)
        assert source == output_path + ".corrupted"
        if self.states[name] == "truncated":
            return subprocess.CompletedProcess(commands, 1, b"", b"ERROR while decoding FLAC input\n")
        with open(output_path, "wb") as f:
            f.write(b"fLaC re-encoded")
        self.states[name] = "clean"
        return subprocess.CompletedProcess(commands, 0, b"", b"")


@pytest.fixture
def fake_flac(monkeypatch, tmp_path):
    def setup(states: dict[str, str]) -> FakeFlac:
        fake = FakeFlac(tmp_path, states)
        monkeypatch.setattr(integrity.anyio, "run_process", fake.run_process)
        return fake

    return setup


def test_check_command_checks_again_after_sanitizing(fake_flac, monkeypatch, tmp_path, capsys) -> None:
    """A sanitize that leaves one file bad is caught, and the file it could not re-encode is kept."""
    fake = fake_flac({"01.flac": "md5_unset", "02.flac": "truncated"})
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)

    anyio.run(integrity.handle_integrity_check, str(tmp_path))

    assert sorted(os.listdir(tmp_path)) == ["01.flac", "02.flac"]
    assert (tmp_path / "02.flac").read_bytes() == b"fLaC 02.flac", "the original must be put back"
    assert sorted(fake.tested) == ["01.flac", "01.flac", "02.flac", "02.flac"], "every file is checked again"
    assert "Sanitization did not clear the integrity check." in click.unstyle(capsys.readouterr().out)


def test_a_failed_metadata_cleanup_puts_the_original_back(fake_flac, tmp_path) -> None:
    """The re-encode succeeded but metaflac did not: the original is restored, not left half-sanitized."""
    fake = fake_flac({"01.flac": "md5_unset"})
    fake.metaflac_fails.add("01.flac")

    sanitized = anyio.run(integrity.sanitize_integrity, str(tmp_path / "01.flac"))

    assert sanitized is False

    assert sorted(os.listdir(tmp_path)) == ["01.flac"]
    assert (tmp_path / "01.flac").read_bytes() == b"fLaC 01.flac"


def _edit_metadata_stubs(monkeypatch, scene: bool = False) -> None:
    async def returns(value=None):
        return value

    monkeypatch.setattr(uploader, "review_metadata_with_ai", lambda metadata, *a, **k: returns(metadata))
    monkeypatch.setattr(uploader, "tag_files", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "check_tags", lambda *a, **k: returns({}))
    monkeypatch.setattr(uploader, "rename_folder", lambda path, *a, **k: returns(path))
    monkeypatch.setattr(uploader, "rename_files", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "check_folder_structure", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "gather_tags", lambda *a, **k: {})
    monkeypatch.setattr(uploader, "gather_audio_info", lambda *a, **k: {})


async def _edit_metadata(path: str, scene: bool = False):
    metadata = {"scene": scene, "genres": []}
    return await uploader.edit_metadata(path, {}, metadata, None, "WEB", {}, False, False, None)


def test_upload_stops_when_sanitizing_leaves_a_file_that_does_not_decode(fake_flac, monkeypatch, tmp_path) -> None:
    """yes_all sanitizes without asking, and used to go on to the upload whatever the sanitize left."""
    fake_flac({"01.flac": "md5_unset", "02.flac": "truncated"})
    _edit_metadata_stubs(monkeypatch)
    monkeypatch.setattr(cfg.upload, "yes_all", True)

    with pytest.raises(click.Abort):
        anyio.run(_edit_metadata, str(tmp_path))


def test_upload_goes_on_once_sanitizing_sets_the_md5(fake_flac, monkeypatch, tmp_path) -> None:
    fake = fake_flac({"01.flac": "md5_unset", "02.flac": "clean"})
    _edit_metadata_stubs(monkeypatch)
    monkeypatch.setattr(cfg.upload, "yes_all", True)

    anyio.run(_edit_metadata, str(tmp_path))

    assert fake.states == {"01.flac": "clean", "02.flac": "clean"}
    assert sorted(fake.tested) == ["01.flac", "01.flac", "02.flac", "02.flac"]


def test_an_unset_md5_can_be_acknowledged(fake_flac, monkeypatch, tmp_path) -> None:
    """The audio decodes: declining to sanitize goes on to the upload with the files as they are."""
    from salmon.checks.integrity import resolve_integrity_for_upload

    fake = fake_flac({"01.flac": "md5_unset", "02.flac": "clean"})
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)

    result = anyio.run(lambda: resolve_integrity_for_upload(str(tmp_path), scene=False, assume_yes=False))

    assert result.md5_unset == ("01.flac",)
    assert fake.states["01.flac"] == "md5_unset"


def test_declining_to_sanitize_a_file_that_does_not_decode_stops_the_upload(fake_flac, monkeypatch, tmp_path) -> None:
    from salmon.checks.integrity import resolve_integrity_for_upload

    fake = fake_flac({"01.flac": "md5_unset", "02.flac": "truncated"})
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)

    with pytest.raises(click.Abort):
        anyio.run(lambda: resolve_integrity_for_upload(str(tmp_path), scene=False, assume_yes=False))

    assert fake.states["01.flac"] == "md5_unset", "nothing is re-encoded when the user declines"


def test_a_scene_release_stops_before_the_sanitize_offer(fake_flac, monkeypatch, tmp_path, capsys) -> None:
    from salmon.checks.integrity import resolve_integrity_for_upload

    fake = fake_flac({"01.flac": "md5_unset"})
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: pytest.fail("no sanitize offer for a scene release"))

    with pytest.raises(click.Abort):
        anyio.run(lambda: resolve_integrity_for_upload(str(tmp_path), scene=True, assume_yes=True))

    assert fake.states["01.flac"] == "md5_unset", "nothing was sanitized"
    output = click.unstyle(capsys.readouterr().out)
    assert "Some files failed the integrity check, and this is a scene release." in output
    assert "failed sanitization" not in output


def test_sanitize_and_verify_reports_the_recheck_not_the_sanitize_return(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_sanitize(path: str, _: int | None = None) -> bool:
        calls.append("sanitize")
        return True

    async def fake_check(path: str, _: int | None = None):
        calls.append("check")
        return integrity.IntegrityResult(False, md5_unset=("01.flac",), checked=1)

    monkeypatch.setattr(integrity, "sanitize_integrity", fake_sanitize)
    monkeypatch.setattr(integrity, "check_integrity", fake_check)

    result = anyio.run(integrity.sanitize_and_verify, "/music")

    assert calls == ["sanitize", "check"]
    assert not result.passed


# What sanitizing must not lose: the artwork the re-encode drops, and the original if it stops partway


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buffer, "png")
    return buffer.getvalue()


def _flac_with_front_cover(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_flac(path, 44100, 16)
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = "image/png"
    picture.data = data
    audio = FLAC(path)
    audio.add_picture(picture)
    audio.save()


async def _encodes(commands: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
    """flac and metaflac succeeding; the re-encode writes its output, without the pictures."""
    if commands[0] == "flac":
        Path(commands[-1]).write_bytes(b"fLaC re-encoded")
    return subprocess.CompletedProcess(commands, 0, b"", b"")


def test_sanitizing_a_folder_keeps_its_only_artwork_as_a_cover_file_in_the_album_folder(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Album"
    _flac_with_front_cover(album / "CD1" / "01.flac", _png())
    monkeypatch.setattr(integrity.anyio, "run_process", _encodes)

    sanitized = anyio.run(integrity.sanitize_integrity, str(album))

    assert sanitized is True

    assert (album / "cover.png").read_bytes() == _png()
    # Not one per disc folder: the album's files are sanitized one by one.
    assert sorted(os.listdir(album / "CD1")) == ["01.flac"]


def test_sanitizing_one_flac_keeps_its_artwork_beside_it(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Album"
    _flac_with_front_cover(album / "01.flac", _png())
    monkeypatch.setattr(integrity.anyio, "run_process", _encodes)

    sanitized = anyio.run(integrity.sanitize_integrity, str(album / "01.flac"))

    assert sanitized is True

    assert (album / "cover.png").read_bytes() == _png()


def test_a_folder_with_a_cover_file_gets_no_other(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Album"
    _flac_with_front_cover(album / "01.flac", _png())
    (album / "folder.jpg").write_bytes(b"the album's own")
    monkeypatch.setattr(integrity.anyio, "run_process", _encodes)

    sanitized = anyio.run(integrity.sanitize_integrity, str(album))

    assert sanitized is True

    assert sorted(os.listdir(album)) == ["01.flac", "folder.jpg"]


def test_a_backup_left_by_a_crashed_run_is_never_overwritten(monkeypatch, tmp_path) -> None:
    flac = tmp_path / "01.flac"
    flac.write_bytes(b"fLaC damaged")
    # The only copy of the original, if that run stopped before putting it back.
    (tmp_path / "01.flac.corrupted").write_bytes(b"fLaC original")
    monkeypatch.setattr(integrity.anyio, "run_process", _encodes)

    sanitized = anyio.run(integrity.sanitize_integrity, str(flac))

    assert sanitized is True

    assert (tmp_path / "01.flac.corrupted").read_bytes() == b"fLaC original"
    assert sorted(os.listdir(tmp_path)) == ["01.flac", "01.flac.corrupted"]


def test_a_backup_name_is_claimed_once(tmp_path) -> None:
    flac = tmp_path / "01.flac"
    flac.write_bytes(b"fLaC")

    claimed = [integrity._reserve_backup_path(str(flac)) for _ in range(3)]

    assert claimed == [f"{flac}.corrupted", f"{flac}.corrupted.1", f"{flac}.corrupted.2"]
    assert all(os.path.exists(path) for path in claimed)


@pytest.mark.parametrize("name", ["01.flac", "01.mp3"])
def test_a_cancelled_sanitize_puts_the_original_back(monkeypatch, tmp_path, name: str) -> None:
    original = tmp_path / name
    original.write_bytes(b"the original")

    async def cancelled(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(integrity.anyio, "run_process", cancelled)
    sanitize = integrity._sanitize_flac if name.endswith(".flac") else integrity._sanitize_mp3

    with pytest.raises(asyncio.CancelledError):
        anyio.run(sanitize, str(original))

    assert sorted(os.listdir(tmp_path)) == [name]
    assert original.read_bytes() == b"the original"


def _mp3val(rewrite: bytes, then: BaseException | None = None, returncode: int = 0):
    """An mp3val that rewrites the file it is given as it would (its own backup, then the fixed file with the
    backup's times, as -t keeps them), then stops with `then` or returns `returncode`."""

    async def run(commands: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        target = Path(commands[-1])
        target.rename(f"{target}.bak")
        target.write_bytes(rewrite)
        kept = os.stat(f"{target}.bak")
        os.utime(target, ns=(kept.st_atime_ns, kept.st_mtime_ns))
        if then is not None:
            raise then
        if returncode == 0:
            Path(f"{target}.bak").unlink()
        return subprocess.CompletedProcess(commands, returncode, b"", b"no frames")

    return run


def test_an_mp3_cancelled_while_mp3val_rewrites_it_is_left_as_it_was(monkeypatch, tmp_path) -> None:
    original = tmp_path / "01.mp3"
    original.write_bytes(b"the original")
    monkeypatch.setattr(integrity.anyio, "run_process", _mp3val(b"half of a fix", then=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        anyio.run(integrity._sanitize_mp3, str(original))

    assert sorted(os.listdir(tmp_path)) == ["01.mp3"]
    assert original.read_bytes() == b"the original"


def test_a_fixed_mp3_replaces_the_original_and_keeps_its_times(monkeypatch, tmp_path) -> None:
    original = tmp_path / "01.mp3"
    original.write_bytes(b"the original")
    os.utime(original, (1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(integrity.anyio, "run_process", _mp3val(b"fixed"))

    sanitized = anyio.run(integrity._sanitize_mp3, str(original))

    assert sanitized is True

    assert sorted(os.listdir(tmp_path)) == ["01.mp3"]
    assert original.read_bytes() == b"fixed"
    assert original.stat().st_mtime == 1_000_000_000


def test_an_mp3_mp3val_fails_on_is_left_as_it_was(monkeypatch, tmp_path) -> None:
    original = tmp_path / "01.mp3"
    original.write_bytes(b"the original")
    monkeypatch.setattr(integrity.anyio, "run_process", _mp3val(b"broken", returncode=1))

    sanitized = anyio.run(integrity._sanitize_mp3, str(original))

    assert sanitized is False

    assert sorted(os.listdir(tmp_path)) == ["01.mp3"]
    assert original.read_bytes() == b"the original"


@pytest.mark.parametrize("given", ["file", "folder"])
def test_an_original_that_cannot_be_put_back_stops_the_sanitize_and_says_where_it_is(
    monkeypatch, tmp_path, given: str
) -> None:
    flac = tmp_path / "01.flac"
    flac.write_bytes(b"fLaC original")

    async def flac_fails(commands: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 1, b"", b"bad")

    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if str(src).endswith(".corrupted"):
            raise PermissionError("not allowed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(integrity.anyio, "run_process", flac_fails)
    monkeypatch.setattr(integrity.os, "replace", replace)

    # A folder's files are sanitized in a task group: the error still comes out bare, as run.main catches it.
    with pytest.raises(UploadError, match=r"the original is at .*01\.flac\.corrupted"):
        anyio.run(integrity.sanitize_integrity, str(flac if given == "file" else tmp_path))

    assert (tmp_path / "01.flac.corrupted").read_bytes() == b"fLaC original"


def test_a_flac_that_cannot_be_read_is_passed_over_for_the_cover(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"not a flac at all")
    _flac_with_front_cover(album / "02.flac", _png())
    monkeypatch.setattr(integrity.anyio, "run_process", _encodes)

    anyio.run(integrity.sanitize_integrity, str(album))

    assert (album / "cover.png").read_bytes() == _png()


def test_a_cancel_between_the_move_and_its_flag_still_puts_the_original_back(monkeypatch, tmp_path) -> None:
    flac = tmp_path / "01.flac"
    flac.write_bytes(b"fLaC original")
    real_replace = os.replace

    def moved_then_interrupted(src, dst, *args, **kwargs):
        real_replace(src, dst, *args, **kwargs)
        if str(dst).endswith(".corrupted"):
            raise KeyboardInterrupt

    monkeypatch.setattr(integrity.os, "replace", moved_then_interrupted)

    with pytest.raises(KeyboardInterrupt):
        anyio.run(integrity._sanitize_flac, str(flac))

    assert sorted(os.listdir(tmp_path)) == ["01.flac"]
    assert flac.read_bytes() == b"fLaC original"


def test_a_cover_that_cannot_be_saved_leaves_the_folder_unsanitized(monkeypatch, tmp_path, capsys) -> None:
    album = tmp_path / "Album"
    _flac_with_front_cover(album / "01.flac", _png())
    before = (album / "01.flac").read_bytes()
    ran: list[list[str]] = []

    async def recorded(commands: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        ran.append(commands)
        return subprocess.CompletedProcess(commands, 0, b"", b"")

    def disk_full(_path: str, _data: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(integrity.anyio, "run_process", recorded)
    monkeypatch.setattr(cover_module, "_write_whole_file", disk_full)

    sanitized = anyio.run(integrity.sanitize_integrity, str(album))

    assert sanitized is False
    assert ran == []
    assert (album / "01.flac").read_bytes() == before
    assert "its embedded cover could not be saved" in capsys.readouterr().out


def test_a_backup_name_that_cannot_be_claimed_fails_that_file_alone(monkeypatch, tmp_path, capsys) -> None:
    flac = tmp_path / "01.flac"
    flac.write_bytes(b"fLaC original")

    def refused(_path: str) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(integrity, "_reserve_backup_path", refused)

    sanitized = anyio.run(integrity._sanitize_flac, str(flac))

    assert sanitized is False
    assert flac.read_bytes() == b"fLaC original"
    assert "no backup name could be claimed" in capsys.readouterr().out


def test_a_working_copy_that_cannot_be_made_fails_that_mp3_alone(monkeypatch, tmp_path, capsys) -> None:
    original = tmp_path / "01.mp3"
    original.write_bytes(b"the original")

    def refused(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(integrity.tempfile, "mkstemp", refused)

    sanitized = anyio.run(integrity._sanitize_mp3, str(original))

    assert sanitized is False
    assert original.read_bytes() == b"the original"
    assert "no working copy could be made" in capsys.readouterr().out
