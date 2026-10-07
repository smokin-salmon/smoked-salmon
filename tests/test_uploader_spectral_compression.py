"""Spectral PNGs are compressed with oxipng where it installs, and left as sox wrote them where it does not (3.14)."""

from functools import partial
from pathlib import Path

import anyio

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
