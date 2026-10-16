import json
from types import SimpleNamespace
from typing import Any, cast

import anyio
import asyncclick as click
import pytest
from bs4 import BeautifulSoup

from salmon import cfg
from salmon.search import bandcamp, beatport, discogs, qobuz
from salmon.search.base import SearchMixin

BANDCAMP_PAGE = """
<ul class="result-items"><li class="searchresult data-search"><div class="result-info">
  <div class="heading"><a>Test Album</a></div>
  <div class="subhead">by Test Artist</div>
  <div class="length">3 tracks</div>
  <div class="released">released </div>
  <div class="itemurl"><a>https://testartist.bandcamp.com/album/test-album</a></div>
</div></li></ul>
"""


def _answer(doc):
    async def get_json(*_args, **_kwargs):
        return doc

    return get_json


def _shown(result) -> str:
    """The one result's line, without its colours."""
    ((_, shown),) = result[1].values()
    return click.unstyle(shown)


def test_edition_joins_only_the_parts_present() -> None:
    assert SearchMixin.edition(2020, None, "", "Test Label") == "2020 Test Label"
    assert SearchMixin.edition(None, None) == ""


def test_bandcamp_result_without_a_year_shows_no_none(monkeypatch) -> None:
    async def fetch_page(*_args, **_kwargs):
        return BeautifulSoup(BANDCAMP_PAGE, "lxml")

    monkeypatch.setattr(bandcamp.Searcher, "fetch_page", fetch_page)

    shown = _shown(anyio.run(bandcamp.Searcher().search_releases, "test album", 5))

    assert shown == "Test Artist - Test Album {Tracks: 3} testartist"


def test_discogs_result_without_a_year_source_or_label_shows_no_none(monkeypatch) -> None:
    rls = {"id": 1, "title": "Test Artist - Test Album", "format": ["Box Set"], "user_data": {"in_collection": False}}
    monkeypatch.setattr(discogs.Searcher, "get_json", _answer({"results": [rls]}))

    shown = _shown(anyio.run(discogs.Searcher().search_releases, "test album", 5))

    assert shown == "Test Artist - Test Album {Box Set} Not On Label"


def test_discogs_result_with_a_label_but_no_catalogue_number_shows_no_none(monkeypatch) -> None:
    rls = {
        "id": 1,
        "title": "Test Artist - Test Album",
        "year": "2020",
        "format": ["CD"],
        "label": ["Test Label"],
        "user_data": {"in_collection": False},
    }
    monkeypatch.setattr(discogs.Searcher, "get_json", _answer({"results": [rls]}))

    shown = _shown(anyio.run(discogs.Searcher().search_releases, "test album", 5))

    assert shown == "Test Artist - Test Album {CD} 2020 CD Test Label"


def test_qobuz_result_without_a_year_shows_no_none(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "test-app")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "test-user")
    rls = {"id": "a1", "artist": {"name": "Test Artist"}, "title": "Test Album", "tracks_count": 3}
    monkeypatch.setattr(qobuz.Searcher, "get_json", _answer({"albums": {"items": [rls]}}))

    shown = _shown(anyio.run(qobuz.Searcher().search_releases, "test album", 5))

    assert shown == "Test Artist - Test Album {Tracks: 3}"


BEATPORT_RELEASE = {
    "id": 1,
    "artists": [{"name": "Test Artist"}],
    "name": "Test Album",
    "publish_date": "2020-01-31",
    "track_count": 3,
    "is_explicit": False,
    "label": {"name": "Test Label"},
    "catalog_number": None,
    "upc": "000000000000",
}


@pytest.mark.parametrize(
    ("missing", "shown"),
    [
        ({"catalog_number": None}, "Test Artist - Test Album {Tracks: 3} Test Label"),
        ({"catalog_number": ...}, "Test Artist - Test Album {Tracks: 3} Test Label"),
        ({"label": None}, "Test Artist - Test Album {Tracks: 3}"),
        ({"label": ...}, "Test Artist - Test Album {Tracks: 3}"),
        ({"upc": ...}, "Test Artist - Test Album {Tracks: 3} Test Label"),
    ],
    ids=["null catno", "no catno", "null label", "no label", "no upc"],
)
def test_beatport_result_missing_a_label_or_catalogue_number_is_listed(monkeypatch, missing, shown) -> None:
    # ... removes the key; None sends it as null.
    release = {key: value for key, value in {**BEATPORT_RELEASE, **missing}.items() if value is not ...}
    monkeypatch.setattr(beatport.Searcher, "get_json", _answer({"releases": [release]}))
    token = cast("Any", SimpleNamespace(get_auth_header=lambda: {}))

    line = _shown(anyio.run(beatport.Searcher().search_releases_api, token, "test album", 5))

    assert line == shown


def test_beatport_scraped_result_without_a_label_is_listed(monkeypatch) -> None:
    found = [
        {
            "release_id": 1,
            "artists": [{"artist_name": "Test Artist", "artist_type_name": "Artist"}],
            "release_name": "Test Album",
        }
    ]
    data = {"props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": {"data": found}}}]}}}}

    async def fetch_page(*_args, **_kwargs):
        return BeautifulSoup(f'<script id="__NEXT_DATA__">{json.dumps(data)}</script>', "lxml")

    monkeypatch.setattr(beatport.Searcher, "fetch_page", fetch_page)

    line = _shown(anyio.run(beatport.Searcher().search_releases_scraping, "test album", 5))

    assert line == "Test Artist - Test Album"
