import json
from types import SimpleNamespace
from typing import Any, cast

import anyio
import asyncclick as click
import pytest
from bs4 import BeautifulSoup

from salmon.search import beatport


def _answer(doc):
    async def get_json(*_args, **_kwargs):
        return doc

    return get_json


def _shown(result) -> str:
    """The one result's line, without its colours."""
    ((_, shown),) = result[1].values()
    return click.unstyle(shown)


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
