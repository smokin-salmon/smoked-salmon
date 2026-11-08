"""salmon up on ALAC in .m4a: the tracks become FLACs holding the same samples, tags and cover (#981)."""

import hashlib
import os
import sys
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import av
import av.audio.stream
import numpy as np
import pytest
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from numpy.typing import NDArray
from PIL import Image

import salmon.uploader as uploader
from salmon import cfg, interaction
from salmon.converter import alac

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

# Stands in for the flac binary, which CI does not have: it takes the raw PCM salmon pipes to it with the options it
# must give, and writes the FLAC with PyAV's encoder. Any other command line fails, as an unknown option to flac does.
FAKE_FLAC = """\
import sys

import av
import numpy as np

args = sys.argv[1:]
expected = {"-V", "-s", "--force-raw-format", "--endian=little", "--sign=signed", "-o", "-"}
if not expected <= set(args) or args[0] != "-%(level)d" or args[-1] != "-":
    sys.exit(f"unexpected command line: {args}")
options = dict(arg[2:].split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)
channels, bits, rate = int(options["channels"]), int(options["bps"]), int(options["sample-rate"])
output = args[args.index("-o") + 1]
try:
    open(output, "xb").close()  # flac without -f never overwrites a file.
except FileExistsError:
    sys.exit(f"{output}: ERROR: output file exists")
raw = sys.stdin.buffer.read()
if bits == 16:
    samples, fmt = np.frombuffer(raw, "<i2"), "s16"
else:
    triples = np.frombuffer(raw, np.uint8).reshape(-1, 3)
    wide = np.zeros((len(triples), 4), np.uint8)
    wide[:, 1:] = triples
    samples, fmt = wide.view("<i4").reshape(-1), "s32"
layout = {1: "mono", 2: "stereo"}[channels]
with av.open(output, "w", format="flac") as out:
    stream = out.add_stream("flac", rate=rate, layout=layout, format=fmt)
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format=fmt, layout=layout)
    frame.rate = rate
    out.mux(stream.encode(frame))
    out.mux(stream.encode(None))
"""

TAGS: dict[str, Any] = {
    "©nam": ["One"],
    "©ART": ["Artist A", "Artist B"],
    "aART": ["Album Artist"],
    "©alb": ["Album"],
    "©day": ["2020"],
    "©gen": ["Electronic"],
    "©wrt": ["Composer"],
    "cprt": ["(C) 2020 Label"],
    "trkn": [(1, 12)],
    "disk": [(1, 2)],
    "----:com.apple.iTunes:ISRC": [MP4FreeForm(b"USAAA2000001")],
    "----:com.apple.iTunes:LABEL": [MP4FreeForm(b"Label")],
    # Dropped: no FLAC field for it in the conversion.
    "©too": ["iTunes"],
}
EXPECTED_TAGS = {
    "title": ["One"],
    "artist": ["Artist A", "Artist B"],
    "albumartist": ["Album Artist"],
    "album": ["Album"],
    "date": ["2020"],
    "genre": ["Electronic"],
    "composer": ["Composer"],
    "copyright": ["(C) 2020 Label"],
    "tracknumber": ["1"],
    "tracktotal": ["12"],
    "discnumber": ["1"],
    "disctotal": ["2"],
    "isrc": ["USAAA2000001"],
    "label": ["Label"],
}


class FakeInteraction:
    """Answers each yes or no question with `answer`, and records it."""

    def __init__(self, answer: bool = True, yyy: bool = False) -> None:
        self.answer = answer
        self.yyy = yyy
        self.confirms: list[tuple[str, dict[str, Any]]] = []

    async def prompt(self, text: str, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected prompt: {text}")

    async def confirm(self, text: str, **kwargs: Any) -> bool:
        self.confirms.append((text, kwargs))
        return self.answer

    async def edit(self, text: str, **kwargs: Any) -> str | None:
        raise AssertionError("unexpected edit")

    async def show_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        raise AssertionError("unexpected spectrals")

    async def assume_defaults(self) -> bool:
        return self.yyy


@pytest.fixture(autouse=True)
def fake_flac(monkeypatch, tmp_path_factory) -> None:
    """Put the fake flac first on PATH."""
    folder = tmp_path_factory.mktemp("bin")
    script = folder / "flac"
    level = cfg.upload.compression.flac_compression_level
    script.write_text(f"#!{sys.executable}\n" + FAKE_FLAC % {"level": level})
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def _samples(bits: int, rate: int, seed: int) -> NDArray[np.int32]:
    """Half a second of stereo noise under a tone, at full use of `bits` bits, as decoded (24-bit: high bytes)."""
    rng = np.random.default_rng(seed)
    count = rate // 2
    tone = np.sin(np.arange(count) * 2 * np.pi * 440 / rate)
    peak = 2 ** (bits - 1) - 1
    data = np.stack([tone * 0.6 * peak + rng.integers(-peak // 4, peak // 4, count) for _ in range(2)])
    data = np.clip(data, -peak, peak).astype(np.int32)
    return data if bits == 16 else data << 8


def _write_alac(path: Path, bits: int, rate: int, seed: int = 0, tags: bool = True) -> NDArray[np.int32]:
    """Write an ALAC .m4a with PyAV, tagged and with a cover; its samples as PyAV decodes them, one row a channel."""
    data = _samples(bits, rate, seed)
    fmt = "s16p" if bits == 16 else "s32p"
    with av.open(str(path), "w", format="ipod") as out:
        stream = out.add_stream("alac", rate=rate, layout="stereo", format=fmt)
        assert isinstance(stream, av.audio.stream.AudioStream)
        for start in range(0, data.shape[1], 4096):
            block = np.ascontiguousarray(data[:, start : start + 4096])
            frame = av.AudioFrame.from_ndarray(
                block.astype(np.int16) if bits == 16 else block, format=fmt, layout="stereo"
            )
            frame.rate = rate
            out.mux(stream.encode(frame))
        out.mux(stream.encode(None))
    if tags:
        tagged = MP4(path)
        if tagged.tags is None:
            tagged.add_tags()
        assert tagged.tags is not None
        for key, value in TAGS.items():
            tagged.tags[key] = value
        tagged.tags["covr"] = [MP4Cover(_png(), imageformat=MP4Cover.FORMAT_PNG), MP4Cover(b"\xff\xd8\xffsecond")]
        tagged.save()
    assert MP4(path).info.codec == "alac"
    return data


def _write_aac(path: Path) -> None:
    """Write a short AAC .m4a with PyAV."""
    with av.open(str(path), "w", format="ipod") as out:
        stream = out.add_stream("aac", rate=44100, layout="stereo")
        assert isinstance(stream, av.audio.stream.AudioStream)
        frame = av.AudioFrame.from_ndarray(np.zeros((2, 4096), np.float32), format="fltp", layout="stereo")
        frame.rate = 44100
        out.mux(stream.encode(frame))
        out.mux(stream.encode(None))
    assert MP4(path).info.codec != "alac"


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(buffer, "PNG")
    return buffer.getvalue()


def _decoded(path: Path) -> NDArray[np.int32]:
    """The samples of an audio file as PyAV decodes them, one row a channel."""
    with av.open(str(path)) as container:
        frames = [frame for frame in container.decode(container.streams.audio[0])]
    planar = frames[0].format.is_planar
    rows = [frame.to_ndarray() for frame in frames]
    if planar:
        return np.concatenate(rows, axis=1).astype(np.int32)
    return np.concatenate(rows, axis=1).reshape(-1, 2).T.astype(np.int32)


def _snapshot(folder: Path) -> dict[str, str]:
    """Every file under folder, by relative path, with a hash of its bytes."""
    return {
        str(entry.relative_to(folder)): hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in sorted(folder.rglob("*"))
        if entry.is_file()
    }


def _convert(album: Path, fake: FakeInteraction, staged: Path | None = None) -> str | None:
    async def run() -> str | None:
        with interaction.using(fake):
            return await alac.convert_alac(str(album), str(staged or album))

    return anyio.run(run)


@pytest.mark.parametrize(("bits", "rate"), [(16, 44100), (24, 96000)])
def test_an_alac_album_becomes_flac_with_the_same_samples_tags_and_cover(tmp_path, bits, rate) -> None:
    (tmp_path / "CD2").mkdir()
    one = _write_alac(tmp_path / "01 One.m4a", bits, rate, seed=1)
    two = _write_alac(tmp_path / "CD2" / "02 Two.M4A", bits, rate, seed=2)
    fake = FakeInteraction()

    assert _convert(tmp_path, fake) is None

    assert sorted(_snapshot(tmp_path)) == ["01 One.flac", "CD2/02 Two.flac"]
    for name, samples in (("01 One.flac", one), ("CD2/02 Two.flac", two)):
        flac = FLAC(tmp_path / name)
        assert (flac.info.bits_per_sample, flac.info.sample_rate, flac.info.channels) == (bits, rate, 2)
        assert np.array_equal(_decoded(tmp_path / name), samples)
        assert {key: flac[key] for key in EXPECTED_TAGS} == EXPECTED_TAGS
        assert "encodedby" not in flac and "tool" not in flac
        assert [(p.type, p.mime, p.data) for p in flac.pictures] == [(3, "image/png", _png())]


def test_the_question_is_asked_once_and_its_default_converts(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100, seed=1)
    _write_alac(tmp_path / "02.m4a", 16, 44100, seed=2)
    fake = FakeInteraction()

    assert _convert(tmp_path, fake) is None

    assert len(fake.confirms) == 1
    text, kwargs = fake.confirms[0]
    assert "2 ALAC files" in text
    assert kwargs["default"] is True
    assert sorted(_snapshot(tmp_path)) == ["01.flac", "02.flac"]


def test_yyy_converts_without_asking(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100)
    fake = FakeInteraction(answer=False, yyy=True)

    assert _convert(tmp_path, fake) is None

    assert fake.confirms == []
    assert sorted(_snapshot(tmp_path)) == ["01.flac"]


def test_a_no_changes_nothing_and_stops(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100)
    before = _snapshot(tmp_path)
    fake = FakeInteraction(answer=False)

    refusal = _convert(tmp_path, fake)

    assert refusal is not None and "ALAC" in refusal
    assert len(fake.confirms) == 1
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("lossy", ["03 Three.m4a", "03 Three.mp3"])
def test_alac_with_lossy_files_stops_naming_them_before_asking(tmp_path, lossy) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100)
    if lossy.endswith(".m4a"):
        _write_aac(tmp_path / lossy)
    else:
        (tmp_path / lossy).write_bytes(b"ID3")
    before = _snapshot(tmp_path)
    fake = FakeInteraction()

    refusal = _convert(tmp_path, fake)

    assert refusal is not None and lossy in refusal
    assert fake.confirms == []
    assert _snapshot(tmp_path) == before


def test_a_refusal_names_a_handful_of_lossy_files_then_counts(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100, tags=False)
    for number in range(2, 10):
        (tmp_path / f"{number:02d}.mp3").write_bytes(b"ID3")

    refusal = _convert(tmp_path, FakeInteraction())

    assert refusal is not None
    assert "02.mp3, 03.mp3, 04.mp3, 05.mp3, 06.mp3 and 3 more" in refusal


def test_alac_with_flac_converts_only_the_alac(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100)
    (tmp_path / "02.flac").write_bytes(b"a FLAC of the user's")
    fake = FakeInteraction()

    assert _convert(tmp_path, fake) is None

    assert "1 ALAC file " in fake.confirms[0][0] + " "
    assert sorted(_snapshot(tmp_path)) == ["01.flac", "02.flac"]
    assert (tmp_path / "02.flac").read_bytes() == b"a FLAC of the user's"


def test_a_flac_where_the_conversion_would_go_stops_it(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100)
    _write_alac(tmp_path / "02.m4a", 16, 44100)
    (tmp_path / "02.flac").write_bytes(b"already here")
    before = _snapshot(tmp_path)
    fake = FakeInteraction()

    refusal = _convert(tmp_path, fake)

    assert refusal is not None and "02.flac" in refusal
    assert fake.confirms == []
    assert _snapshot(tmp_path) == before


def test_a_failed_verification_keeps_every_m4a_and_leaves_no_flac(monkeypatch, tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100, seed=1)
    _write_alac(tmp_path / "02.m4a", 16, 44100, seed=2)
    before = _snapshot(tmp_path)
    decode = alac._pcm_chunks

    def flipped(path: str, bits: int, channels: int) -> Iterator[bytes]:
        for chunk in decode(path, bits, channels):
            # The FLAC of 02 decodes to other samples.
            yield bytes([chunk[0] ^ 1]) + chunk[1:] if path.endswith("02.flac") else chunk

    monkeypatch.setattr(alac, "_pcm_chunks", flipped)

    refusal = _convert(tmp_path, FakeInteraction())

    assert refusal is not None and "02.m4a" in refusal and "same samples" in refusal
    assert _snapshot(tmp_path) == before


def test_an_unreadable_m4a_beside_alac_is_named_as_unreadable_not_lossy(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100)
    (tmp_path / "02.m4a").write_bytes(b"not an mp4")
    before = _snapshot(tmp_path)
    fake = FakeInteraction()

    refusal = _convert(tmp_path, fake)

    assert refusal is not None and "02.m4a" in refusal
    assert "cannot read" in refusal and "lossy" not in refusal
    assert fake.confirms == []
    assert _snapshot(tmp_path) == before


def test_two_alac_files_converting_to_one_name_stop_naming_both(tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100, seed=1)
    _write_alac(tmp_path / "01.M4A", 16, 44100, seed=2)
    before = _snapshot(tmp_path)

    refusal = _convert(tmp_path, FakeInteraction())

    assert refusal is not None and "01.m4a" in refusal and "01.M4A" in refusal
    assert "already in" not in refusal
    assert _snapshot(tmp_path) == before


def test_an_m4a_that_cannot_be_removed_does_not_keep_the_others(monkeypatch, tmp_path) -> None:
    _write_alac(tmp_path / "01.m4a", 16, 44100, seed=1)
    _write_alac(tmp_path / "02.m4a", 16, 44100, seed=2)
    remove = os.remove

    def locked(path: str) -> None:
        if path.endswith("01.m4a"):
            raise PermissionError(13, "Permission denied", path)
        remove(path)

    monkeypatch.setattr(alac.os, "remove", locked)

    refusal = _convert(tmp_path, FakeInteraction())

    assert refusal is not None and "01.m4a" in refusal and "02.m4a" not in refusal
    assert sorted(_snapshot(tmp_path)) == ["01.flac", "01.m4a", "02.flac"]


def test_an_m4a_that_cannot_be_removed_from_a_copy_never_asks_to_remove_the_albums(monkeypatch, tmp_path) -> None:
    album, copy = tmp_path / "album", tmp_path / "copy"
    album.mkdir()
    _write_alac(album / "01.m4a", 16, 44100)
    copy.mkdir()
    (copy / "01.m4a").write_bytes((album / "01.m4a").read_bytes())
    remove = os.remove

    def locked(path: str) -> None:
        if path.endswith("01.m4a"):
            raise PermissionError(13, "Permission denied", path)
        remove(path)

    monkeypatch.setattr(alac.os, "remove", locked)

    refusal = _convert(album, FakeInteraction(), staged=copy)

    assert refusal is not None and "01.m4a" in refusal
    assert "The album is unchanged: keep its .m4a files" in refusal
    assert "remove it" not in refusal
    assert sorted(_snapshot(album)) == ["01.m4a"]
    assert sorted(_snapshot(copy)) == ["01.flac", "01.m4a"]


def test_a_failed_track_stops_the_tracks_not_started_yet(monkeypatch, tmp_path) -> None:
    for number in (1, 2, 3):
        _write_alac(tmp_path / f"0{number}.m4a", 16, 44100, seed=number, tags=False)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(cfg.upload, "simultaneous_threads", 1)
    encoded: list[str] = []

    def failing(track: alac.AlacTrack) -> bytes:
        encoded.append(track.name)
        raise alac.ConversionError("flac failed")

    monkeypatch.setattr(alac, "_encode", failing)

    refusal = _convert(tmp_path, FakeInteraction())

    assert refusal is not None and "01.m4a" in refusal
    assert encoded == ["01.m4a"]
    assert _snapshot(tmp_path) == before


def test_an_album_with_no_alac_is_left_as_it_is(tmp_path) -> None:
    _write_aac(tmp_path / "01.m4a")
    (tmp_path / "02.flac").write_bytes(b"flac")
    before = _snapshot(tmp_path)
    fake = FakeInteraction()

    assert _convert(tmp_path, fake) is None

    assert fake.confirms == []
    assert _snapshot(tmp_path) == before


# Through salmon up: the conversion runs on the folder the upload works on, before the upload's own steps.


@pytest.fixture
def downloads(monkeypatch, tmp_path) -> Path:
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(folder))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", False)
    return folder


def _up(monkeypatch, album: Path, fake: FakeInteraction) -> list[dict[str, str]]:
    """Run salmon up on album with the upload itself stubbed: what the folder it was given held, if it ran."""
    seen: list[dict[str, str]] = []

    async def staged_run(_site, path, *_args, **_kwargs):
        seen.append(_snapshot(Path(path)))
        return None, False, False, None

    monkeypatch.setattr(uploader, "_upload_staged", staged_run)

    async def run() -> None:
        with interaction.using(fake):
            await uploader.upload(cast("BaseGazelleApi", object()), str(album), None, "WEB", False, (), None)

    anyio.run(run)
    return seen


def test_salmon_up_uploads_the_converted_flacs(monkeypatch, downloads, tmp_path) -> None:
    album = tmp_path / "inbox" / "Album"
    album.mkdir(parents=True)
    _write_alac(album / "01.m4a", 24, 96000)

    seen = _up(monkeypatch, album, FakeInteraction())

    assert [sorted(files) for files in seen] == [["01.flac"]]


def test_salmon_up_stops_on_a_no(monkeypatch, downloads, tmp_path) -> None:
    album = tmp_path / "inbox" / "Album"
    album.mkdir(parents=True)
    _write_alac(album / "01.m4a", 16, 44100)
    before = _snapshot(album)

    seen = _up(monkeypatch, album, FakeInteraction(answer=False))

    assert seen == []
    assert _snapshot(album) == before


def test_salmon_up_converts_a_library_album_in_its_copy_only(monkeypatch, downloads, tmp_path) -> None:
    library = tmp_path / "library"
    album = library / "Album"
    album.mkdir(parents=True)
    _write_alac(album / "01.m4a", 16, 44100)
    before = _snapshot(album)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])

    seen = _up(monkeypatch, album, FakeInteraction())

    assert [sorted(files) for files in seen] == [["01.flac"]]
    assert _snapshot(album) == before


def test_salmon_up_leaves_an_aac_album_to_the_encoding_question(monkeypatch, downloads, tmp_path) -> None:
    album = tmp_path / "inbox" / "Album"
    album.mkdir(parents=True)
    _write_aac(album / "01.m4a")
    before = _snapshot(album)
    fake = FakeInteraction()

    seen = _up(monkeypatch, album, fake)

    assert seen == [before]
    assert fake.confirms == []
