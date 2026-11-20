"""The MusicBrainz release's cover, from recorded Cover Art Archive answers (#1065).

The answers are in tests/fixtures/stores/musicbrainz/caa/. Moving the client to the JSON API must leave these as
they are.
"""

import json
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from helpers.musicbrainz import RECORDINGS, FakeMusicBrainz, serving_musicbrainz

from salmon.sources import musicbrainz
from salmon.tagger.sources.musicbrainz import Scraper

KIND_OF_BLUE = "4c8af71f-5746-4981-92d7-0d6c64db1155"
LED_ZEPPELIN_II = "0d1fc06f-4dcb-45b0-8fae-33deb44f46ed"
RELAXIN = "3fb119cb-5595-33cf-b192-743c17c7f584"
# Recorded, but with no Cover Art Archive answer: the fake answers 404.
NO_CAA_ANSWER = "09652847-76db-4f26-b22c-8f74192eb219"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMusicBrainz]:
    with serving_musicbrainz(monkeypatch) as fake:
        yield fake


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(musicbrainz, "RETRY_WAIT", 0)


def _release(mbid: str) -> dict[str, Any]:
    return json.loads((RECORDINGS / "release" / f"{mbid}.json").read_text(encoding="utf-8"))


def _cover(release: dict[str, Any]) -> str | None:
    return anyio.run(Scraper().front_cover, release)


def _asked(server: FakeMusicBrainz) -> list[str]:
    return [path for path, _query, _headers in server.requests]


@pytest.mark.parametrize(
    ("mbid", "image"),
    [
        (KIND_OF_BLUE, f"https://coverartarchive.org/release/{KIND_OF_BLUE}/23058063062.png"),
        (LED_ZEPPELIN_II, f"http://coverartarchive.org/release/{LED_ZEPPELIN_II}/35234208580.jpg"),
    ],
)
def test_front_cover_is_the_approved_front_image(mbid: str, image: str, server: FakeMusicBrainz) -> None:
    cover = _cover(_release(mbid))
    assert cover == image
    assert _asked(server) == [f"/release/{mbid}"]


def test_no_front_cover_makes_no_request(server: FakeMusicBrainz) -> None:
    release = _release(RELAXIN)
    assert release["cover-art-archive"]["front"] is False
    assert _cover(release) is None
    assert server.requests == []


def test_a_release_claiming_a_front_image_gets_none_when_no_listed_image_is_front(server: FakeMusicBrainz) -> None:
    release = _release(RELAXIN)
    release["cover-art-archive"]["front"] = True
    assert _cover(release) is None
    assert _asked(server) == [f"/release/{RELAXIN}"]


def test_a_404_from_the_cover_art_archive_is_no_cover(server: FakeMusicBrainz, capsys: pytest.CaptureFixture) -> None:
    release = {"id": NO_CAA_ANSWER, "cover-art-archive": {"front": True}}

    assert _cover(release) is None
    assert capsys.readouterr().out == ""


def test_a_failed_cover_art_archive_request_is_no_cover_and_says_so(
    server: FakeMusicBrainz, capsys: pytest.CaptureFixture
) -> None:
    # A Cover Art Archive that fails costs the cover, not the release.
    server.status = [503, 503]

    assert _cover(_release(KIND_OF_BLUE)) is None
    assert "Cover Art Archive request failed: no cover." in capsys.readouterr().out
    assert len(server.requests) == 2


def test_a_cover_art_archive_answer_that_is_not_json_is_no_cover_and_says_so(
    server: FakeMusicBrainz, capsys: pytest.CaptureFixture
) -> None:
    # A 200 with a page in place of the image list costs the cover too.
    server.body = "<html>Not the image list</html>"

    assert _cover(_release(KIND_OF_BLUE)) is None
    assert "Cover Art Archive request failed: no cover." in capsys.readouterr().out
    assert len(server.requests) == 1


def test_a_fetched_release_carries_its_front_cover(server: FakeMusicBrainz) -> None:
    scraper = Scraper()

    release = anyio.run(scraper.fetch_data, f"https://musicbrainz.org/release/{KIND_OF_BLUE}?tab=tracks")

    assert release["id"] == KIND_OF_BLUE
    assert scraper.parse_cover_url(release) == (f"https://coverartarchive.org/release/{KIND_OF_BLUE}/23058063062.png")
    (path, query, _headers), (caa_path, _, _) = server.requests
    assert path == f"/ws/2/release/{KIND_OF_BLUE}"
    assert query["inc"].split() == list(musicbrainz.INCLUDES)
    assert caa_path == f"/release/{KIND_OF_BLUE}"


def test_a_link_that_is_not_a_musicbrainz_release_is_refused() -> None:
    with pytest.raises(ValueError, match="Invalid MusicBrainz URL"):
        anyio.run(Scraper().fetch_data, "https://musicbrainz.org/artist/1")
