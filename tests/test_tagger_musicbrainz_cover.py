"""The MusicBrainz release's cover, from recorded Cover Art Archive answers (#1065).

The answers are in tests/fixtures/stores/musicbrainz/caa/. Moving the client to the JSON API must leave these as
they are.
"""

import json
from pathlib import Path
from typing import Any

import musicbrainzngs
import pytest
from musicbrainzngs import mbxml

from salmon.tagger.sources import METASOURCES

RECORDINGS = Path(__file__).parent / "fixtures" / "stores" / "musicbrainz"
KIND_OF_BLUE = "4c8af71f-5746-4981-92d7-0d6c64db1155"
LED_ZEPPELIN_II = "0d1fc06f-4dcb-45b0-8fae-33deb44f46ed"
RELAXIN = "3fb119cb-5595-33cf-b192-743c17c7f584"


def _release(mbid: str) -> dict[str, Any]:
    return mbxml.parse_message((RECORDINGS / "release" / f"{mbid}.xml").read_bytes())["release"]


def _cover(mbid: str) -> str | None:
    return METASOURCES["MusicBrainz"].Scraper().parse_cover_url(_release(mbid))


def _serve_recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def _image_list(mbid: str) -> dict[str, Any]:
        asked.append(mbid)
        return json.loads((RECORDINGS / "caa" / f"{mbid}.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(musicbrainzngs, "get_image_list", _image_list)
    return asked


@pytest.mark.parametrize(
    ("mbid", "image"),
    [
        (KIND_OF_BLUE, f"https://coverartarchive.org/release/{KIND_OF_BLUE}/23058063062.png"),
        (LED_ZEPPELIN_II, f"http://coverartarchive.org/release/{LED_ZEPPELIN_II}/35234208580.jpg"),
    ],
)
def test_front_cover_is_the_approved_front_image(mbid: str, image: str, monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _serve_recorded(monkeypatch)
    assert _cover(mbid) == image
    assert asked == [mbid]


def test_no_front_cover_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _serve_recorded(monkeypatch)
    assert _release(RELAXIN)["cover-art-archive"]["front"] == "false"
    assert _cover(RELAXIN) is None
    assert asked == []


def test_a_404_from_the_cover_art_archive_is_no_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    def _not_found(mbid: str) -> dict[str, Any]:
        raise musicbrainzngs.ResponseError(cause=None)

    monkeypatch.setattr(musicbrainzngs, "get_image_list", _not_found)
    assert _cover(KIND_OF_BLUE) is None
