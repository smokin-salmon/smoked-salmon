"""Sanitizing is checked again afterwards, and the upload stops on a file that still does not decode."""

import os
import subprocess
from importlib import import_module

import anyio
import asyncclick as click
import pytest

import salmon.uploader as uploader
from salmon import cfg

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
        for name in states:
            (folder / name).write_bytes(b"fLaC " + name.encode())

    async def run_process(self, commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if commands[0] == "metaflac":
            return subprocess.CompletedProcess(commands, 0, b"", b"")
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


def _edit_metadata_stubs(monkeypatch, scene: bool = False) -> None:
    async def returns(value=None):
        return value

    monkeypatch.setattr(uploader, "review_metadata_with_ai", lambda metadata, *a, **k: returns(metadata))
    monkeypatch.setattr(uploader, "tag_files", lambda *a, **k: None)
    monkeypatch.setattr(uploader, "check_tags", lambda *a, **k: returns({}))
    monkeypatch.setattr(uploader, "rename_folder", lambda path, *a, **k: path)
    monkeypatch.setattr(uploader, "rename_files", lambda *a, **k: None)
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


def test_a_scene_release_stops_before_the_sanitize_offer(fake_flac, monkeypatch, tmp_path) -> None:
    from salmon.checks.integrity import resolve_integrity_for_upload

    fake_flac({"01.flac": "md5_unset"})
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: pytest.fail("no sanitize offer for a scene release"))

    with pytest.raises(click.Abort):
        anyio.run(lambda: resolve_integrity_for_upload(str(tmp_path), scene=True, assume_yes=True))


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
