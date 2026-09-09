"""Without an account, Qobuz metadata comes from the public album page, read into the API's shape."""

import anyio
import pytest
from bs4 import BeautifulSoup

from salmon import cfg
from salmon.sources import qobuz as qobuz_base
from salmon.tagger.sources import qobuz
from salmon.tagger.sources.qobuz import Scraper, page_to_api_shape

QOBUZ_URL = "https://www.qobuz.com/au-en/album/journaling-illy/ul39e7xjbuqrb"
ENGLISH_PAGE = "https://www.qobuz.com/gb-en/album/-/ul39e7xjbuqrb"


def _track(number: int, title: str, duration: str, performers: str, copyright: str) -> str:
    return f"""
    <div class="track " data-track="{159285677 + number}">
      <div class="track__items" title="{title}">
        <div class="track__item track__item--number"><span>{number}</span></div>
        <div class="track__item track__item--name" itemprop="name"><span>{title}</span></div>
        <span class="track__item track__item--duration">{duration}</span>
      </div>
      <div class="track__infos">
        <p class="track__info">{performers}</p>
        <p class="track__info">{copyright}</p>
      </div>
    </div>"""


def _page(tracks_html: str, about_count: str = "1 disc(s) - 6 track(s)") -> str:
    # Mirrors the markup of a real Qobuz album page, trimmed to the parts that carry data.
    return f"""<html><head>
    <meta property="og:title" content="journaling, Illy - Qobuz">
    <meta property="og:image" content="https://static.qobuz.com/images/covers/rb/uq/ul39e7xjbuqrb_600.jpg">
    </head><body>
    <div class="album-meta">
      <h1 class="album-meta__title" title="journaling by Illy">
        <span class="album-title">journaling</span> <span class="album-connector">by</span>
        <span class="artist-name">Illy</span>
      </h1>
      <ul class="album-meta__items">
        <li class="album-meta__item"> Released on 24/5/22 by
          <a class="album-meta__link" href="/l">3285978 Records DK</a> </li>
        <li class="album-meta__item"> Main artists:
          <a class="album-meta__link" href="/a" title="Illy">Illy</a> </li>
        <li class="album-meta__item">Genre: <a class="album-meta__link" href="/g">Alternative &amp; Indie</a> </li>
      </ul>
      <div class="album-quality"><span class="album-quality__info">24-Bit/44.1 kHz</span></div>
    </div>
    <section class="album-about" id="about">
      <ul class="album-about__items">
        <li class="album-about__item">{about_count}</li>
        <li class="album-about__item">Total length: <span class="album-about__item--duration">00:12:15</span></li>
      </ul>
      <ul class="album-about__items">
        <li class="album-about__item">Label:
          <a class="album-about__item album-about__item--link" href="/l">3285978 Records DK</a></li>
        <li class="album-about__item">Genre:
          <a class="album-about__item album-about__item--link" href="/g1">Pop/Rock</a>
          <a class="album-about__item album-about__item--link" href="/g2">Rock</a>
          <a class="album-about__item album-about__item--link" href="/g3">Alternative &amp; Indie</a>
        </li>
      </ul>
    </section>
    <div class="player__tracks">{tracks_html}</div>
    </body></html>"""


PERFORMERS = "Illy, MainArtist - Ileàna Justice, Composer"
COPYRIGHT = "2022 3285978 Records DK"
SINGLE_DISC = _page(
    _track(1, "walking life", "00:00:47", PERFORMERS, COPYRIGHT) + _track(2, "muney", "00:02:08", PERFORMERS, COPYRIGHT)
)


def test_page_to_api_shape_reads_the_album_facts() -> None:
    data = page_to_api_shape(BeautifulSoup(SINGLE_DISC, "lxml"))

    assert data["title"] == "journaling"
    assert data["artist"] == {"name": "Illy"}
    assert data["artists"] == [{"name": "Illy", "roles": ["main-artist"]}]
    assert data["label"] == {"name": "3285978 Records DK"}
    assert data["release_date_original"] == "2022-05-24"
    assert data["copyright"] == COPYRIGHT
    assert data["genres_list"] == ["Pop/Rock", "Rock", "Alternative & Indie"]
    assert data["tracks_count"] == 6
    assert data["image"] == {"large": "https://static.qobuz.com/images/covers/rb/uq/ul39e7xjbuqrb_max.jpg"}
    assert data["upc"] is None
    assert data["tracks"]["items"] == [
        {
            "media_number": 1,
            "track_number": 1,
            "title": "walking life",
            "duration": 47,
            "performers": PERFORMERS,
            "copyright": COPYRIGHT,
        },
        {
            "media_number": 1,
            "track_number": 2,
            "title": "muney",
            "duration": 128,
            "performers": PERFORMERS,
            "copyright": COPYRIGHT,
        },
    ]


def test_a_four_digit_year_reads_the_same() -> None:
    soup = BeautifulSoup(SINGLE_DISC.replace("Released on 24/5/22", "Released on 24/05/2022"), "lxml")

    assert page_to_api_shape(soup)["release_date_original"] == "2022-05-24"


def test_smaller_cover_sizes_are_lifted_to_the_maximum() -> None:
    soup = BeautifulSoup(SINGLE_DISC.replace("_600.jpg", "_230.jpg"), "lxml")

    assert page_to_api_shape(soup)["image"] == {"large": "https://static.qobuz.com/images/covers/rb/uq/ul39e7xjbuqrb_max.jpg"}


def test_a_disc_heading_moves_the_following_tracks_to_that_disc() -> None:
    html = _page(
        _track(1, "one", "00:01:00", PERFORMERS, COPYRIGHT)
        + '<h3 class="player__disc">Disc 2</h3>'
        + _track(1, "two", "00:01:00", PERFORMERS, COPYRIGHT),
        about_count="2 disc(s) - 2 track(s)",
    )

    items = page_to_api_shape(BeautifulSoup(html, "lxml"))["tracks"]["items"]

    assert [(track["media_number"], track["track_number"], track["title"]) for track in items] == [
        (1, 1, "one"),
        (2, 1, "two"),
    ]


def test_the_existing_parsers_read_the_page_shape() -> None:
    scraper = Scraper()
    data = page_to_api_shape(BeautifulSoup(SINGLE_DISC, "lxml"))

    assert scraper.parse_release_title(data) == "journaling"
    assert scraper.parse_release_group_year(data) == 2022
    assert scraper.parse_release_label(data) == "3285978 Records DK"
    assert scraper.parse_cover_url(data) == "https://static.qobuz.com/images/covers/rb/uq/ul39e7xjbuqrb_max.jpg"
    assert {"Pop", "Rock"} <= set(scraper.parse_genres(data))
    tracks = anyio.run(scraper.parse_tracks, data)
    assert tracks["1"]["1"]["title"] == "walking life"
    assert ("Illy", "main") in tracks["1"]["1"]["artists"]


@pytest.mark.parametrize(
    "pasted",
    [
        QOBUZ_URL,
        "https://www.qobuz.com/de-de/album/journaling-illy/ul39e7xjbuqrb",
        "https://www.qobuz.com/us-en/album/journaling-illy/ul39e7xjbuqrb",
        "https://open.qobuz.com/album/ul39e7xjbuqrb",
    ],
)
def test_fetch_data_reads_the_english_page_whatever_locale_was_pasted(monkeypatch, pasted: str) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", None)
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", None)
    fetched: list[str] = []

    async def fake_page(_self, url, *_args, **_kwargs):
        fetched.append(url)
        return BeautifulSoup(SINGLE_DISC, "lxml")

    async def no_api(*_args, **_kwargs):
        raise AssertionError("the API must not be called without credentials")

    monkeypatch.setattr(qobuz_base.QobuzBase, "fetch_page", fake_page)
    monkeypatch.setattr(qobuz_base.QobuzBase, "get_json", no_api)

    data = anyio.run(Scraper().fetch_data, pasted)

    assert fetched == [ENGLISH_PAGE]
    assert data["title"] == "journaling"


def test_fetch_data_rejects_a_page_without_album_data(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", None)
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", None)

    async def empty_page(_self, _url, *_args, **_kwargs):
        return BeautifulSoup("<html><body><p>Not an album</p></body></html>", "lxml")

    async def no_api(*_args, **_kwargs):
        raise AssertionError("the API must not be called without credentials")

    monkeypatch.setattr(qobuz_base.QobuzBase, "fetch_page", empty_page)
    monkeypatch.setattr(qobuz_base.QobuzBase, "get_json", no_api)

    with pytest.raises(qobuz.ScrapeError, match="no album data"):
        anyio.run(Scraper().fetch_data, QOBUZ_URL)


def test_fetch_data_still_uses_the_api_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "app-123")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "token-456")

    async def api(_self, _url, params=None, headers=None):
        return {"title": "from the api"}

    async def no_page(*_args, **_kwargs):
        raise AssertionError("the page must not be fetched when the API is configured")

    monkeypatch.setattr(qobuz_base.QobuzBase, "get_json", api)
    monkeypatch.setattr(qobuz_base.QobuzBase, "fetch_page", no_page)

    data = anyio.run(Scraper().fetch_data, QOBUZ_URL)

    assert data == {"title": "from the api"}
