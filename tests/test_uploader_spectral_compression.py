"""Spectral PNGs are compressed with oxipng where it installs, and left as sox wrote them where it does not (3.14)."""

import sys
import threading
from functools import partial
from pathlib import Path

import anyio
import pytest

from salmon.uploader import spectrals


def _spectral_of_copy(path) -> str:
    """The name of the spectral whose working copy this is."""
    return Path(path).name.removeprefix(".").split(".png")[0] + ".png"


def _spectrals_dir(tmp_path: Path) -> Path:
    path = tmp_path / "Spectrals"
    path.mkdir()
    for sid in (1, 2):
        for kind in ("Full", "Zoom"):
            (path / f"{sid:02d} {kind}.png").write_bytes(f"png {sid} {kind}".encode())
    return path


def test_without_oxipng_spectrals_are_left_as_is_and_the_notice_is_printed_once(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(spectrals, "oxipng", None)
    monkeypatch.setattr(spectrals.shutil, "which", lambda _name: None)
    monkeypatch.setattr(spectrals, "_not_compressed_notice_shown", False, raising=False)
    path = _spectrals_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in path.iterdir()}

    # Twice in one run, as when spectrals are compressed for more than one upload.
    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))
    anyio.run(partial(spectrals._compress_spectrals, str(path)))

    assert {p.name: p.read_bytes() for p in path.iterdir()} == before
    out = capsys.readouterr().out
    assert out.count("Spectrals are not compressed") == 1
    assert "uv tool install --python 3.13 git+https://github.com/smokin-salmon/smoked-salmon" in out
    assert "Finished compressing" not in out


def test_with_oxipng_it_is_used(monkeypatch, tmp_path) -> None:
    calls = []

    class FakeStripChunks:
        @staticmethod
        def all() -> str:
            return "all"

    class FakeOxipng:
        StripChunks = FakeStripChunks

        @staticmethod
        def optimize(path, **kwargs) -> None:
            calls.append((_spectral_of_copy(path), kwargs))

    monkeypatch.setattr(spectrals, "oxipng", FakeOxipng)
    monkeypatch.setattr(spectrals, "_not_compressed_notice_shown", False, raising=False)
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert sorted(calls) == [
        ("01 Full.png", {"level": 2, "strip": "all"}),
        ("01 Zoom.png", {"level": 2, "strip": "all"}),
    ]


class _FakeOxipng:
    """pyoxipng on any Python: optimize() calls the function given, with the file's Path."""

    class PngError(Exception):
        pass

    class StripChunks:
        @staticmethod
        def all() -> str:
            return "all"

    def __init__(self, optimize) -> None:
        self.optimize = lambda path, **_kwargs: optimize(Path(path))


def _hidden(path: Path) -> list[str]:
    return [entry.name for entry in path.iterdir() if entry.name.startswith(".")]


def test_the_file_stays_whole_until_its_compressed_copy_replaces_it(monkeypatch, tmp_path, capsys) -> None:
    path = _spectrals_dir(tmp_path)
    original = path / "01 Full.png"
    while_compressing = []

    def optimize(working: Path) -> None:
        working.write_bytes(b"small")
        if working.name.startswith(".01 Full"):
            while_compressing.append((working.parent, original.read_bytes()))

    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(optimize))

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert while_compressing == [(path, b"png 1 Full")]
    assert original.read_bytes() == b"small"
    assert _hidden(path) == []
    assert "Finished compressing spectrals." in capsys.readouterr().out


def test_two_compressions_of_one_file_at_once_each_work_on_their_own_copy(monkeypatch, tmp_path) -> None:
    path = _spectrals_dir(tmp_path)
    both_running = threading.Barrier(2, timeout=10)
    seen = []

    def optimize(working: Path) -> None:
        seen.append(working)
        both_running.wait()
        working.write_bytes(b"small")

    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(optimize))

    async def both() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(spectrals._compress_single_spectral, str(path / "01 Full.png"), 0)
            tg.start_soon(spectrals._compress_single_spectral, str(path / "01 Full.png"), 0)

    anyio.run(both)

    assert len(set(seen)) == 2
    assert (path / "01 Full.png").read_bytes() == b"small"
    assert _hidden(path) == []


def _refused(*_args) -> None:
    raise OSError("no")


@pytest.mark.parametrize("step", ["copyfile", "replace"], ids=["no-space-for-the-copy", "file-held-open-on-windows"])
def test_a_spectral_that_cannot_be_compressed_is_left_as_it_is_and_the_run_goes_on(
    monkeypatch, tmp_path, capsys, step
) -> None:
    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(lambda working: working.write_bytes(b"small")))
    monkeypatch.setattr(spectrals.shutil if step == "copyfile" else spectrals.os, step, _refused)
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    assert _hidden(path) == []
    out = capsys.readouterr().out
    assert "Could not compress 01 Full.png" in out
    assert "Could not compress 01 Zoom.png" in out
    assert "Some spectrals could not be compressed." in out


def test_one_spectral_failing_does_not_stop_the_next_being_compressed(monkeypatch, tmp_path, capsys) -> None:
    copy = spectrals.shutil.copyfile

    def refused_for_full(source: str, target: str) -> None:
        if source.endswith("01 Full.png"):
            _refused()
        copy(source, target)

    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(lambda working: working.write_bytes(b"small")))
    monkeypatch.setattr(spectrals.shutil, "copyfile", refused_for_full)
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    assert (path / "01 Zoom.png").read_bytes() == b"small"
    out = capsys.readouterr().out
    assert "Could not compress 01 Full.png" in out
    assert "Could not compress 01 Zoom.png" not in out
    assert "Some spectrals could not be compressed." in out
    assert "Finished compressing" not in out


def test_a_copy_that_cannot_be_removed_does_not_end_the_run(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(lambda working: working.write_bytes(b"small")))
    monkeypatch.setattr(spectrals.os, "replace", _refused)
    monkeypatch.setattr(spectrals.os, "remove", _refused)
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    assert "Could not compress 01 Full.png" in capsys.readouterr().out


def test_a_cancelled_compression_leaves_the_file_and_no_copy(monkeypatch, tmp_path) -> None:
    def optimize(_working: Path) -> None:
        raise AssertionError("the cancellation comes first")

    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(optimize))
    path = _spectrals_dir(tmp_path)

    async def cancelled() -> None:
        with anyio.CancelScope() as scope:
            scope.cancel()
            await spectrals._compress_single_spectral(str(path / "01 Full.png"), 0)
        assert scope.cancelled_caught

    anyio.run(cancelled)

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    assert _hidden(path) == []


def test_a_png_error_from_pyoxipng_is_handled_like_a_failing_program(monkeypatch, tmp_path, capsys) -> None:
    def optimize(_working: Path) -> None:
        raise _FakeOxipng.PngError("bad")

    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(optimize))
    path = _spectrals_dir(tmp_path)

    compressed = anyio.run(spectrals._compress_single_spectral, str(path / "01 Full.png"), 0)

    assert compressed is False
    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    assert _hidden(path) == []
    assert "Could not compress 01 Full.png with oxipng; the original is kept." in capsys.readouterr().out


def _hide_pyoxipng(monkeypatch) -> None:
    monkeypatch.setattr(spectrals, "oxipng", None)
    monkeypatch.setattr(spectrals, "_not_compressed_notice_shown", False, raising=False)


def _fake_program(monkeypatch, tmp_path: Path, body: str) -> Path:
    """Put a fake oxipng on PATH; it logs its arguments to a file, then runs `body`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    script = bin_dir / "oxipng"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n{body}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    return log


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake program is a shell script")


@posix_only
def test_without_pyoxipng_the_oxipng_program_compresses_each_file(monkeypatch, tmp_path) -> None:
    _hide_pyoxipng(monkeypatch)
    log = _fake_program(monkeypatch, tmp_path, 'for last; do :; done; echo small > "$last"')
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    calls = sorted(log.read_text().splitlines())
    assert len(calls) == 2
    assert all(call.startswith("-o 2 --strip all ") and call.endswith(".png") for call in calls)
    assert "01 Full.png" in calls[0]
    assert "01 Zoom.png" in calls[1]
    assert (path / "01 Full.png").read_text().strip() == "small"
    assert (path / "02 Full.png").read_bytes() == b"png 2 Full"
    assert sorted(p.name for p in path.iterdir()) == sorted(
        f"{s:02d} {k}.png" for s in (1, 2) for k in ("Full", "Zoom")
    )


@posix_only
def test_a_failing_oxipng_program_leaves_the_file_and_prints_a_line(monkeypatch, tmp_path, capsys) -> None:
    _hide_pyoxipng(monkeypatch)
    # A program that damages its input before failing must not touch the original.
    _fake_program(monkeypatch, tmp_path, 'for last; do :; done; echo broken > "$last"; exit 1')
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    assert sorted(p.name for p in path.iterdir()) == sorted(
        f"{s:02d} {k}.png" for s in (1, 2) for k in ("Full", "Zoom")
    )
    out = capsys.readouterr().out
    assert "Could not compress 01 Full.png" in out
    assert "Could not compress 01 Zoom.png" in out
    assert "Finished compressing" not in out


@posix_only
def test_an_oxipng_program_that_hangs_is_given_up_on(monkeypatch, tmp_path, capsys) -> None:
    _hide_pyoxipng(monkeypatch)
    monkeypatch.setattr(spectrals, "OXIPNG_PROGRAM_TIMEOUT", 0.3)
    _fake_program(monkeypatch, tmp_path, 'for last; do :; done; echo broken > "$last"; exec sleep 30')
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    assert sorted(p.name for p in path.iterdir()) == sorted(
        f"{s:02d} {k}.png" for s in (1, 2) for k in ("Full", "Zoom")
    )
    assert "Could not compress 01 Full.png" in capsys.readouterr().out


def test_without_pyoxipng_and_program_the_notice_names_the_program_option(monkeypatch, tmp_path, capsys) -> None:
    _hide_pyoxipng(monkeypatch)
    monkeypatch.setattr(spectrals.shutil, "which", lambda _name: None)
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path)))
    anyio.run(partial(spectrals._compress_spectrals, str(path)))

    out = capsys.readouterr().out
    assert out.count("Spectrals are not compressed") == 1
    assert "oxipng program" in out


def test_with_pyoxipng_the_program_is_never_looked_up(monkeypatch, tmp_path) -> None:
    looked_up = []

    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(lambda _working: None))
    monkeypatch.setattr(spectrals.shutil, "which", lambda name: looked_up.append(name))
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path)))

    assert looked_up == []


def test_a_hidden_leftover_is_neither_compressed_nor_shown(monkeypatch, tmp_path) -> None:
    compressed: list[str] = []

    monkeypatch.setattr(spectrals, "oxipng", _FakeOxipng(lambda working: compressed.append(_spectral_of_copy(working))))
    path = _spectrals_dir(tmp_path)
    (path / ".01 Full.png.tmp.png").write_bytes(b"leftover")

    anyio.run(partial(spectrals._compress_spectrals, str(path)))

    assert sorted(compressed) == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]
    assert spectrals._spectral_images(str(path)) == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]


def test_the_windows_viewer_skips_a_hidden_leftover(monkeypatch, tmp_path) -> None:
    opened: list[str] = []
    monkeypatch.setattr(spectrals.os, "startfile", opened.append, raising=False)
    path = _spectrals_dir(tmp_path)
    (path / ".00 Full.png.tmp.png").write_bytes(b"leftover")

    spectrals._open_specs_in_windows(str(path))

    assert opened == [str(path / "01 Full.png")]


def test_the_quick_look_viewer_is_given_the_pngs_only(monkeypatch, tmp_path) -> None:
    run: list[list[str]] = []

    async def fake_run_process(args, **kwargs) -> None:
        run.append(args)

    monkeypatch.setattr(spectrals.anyio, "run_process", fake_run_process)
    path = _spectrals_dir(tmp_path)
    (path / ".00 Full.png.tmp.png").write_bytes(b"leftover")
    (path / "notes.txt").write_bytes(b"not an image")

    anyio.run(spectrals._open_specs_in_preview, str(path))

    assert [Path(arg).name for arg in run[0][2:]] == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]
