"""Spectral PNGs are compressed with oxipng where it installs, and left as sox wrote them where it does not (3.14)."""

import sys
from functools import partial
from pathlib import Path

import anyio
import pytest

from salmon.uploader import spectrals


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
            calls.append((Path(path).name, kwargs))

    monkeypatch.setattr(spectrals, "oxipng", FakeOxipng)
    monkeypatch.setattr(spectrals, "_not_compressed_notice_shown", False, raising=False)
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert sorted(calls) == [
        ("01 Full.png", {"level": 2, "strip": "all"}),
        ("01 Zoom.png", {"level": 2, "strip": "all"}),
    ]


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

    assert sorted(log.read_text().splitlines()) == [
        f"-o 2 --strip all {path / '01 Full.png'}",
        f"-o 2 --strip all {path / '01 Zoom.png'}",
    ]
    assert (path / "01 Full.png").read_text().strip() == "small"
    assert (path / "02 Full.png").read_bytes() == b"png 2 Full"


@posix_only
def test_a_failing_oxipng_program_leaves_the_file_and_prints_a_line(monkeypatch, tmp_path, capsys) -> None:
    _hide_pyoxipng(monkeypatch)
    _fake_program(monkeypatch, tmp_path, "exit 1")
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
    out = capsys.readouterr().out
    assert "Could not compress 01 Full.png" in out
    assert "Could not compress 01 Zoom.png" in out


@posix_only
def test_an_oxipng_program_that_hangs_is_given_up_on(monkeypatch, tmp_path, capsys) -> None:
    _hide_pyoxipng(monkeypatch)
    monkeypatch.setattr(spectrals, "OXIPNG_PROGRAM_TIMEOUT", 0.3)
    _fake_program(monkeypatch, tmp_path, "exec sleep 30")
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path), {1: "01.flac"}))

    assert (path / "01 Full.png").read_bytes() == b"png 1 Full"
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

    class FakeOxipng:
        class StripChunks:
            @staticmethod
            def all() -> str:
                return "all"

        @staticmethod
        def optimize(path, **kwargs) -> None:
            pass

    monkeypatch.setattr(spectrals, "oxipng", FakeOxipng)
    monkeypatch.setattr(spectrals.shutil, "which", lambda name: looked_up.append(name))
    path = _spectrals_dir(tmp_path)

    anyio.run(partial(spectrals._compress_spectrals, str(path)))

    assert looked_up == []
