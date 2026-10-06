"""Ripper and store markers in the tags, and the claims among them that the audio contradicts (#538).

Ported in part from chodeus's fork (tests/test_checks_provenance.py).
"""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.flac import FLAC, VCFLACDict
from mutagen.id3 import COMM, TENC, TSSE
from mutagen.mp3 import MP3

from salmon.checks import provenance as pv


def _write_flac(path: Path, *, bits: int = 16, rate: int = 44100, vendor: str | None = None, **tags: str) -> Path:
    """A FLAC with no audio: a STREAMINFO block saying `bits` and `rate`, and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((rate << 44) | (1 << 41) | ((bits - 1) << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    tagged.add_tags()
    comments = tagged.tags
    assert isinstance(comments, VCFLACDict)
    for key, value in tags.items():
        comments[key] = value
    if vendor is not None:
        comments.vendor = vendor
    tagged.save()
    return path


def _write_mp3(path: Path, *frames, rate: int = 44100) -> Path:
    """A silent MPEG-1 layer III file at 128 kbps and `rate`, tagged with the given ID3 frames."""
    rate_index, size = {44100: (0, 417), 48000: (1, 384)}[rate]
    header = bytes([0xFF, 0xFB, 0x90 | rate_index << 2, 0x00])
    path.write_bytes((header + bytes(size - len(header))) * 3)
    mut = MP3(path)
    mut.add_tags()
    assert mut.tags is not None
    for frame in frames:
        mut.tags.add(frame)
    mut.save(v1=0)
    return path


def _album(tmp_path: Path, **flac) -> str:
    _write_flac(tmp_path / "01.flac", **flac)
    return str(tmp_path)


# A bit depth the audio does not have


def test_a_24bit_claim_on_a_16bit_file_is_reported_with_its_file_field_and_both_depths(tmp_path) -> None:
    contradictions = pv.gather_provenance(_album(tmp_path, bits=16, comment="24bit remaster"))["contradictions"]

    assert contradictions == ["01.flac: comment claims 24bit, the audio is 16bit"]


def test_a_matching_claim_is_not_a_contradiction(tmp_path) -> None:
    album = _album(tmp_path, bits=24, rate=96000, comment="24-bit master")

    assert pv.gather_provenance(album)["contradictions"] == []


@pytest.mark.parametrize(
    "marker",
    [
        "hd24bit.com",
        "hd24bit.de",
        "hd24bit.com/24bit",
        "hd24bit.com:8080/24bit",
        "from hd24bit.com/releases/24bit-master",
    ],
)
def test_a_depth_inside_a_domain_is_a_name_not_a_claim(tmp_path, marker: str) -> None:
    album = _album(tmp_path, bits=16, comment=marker)

    provenance = pv.gather_provenance(album)

    assert provenance["contradictions"] == []
    assert provenance["markers"] == [f"comment: {marker}"], "the marker is still read, only not as a claim"


def test_a_real_claim_beside_a_domain_is_still_caught(tmp_path) -> None:
    for marker in ("24bit master from hd24bit.com", "hd24bit.com, 24bit master", "hd24bit.de 24bit master"):
        _write_flac(tmp_path / "01.flac", bits=16, comment=marker)
        assert len(pv.gather_provenance(str(tmp_path))["contradictions"]) == 1, marker


def test_a_file_with_no_lossless_depth_cannot_contradict_a_depth_claim() -> None:
    """A lossy file's depth (AAC reads as 16) says nothing about the master it was encoded from."""
    for info in (None, SimpleNamespace(codec="mp4a.40.2", bits_per_sample=16, sample_rate=44100)):
        tagfile = SimpleNamespace(mut=SimpleNamespace(tags={"\xa9cmt": ["24bit master"]}, info=info))
        assert pv._contradictions([pv._file_provenance("01.m4a", tagfile)]) == []


def test_an_alac_file_has_a_depth_to_contradict() -> None:
    info = SimpleNamespace(codec="alac", bits_per_sample=16, sample_rate=44100)
    tagfile = SimpleNamespace(mut=SimpleNamespace(tags={"\xa9cmt": ["24bit master"]}, info=info))
    assert pv._contradictions([pv._file_provenance("01.m4a", tagfile)]) == [
        "01.m4a: comment claims 24bit, the audio is 16bit"
    ]


# A CD ripper on audio a CD cannot hold


def test_eac_on_a_24bit_96khz_flac_is_reported(tmp_path) -> None:
    provenance = pv.gather_provenance(_album(tmp_path, bits=24, rate=96000, **{"encoded-by": "EAC"}))

    assert provenance["contradictions"] == ["01.flac: encoded-by names EAC, a CD ripper, but the audio is 24bit/96kHz"]


def test_eac_on_a_16bit_44khz_flac_is_ordinary(tmp_path) -> None:
    album = _album(tmp_path, **{"encoded-by": "EAC"}, comment="EAC FLAC -8")

    assert pv.gather_provenance(album)["contradictions"] == []


@pytest.mark.parametrize(
    "marker",
    ["Exact Audio Copy V1.6", "eac", "whipper 0.10.0", "morituri", "Rubyripper", "CUERipper v2.2.6"],
)
def test_every_pure_cd_ripper_is_recognised_as_a_word(tmp_path, marker: str) -> None:
    album = _album(tmp_path, bits=16, rate=48000, encoder=marker)

    assert len(pv.gather_provenance(album)["contradictions"]) == 1


@pytest.mark.parametrize(
    "marker", ["XLD", "dBpoweramp Release 17.6", "CUETools 2.2.5", "fre:ac", "EZ CD Audio Converter"]
)
def test_a_ripper_that_also_converts_downloads_is_not_a_cd_claim(tmp_path, marker: str) -> None:
    """These also convert hi-res downloads: their marker on a clean 24/96 WEB release must not warn."""
    album = _album(tmp_path, bits=24, rate=96000, **{"encoded-by": marker})

    assert pv.gather_provenance(album)["contradictions"] == []


@pytest.mark.parametrize("marker", ["PEACE", "E-AC-3", "eac3to", "teacher"])
def test_eac_inside_another_word_is_not_a_ripper(tmp_path, marker: str) -> None:
    album = _album(tmp_path, bits=24, rate=96000, comment=marker)

    assert pv.gather_provenance(album)["contradictions"] == []


# MP3 and M4A frames, read by their field names


def test_mp3_markers_are_read_under_their_field_names(tmp_path) -> None:
    _write_mp3(
        tmp_path / "01.mp3",
        COMM(encoding=3, lang="eng", desc="", text=["24bit master"]),
        TENC(encoding=3, text=["EAC"]),
        TSSE(encoding=3, text=["LAME 3.100"]),
    )

    provenance = pv.gather_provenance(str(tmp_path))

    assert provenance["markers"] == ["comment: 24bit master", "encoded-by: EAC", "encoder settings: LAME 3.100"]
    assert provenance["contradictions"] == [], "an MP3 at 44.1 kHz has no depth to contradict"


def test_a_cd_ripper_on_a_48khz_mp3_is_reported(tmp_path) -> None:
    _write_mp3(tmp_path / "01.mp3", COMM(encoding=3, lang="eng", desc="", text=["Ripped with EAC"]), rate=48000)

    assert pv.gather_provenance(str(tmp_path))["contradictions"] == [
        "01.mp3: comment names EAC, a CD ripper, but the audio is 48kHz"
    ]


def test_m4a_markers_are_read_under_their_field_names() -> None:
    tags = {"\xa9too": ["iTunes 12.9.0.167"], "\xa9cmt": ["ripped with XLD"]}
    entry = pv._file_provenance("01.m4a", SimpleNamespace(mut=SimpleNamespace(tags=tags, info=None)))
    assert entry["markers"] == {"encoder": "iTunes 12.9.0.167", "comment": "ripped with XLD"}


def test_a_store_url_under_any_key_is_a_marker(tmp_path) -> None:
    url = "https://www.qobuz.com/album/journaling-illy/ul39e7xjbuqrb"
    provenance = pv.gather_provenance(_album(tmp_path, qobuz_url=url))
    assert provenance["markers"] == [f"qobuz_url: {url}"]
    assert provenance["urls"] == [url]


# What is not a signal


def test_a_clean_web_release_has_markers_and_no_contradiction(tmp_path) -> None:
    provenance = pv.gather_provenance(
        _album(tmp_path, bits=24, rate=96000, vendor="Lavf60.16.100", comment="Qobuz", url="https://www.qobuz.com/x")
    )

    assert provenance["vendors"] == ["Lavf60.16.100"]
    assert provenance["markers"] == ["comment: Qobuz", "url: https://www.qobuz.com/x"]
    assert provenance["contradictions"] == []


def test_a_folder_with_no_readable_files_has_nothing_to_report(tmp_path) -> None:
    (tmp_path / "01.flac").write_bytes(b"not a flac")
    provenance = pv.gather_provenance(str(tmp_path))
    assert provenance["contradictions"] == []


def test_a_file_without_tags_reads_as_empty_rather_than_raising() -> None:
    entry = pv._file_provenance("01.flac", SimpleNamespace(mut=None))
    assert entry["markers"] == {}
    assert entry["vendor"] is None
    assert entry["bitdepth"] is None
