"""Detecting the media source from the album's own files (#537)."""

import struct
from functools import partial
from pathlib import Path

import anyio
import pytest
from mutagen.flac import FLAC
from mutagen.id3 import COMM, TMED, TRCK, TXXX, WXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4FreeForm

import salmon.uploader
from salmon.checks import source as source_mod
from salmon.checks.source import DetectedSource, detect_source
from salmon.constants import SOURCES
from salmon.trackers.red import RedApi

QOBUZ_URL = "https://www.qobuz.com/album/journaling-illy/ul39e7xjbuqrb"
EAC_LOG = "Exact Audio Copy V1.6 from 23. October 2020\r\n\r\nEAC extraction logfile from 1. January 2024\r\n"


def _write_flac(path: Path, tags: dict[str, str], bits: int = 16, rate: int = 44100) -> None:
    """Write a FLAC file with no audio: just a STREAMINFO block, tagged with `tags`."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((rate << 44) | (1 << 41) | ((bits - 1) << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    mut = FLAC(path)
    for key, value in tags.items():
        mut[key] = value
    mut.save()


def _write_mp3(path: Path, *frames) -> None:
    """Write a minimal MP3 (silent, no real audio) carrying the given ID3 frames."""
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)
    path.write_bytes(frame * 3)
    mut = MP3(path)
    mut.add_tags()
    assert mut.tags is not None
    for id3_frame in frames:
        mut.tags.add(id3_frame)
    mut.save()


class _FakeInfo:
    bits_per_sample = 16
    sample_rate = 44100


class _FakeM4a:
    info = _FakeInfo()

    def __init__(self, tags: dict) -> None:
        self.tags = tags


def _fake_m4a(album: Path, monkeypatch, tags: dict) -> None:
    """An M4A carrying `tags`: mutagen cannot write a playable M4A from nothing, so its reader is faked."""
    (album / "01.m4a").write_bytes(b"")
    monkeypatch.setattr(source_mod, "MutagenFile", lambda _path: _FakeM4a(tags))


def _album(tmp_path: Path, tags: dict[str, str] | None = None, *, tracks: int = 2, **audio) -> Path:
    """A folder of FLACs, all carrying `tags` besides their own track number."""
    for number in range(1, tracks + 1):
        _write_flac(tmp_path / f"{number:02d}.flac", {"TRACKNUMBER": str(number), **(tags or {})}, **audio)
    return tmp_path


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("EAC.log", EAC_LOG.encode("utf-16")),
        ("album.log", b"X Lossless Decoder version 20230627 (158.2)\n\nXLD extraction logfile\n"),
        ("CD1/whipper.log", b"Log created by: whipper 0.10.0 (internal logger)\n"),
    ],
    ids=["eac-utf16", "xld", "whipper-in-disc-folder"],
)
def test_a_rip_log_proves_cd(tmp_path, name, content) -> None:
    album = _album(tmp_path)
    (album / name).parent.mkdir(exist_ok=True)
    (album / name).write_bytes(content)

    detected = detect_source(str(album))

    assert detected == DetectedSource("CD", f"rip log found ({Path(name).name})")


def test_a_log_that_is_not_a_rip_log_proves_nothing(tmp_path) -> None:
    album = _album(tmp_path)
    (album / "download.log").write_text("Downloading track 1 of 10\n")

    assert detect_source(str(album)) is None


@pytest.mark.parametrize(
    ("tags", "reason"),
    [
        ({"PURCHASE_LINK": QOBUZ_URL}, "Qobuz URL in the tags"),
        ({"COMMENT": "https://listen.tidal.com/album/2468665"}, "Tidal URL in the tags"),
        ({"MY OWN KEY": "https://artist.bandcamp.com/album/y"}, "Bandcamp URL in the tags"),
    ],
    ids=["qobuz-custom-key", "tidal-comment", "bandcamp-custom-key"],
)
def test_a_store_url_proves_web_whatever_tag_holds_it(tmp_path, tags, reason) -> None:
    album = _album(tmp_path, tags)

    assert detect_source(str(album)) == DetectedSource("WEB", reason)


def test_a_store_url_in_an_mp3_proves_web(tmp_path) -> None:
    _write_mp3(tmp_path / "01.mp3", WXXX(encoding=3, desc="PAGE", url="https://music.apple.com/au/album/j/1623086473"))

    assert detect_source(str(tmp_path)) == DetectedSource("WEB", "Apple URL in the tags")


def test_a_bandcamp_comment_in_a_flac_proves_web(tmp_path) -> None:
    album = _album(tmp_path, {"COMMENT": "Visit https://artist.bandcamp.com"})

    assert detect_source(str(album)) == DetectedSource("WEB", "Bandcamp comment in the tags")


def test_an_amazon_comment_in_an_mp3_proves_web(tmp_path) -> None:
    comment = COMM(encoding=3, lang="eng", desc="", text=["Amazon.com Song ID: 200000707885981"])
    _write_mp3(tmp_path / "01.mp3", comment)

    assert detect_source(str(tmp_path)) == DetectedSource("WEB", "Amazon download comment in the tags")


@pytest.mark.parametrize("tags", [{"apID": ["someone@example.com"]}, {"purd": ["2020-01-01 10:00:00"]}])
def test_itunes_purchase_tags_in_an_m4a_prove_web(tmp_path, monkeypatch, tags) -> None:
    _fake_m4a(tmp_path, monkeypatch, tags)

    assert detect_source(str(tmp_path)) == DetectedSource("WEB", "iTunes purchase tags")


def test_a_media_tag_in_a_flac_is_taken_at_its_word(tmp_path) -> None:
    album = _album(tmp_path, {"MEDIA": "Digital Media"})

    assert detect_source(str(album)) == DetectedSource("WEB", 'media tag says "Digital Media"')


def test_a_media_tag_in_an_mp3_is_taken_at_its_word(tmp_path) -> None:
    _write_mp3(tmp_path / "01.mp3", TMED(encoding=3, text=["CD"]))

    assert detect_source(str(tmp_path)) == DetectedSource("CD", 'media tag says "CD"')


def test_a_media_tag_in_an_m4a_is_taken_at_its_word(tmp_path, monkeypatch) -> None:
    _fake_m4a(tmp_path, monkeypatch, {"----:com.apple.iTunes:MEDIA": [MP4FreeForm(b'12" Vinyl')]})

    assert detect_source(str(tmp_path)) == DetectedSource("Vinyl", 'media tag says "12" Vinyl"')


def test_every_source_the_detector_names_is_a_valid_answer_to_the_prompt() -> None:
    valid = set(SOURCES.values())
    assert set(source_mod._MEDIA_VALUES.values()).issubset(valid)
    assert source_mod._VINYL_SIDE_SOURCES.issubset(valid)


def test_vinyl_side_numbering_alone_proves_nothing(tmp_path) -> None:
    for number, side in enumerate(["A1", "A2", "B1", "B2"], start=1):
        _write_flac(tmp_path / f"{number:02d}.flac", {"TRACKNUMBER": side}, bits=24, rate=96000)

    assert detect_source(str(tmp_path)) is None


def test_hi_res_alone_proves_nothing(tmp_path) -> None:
    album = _album(tmp_path, bits=24, rate=96000)

    assert detect_source(str(album)) is None


def test_plain_cd_quality_with_no_log_is_undecidable(tmp_path) -> None:
    """16/44.1 with no log could be a logless CD rip or a WEB download: a cue sheet does not decide it."""
    album = _album(tmp_path, {"ARTIST": "X", "ALBUM": "Y"})
    (album / "album.cue").write_text('FILE "01.flac" WAVE\n')

    assert detect_source(str(album)) is None


@pytest.mark.parametrize(
    "tags",
    [
        {"ASIN": "B000123456"},
        {"MUSICBRAINZ_RELATIONSHIP_URL__PURCHASE FOR DOWNLOAD": "https://artist.bandcamp.com/album/y"},
        {"URL": "https://www.discogs.com/release/123-Artist-Album"},
        {"WEBSITE": "https://www.artist-homepage.com/"},
        {"COMMENT": "Bought on Qobuz, tagged by hand"},
        {"COMMENT": "bought on bandcamp.com"},
    ],
    ids=[
        "picard-asin",
        "musicbrainz-purchase-link",
        "discogs-url",
        "artist-homepage",
        "hand-written-comment",
        "hand-written-bandcamp-comment",
    ],
)
def test_tags_a_user_or_a_tagger_writes_do_not_prove_web(tmp_path, tags) -> None:
    album = _album(tmp_path, tags)

    assert detect_source(str(album)) is None


def test_a_custom_m4a_atom_does_not_prove_web(tmp_path, monkeypatch) -> None:
    """Every tagger files custom M4A fields under com.apple.iTunes, so the namespace proves nothing."""
    _fake_m4a(tmp_path, monkeypatch, {"----:com.apple.iTunes:BARCODE": [MP4FreeForm(b"0602448406705")]})

    assert detect_source(str(tmp_path)) is None


def test_an_mp3_txxx_that_looks_like_a_store_field_does_not_prove_web(tmp_path) -> None:
    _write_mp3(tmp_path / "01.mp3", TXXX(encoding=3, desc="STORE", text=["Qobuz"]))

    assert detect_source(str(tmp_path)) is None


def test_a_rip_log_and_a_store_url_conflict(tmp_path) -> None:
    album = _album(tmp_path, {"COMMENT": QOBUZ_URL})
    (album / "EAC.log").write_bytes(EAC_LOG.encode("utf-16"))

    assert detect_source(str(album)) is None


def test_a_media_tag_that_disagrees_with_a_store_url_conflicts(tmp_path) -> None:
    album = _album(tmp_path, {"MEDIA": "CD", "SOURCE": QOBUZ_URL})

    assert detect_source(str(album)) is None


def test_files_whose_media_tags_disagree_conflict(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", {"MEDIA": "CD"})
    _write_flac(tmp_path / "02.flac", {"MEDIA": "Vinyl"})

    assert detect_source(str(tmp_path)) is None


def test_a_rip_log_beside_hi_res_files_conflicts(tmp_path) -> None:
    """A CD holds 16/44.1 at most, so hi-res files contradict the log."""
    album = _album(tmp_path, bits=24, rate=96000)
    (album / "EAC.log").write_bytes(EAC_LOG.encode("utf-16"))

    assert detect_source(str(album)) is None


def test_a_rip_log_beside_vinyl_sides_conflicts(tmp_path) -> None:
    for number, side in enumerate(["A1", "B1"], start=1):
        _write_flac(tmp_path / f"{number:02d}.flac", {"TRACKNUMBER": side})
    (tmp_path / "EAC.log").write_bytes(EAC_LOG.encode("utf-16"))

    assert detect_source(str(tmp_path)) is None


def test_mp3_vinyl_sides_are_read_from_trck(tmp_path) -> None:
    (tmp_path / "EAC.log").write_bytes(EAC_LOG.encode("utf-16"))
    for number, side in enumerate(["A1/4", "B1/4"], start=1):
        _write_mp3(tmp_path / f"{number:02d}.mp3", TRCK(encoding=3, text=[side]))

    assert detect_source(str(tmp_path)) is None


def test_vinyl_sides_agree_with_a_store_url(tmp_path) -> None:
    """The WEB release of a vinyl album can keep its side numbering."""
    for number, side in enumerate(["A1", "B1"], start=1):
        _write_flac(tmp_path / f"{number:02d}.flac", {"TRACKNUMBER": side, "SOURCE": QOBUZ_URL})

    assert detect_source(str(tmp_path)) == DetectedSource("WEB", "Qobuz URL in the tags")


def test_agreeing_proofs_give_every_reason(tmp_path) -> None:
    album = _album(tmp_path, {"MEDIA": "CD"})
    (album / "EAC.log").write_bytes(EAC_LOG.encode("utf-16"))

    assert detect_source(str(album)) == DetectedSource("CD", 'rip log found (EAC.log); media tag says "CD"')


def test_a_corrupt_audio_file_does_not_sink_the_scan(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", {"COMMENT": QOBUZ_URL})
    (tmp_path / "02.flac").write_bytes(b"fLaC not really")

    assert detect_source(str(tmp_path)) == DetectedSource("WEB", "Qobuz URL in the tags")


def _prompt(monkeypatch, answer: str) -> dict:
    """Answer the source prompt with `answer`, as click does: an empty answer takes the default."""
    asked: dict = {}

    async def fake_prompt(text: str, default: str = "", **_kwargs) -> str:
        asked["default"] = default
        return answer or default

    monkeypatch.setattr(salmon.uploader.click, "prompt", fake_prompt)
    return asked


def test_the_prompt_offers_the_detected_source_and_says_why(monkeypatch, capsys) -> None:
    asked = _prompt(monkeypatch, "")

    source = anyio.run(salmon.uploader._prompt_source, DetectedSource("WEB", "Qobuz URL in the tags"))

    assert source == "WEB"
    assert asked["default"] == "WEB"
    assert "Qobuz URL in the tags" in capsys.readouterr().out


def test_the_user_can_still_answer_another_source(monkeypatch) -> None:
    _prompt(monkeypatch, "cd")

    assert anyio.run(salmon.uploader._prompt_source, DetectedSource("WEB", "Qobuz URL in the tags")) == "CD"


def test_without_a_detection_the_prompt_is_unchanged(monkeypatch, capsys) -> None:
    asked = _prompt(monkeypatch, "vinyl")

    assert anyio.run(salmon.uploader._prompt_source, None) == "Vinyl"
    assert asked["default"] == ""
    assert "The files say" not in capsys.readouterr().out


class _SourceSettled(Exception):
    """Raised by the first step after the source is settled, to stop the upload there."""


def _upload_until_the_source_is_settled(monkeypatch, source: str | None) -> tuple[list[str], list]:
    """Run _upload_staged up to the source step. Returns the paths detected and the detections prompted with."""
    detected_paths: list[str] = []
    prompted_with: list = []
    detection = DetectedSource("WEB", "Qobuz URL in the tags")

    def fake_detect_source(path: str) -> DetectedSource:
        detected_paths.append(path)
        return detection

    async def fake_prompt_source(detected=None) -> str:
        prompted_with.append(detected)
        return "WEB"

    def settled(*_args, **_kwargs):
        raise _SourceSettled

    monkeypatch.setattr(salmon.uploader, "detect_source", fake_detect_source)
    monkeypatch.setattr(salmon.uploader, "_prompt_source", fake_prompt_source)
    monkeypatch.setattr(salmon.uploader, "gather_audio_info", settled)
    with pytest.raises(_SourceSettled):
        anyio.run(
            partial(
                salmon.uploader._upload_staged,
                RedApi(),
                "/staged/album",
                None,
                source,
                None,
                (),
                None,
                scene=False,
                overwrite_meta=False,
                recompress=False,
                source_url=None,
                searchstrs=None,
                request_id=None,
                spectrals_after=False,
                auto_rename=False,
                skip_up=False,
                skip_mqa=False,
                skip_log_check=False,
                skip_integrity_check=False,
                essential_only=False,
                flac_group=None,
                skip_initial_review=False,
                apply_ai_suggestions=False,
                rename_into=None,
                library_album=None,
            )
        )
    return detected_paths, prompted_with


def test_the_upload_offers_what_it_detects_in_the_staged_folder(monkeypatch) -> None:
    detected_paths, prompted_with = _upload_until_the_source_is_settled(monkeypatch, None)

    assert detected_paths == ["/staged/album"]
    assert prompted_with == [DetectedSource("WEB", "Qobuz URL in the tags")]


def test_a_known_source_skips_the_detection_and_the_prompt(monkeypatch) -> None:
    detected_paths, prompted_with = _upload_until_the_source_is_settled(monkeypatch, "CD")

    assert detected_paths == []
    assert prompted_with == []
