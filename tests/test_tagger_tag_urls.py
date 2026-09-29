"""Reading the store URL a release's own files carry in their tags (#545)."""

import struct

from mutagen.flac import FLAC
from mutagen.id3 import WOAS, WXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import AtomDataType, MP4FreeForm

from salmon.tagger import tag_urls as tag_urls_mod
from salmon.tagger.tag_urls import tag_urls

DEEZER_URL = "https://www.deezer.com/album/322064097"


def _write_flac(path, tags: dict[str, str]) -> None:
    """Write a FLAC file with no audio: just a STREAMINFO block, tagged with `tags`."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    mut = FLAC(path)
    for key, value in tags.items():
        mut[key] = value
    mut.save()


def _write_mp3(path) -> MP3:
    """Write a minimal single-frame MP3 (silent, no real audio) and return it with tags attached."""
    frame_header = bytes([0xFF, 0xFB, 0x90, 0x00])
    frame_size = 417
    frame = frame_header + bytes(frame_size - len(frame_header))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * 3)
    mut = MP3(path)
    mut.add_tags()
    return mut


def test_flac_source_key_is_read_as_a_sourced_url(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", {"SOURCE": DEEZER_URL})

    sourced, other = tag_urls(str(tmp_path))

    assert sourced == [DEEZER_URL]
    assert other == []


def test_mp3_wxxx_and_woas_are_read_as_sourced_urls(tmp_path) -> None:
    mut = _write_mp3(tmp_path / "01.mp3")
    assert mut.tags is not None
    mut.tags.add(WXXX(encoding=3, desc="URL", url=DEEZER_URL))
    mut.tags.add(WOAS(encoding=3, url="https://www.qobuz.com/album/other-id"))
    mut.save()

    sourced, other = tag_urls(str(tmp_path))

    assert set(sourced) == {DEEZER_URL, "https://www.qobuz.com/album/other-id"}
    assert other == []


class _FakeM4a:
    def __init__(self, tags: dict) -> None:
        self.tags = tags


def test_m4a_freeform_source_key_is_read_as_a_sourced_url(tmp_path, monkeypatch) -> None:
    (tmp_path / "01.m4a").write_bytes(b"")
    fake_tags = {"----:com.apple.iTunes:SOURCE": [MP4FreeForm(DEEZER_URL.encode())]}
    monkeypatch.setattr(tag_urls_mod, "MutagenFile", lambda _path: _FakeM4a(fake_tags))

    sourced, other = tag_urls(str(tmp_path))

    assert sourced == [DEEZER_URL]
    assert other == []


def test_m4a_freeform_utf16_value_is_decoded_as_utf16(tmp_path, monkeypatch) -> None:
    """A UTF-16 freeform value decoded as UTF-8 comes out mangled and fails the URL match (CodeRabbit, #562)."""
    (tmp_path / "01.m4a").write_bytes(b"")
    fake_tags = {
        "----:com.apple.iTunes:SOURCE": [MP4FreeForm(DEEZER_URL.encode("utf-16"), dataformat=AtomDataType.UTF16)]
    }
    monkeypatch.setattr(tag_urls_mod, "MutagenFile", lambda _path: _FakeM4a(fake_tags))

    sourced, other = tag_urls(str(tmp_path))

    assert sourced == [DEEZER_URL]
    assert other == []


def test_musicbrainz_url_is_ignored(tmp_path) -> None:
    _write_flac(
        tmp_path / "01.flac",
        {"musicbrainz_albumid": "https://musicbrainz.org/release/11111111-1111-1111-1111-111111111111"},
    )

    sourced, other = tag_urls(str(tmp_path))

    assert sourced == []
    assert other == []


def test_url_under_a_non_source_key_lands_in_other(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", {"comment": DEEZER_URL})

    sourced, other = tag_urls(str(tmp_path))

    assert sourced == []
    assert other == [DEEZER_URL]
