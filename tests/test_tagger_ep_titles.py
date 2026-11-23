import anyio
import pytest

from salmon import cfg
from salmon.tagger.sources.apple_music import Scraper as AppleMusicScraper
from salmon.tagger.sources.qobuz import Scraper as QobuzScraper


def release(title: str) -> dict:
    tracks = {"1": {str(n): {"title": f"Track {n}"} for n in range(1, 5)}}
    return {"title": title, "rls_type": None, "tracks": tracks, "artists": [("An Artist", "main")]}


def title_and_type(title: str) -> tuple[str, str]:
    return AppleMusicScraper().determine_rls_type(release(title))


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space EP", "Space"),
        ("Space E.P.", "Space"),
        ("Space - EP", "Space"),
        ("Space (EP)", "Space"),
        ("Space [EP]", "Space"),
        ("The EP Sessions Vol. 2", "The EP Sessions Vol. 2"),
        ("EP 1", "EP 1"),
        ("EP", "EP"),
    ],
)
def test_only_the_ep_ending_the_title_is_dropped(title: str, expected: str) -> None:
    assert title_and_type(title) == (expected, "EP")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space EP", "Space EP"),
        ("Space E.P.", "Space E.P."),
        ("Space - EP", "Space"),
        ("Space (EP)", "Space"),
        ("The EP Sessions Vol. 2", "The EP Sessions Vol. 2"),
    ],
)
def test_ep_ending_the_title_is_kept_when_not_stripped(monkeypatch, title: str, expected: str) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_ep_from_title", False)

    assert title_and_type(title) == (expected, "EP")


@pytest.mark.parametrize("strip", [True, False])
def test_qobuz_title_without_a_release_type_follows_the_setting(monkeypatch, strip: bool) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_ep_from_title", strip)
    soups = [{"release_type": "", "title": "Space EP"}, {"release_type": "", "title": "Space - EP"}]

    assert [QobuzScraper().parse_release_type(soup) for soup in soups] == ["EP", "EP"]
    assert [soup["title"] for soup in soups] == ["Space" if strip else "Space EP", "Space"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space – EP", "Space"),
        ("Space — EP", "Space"),
        ("MAX-EP", "MAX-EP"),
        ("Super-EP", "Super-EP"),
    ],
)
def test_a_store_marker_takes_any_dash_after_a_space(title: str, expected: str) -> None:
    assert title_and_type(title)[0] == expected


@pytest.mark.parametrize("title", ["Deep", "Sweep", "Steep - Remixes"])
def test_qobuz_title_ending_in_ep_letters_is_no_ep(title: str) -> None:
    soup = {"release_type": "", "title": title}

    rls_type = QobuzScraper().parse_release_type(soup)

    assert rls_type != "EP"
    assert soup["title"] == title


@pytest.mark.parametrize(
    ("strip", "title", "expected"),
    [(True, "Space EP", "Space"), (False, "Space EP", "Space EP"), (False, "Space - EP", "Space")],
)
def test_qobuz_explicit_ep_title_follows_the_setting(monkeypatch, strip: bool, title: str, expected: str) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_ep_from_title", strip)

    assert QobuzScraper().determine_rls_type({"title": title, "rls_type": "EP"}) == (expected, "EP")


def twelve_tracks(title: str) -> dict:
    """A store's release of 12 tracks the store calls an album: only its title can say otherwise."""
    tracks = {"1": {str(n): {"title": f"Track {n}"} for n in range(1, 13)}}
    return {"title": title, "rls_type": "Album", "tracks": tracks, "artists": [("An Artist", "main")]}


@pytest.mark.parametrize("title", ["Space (Single)", "Space [Single]", "Space (single)", "Space – Single"])
def test_a_single_set_apart_ending_the_title_is_cut_and_says_the_type(title: str) -> None:
    assert AppleMusicScraper().determine_rls_type(twelve_tracks(title)) == ("Space", "Single")


@pytest.mark.parametrize("title", ["Forever Single", "Wholesingle", "Single", "- Single"])
def test_a_single_not_set_apart_is_part_of_the_name(title: str) -> None:
    assert AppleMusicScraper().determine_rls_type(twelve_tracks(title)) == (title, "Album")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space - Single", ("Space", "Single")),
        ("Space -Single", ("Space", "Single")),
        ("The Singles", ("The Singles", "Album")),
        ("Forever Single Mothers", ("Forever Single Mothers", "Album")),
    ],
)
def test_as_before_a_title_ending_in_a_dash_and_single_is_a_single(title: str, expected: tuple[str, str]) -> None:
    assert AppleMusicScraper().determine_rls_type(twelve_tracks(title)) == expected


def apple_type(name: str) -> str:
    return AppleMusicScraper().parse_release_type({"album": {"name": name, "trackCount": 12}})


def test_apple_reads_the_type_from_a_single_set_apart_and_not_from_a_name() -> None:
    assert [apple_type(name) for name in ("Space (Single)", "Space – Single", "Forever Single")] == [
        "Single",
        "Single",
        "Album",
    ]


def test_as_before_apple_reads_the_type_from_a_dash_and_single() -> None:
    assert [apple_type(name) for name in ("Space - Single", "Space")] == ["Single", "Album"]


def qobuz_title_and_type(title: str) -> tuple[str, str]:
    soup = {"release_type": "", "title": title, "tracks_count": 12}
    rls_type = QobuzScraper().parse_release_type(soup)
    return soup["title"], rls_type


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space (Single)", ("Space", "Single")),
        ("Space – Single", ("Space", "Single")),
        ("Space - Single", ("Space", "Single")),
        ("Forever Single", ("Forever Single", "Album")),
    ],
)
def test_qobuz_cuts_a_single_set_apart_and_keeps_one_in_the_name(title: str, expected: tuple[str, str]) -> None:
    assert qobuz_title_and_type(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Space (Deluxe Edition) – Single",
        "Space (Deluxe Edition) (EP)",
        "Space (Deluxe Edition) [Single]",
        "Space (Deluxe Edition) EP",
    ],
)
def test_qobuz_finds_the_edition_before_any_release_type_marker(title: str) -> None:
    assert QobuzScraper().parse_edition_title({"title": title}) == "Deluxe Edition"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space (Deluxe Edition) - Single", "Deluxe Edition"),
        ("Space (Deluxe Edition)", "Deluxe Edition"),
        ("Space (EP)", None),
        ("Space - Single", None),
    ],
)
def test_as_before_qobuz_finds_the_edition_ending_the_title(title: str, expected: str | None) -> None:
    assert QobuzScraper().parse_edition_title({"title": title}) == expected


@pytest.mark.parametrize("title", ["Space - Single", "Space (Single)"])
def test_as_before_an_ep_cut_leaves_a_single_marker_alone(title: str) -> None:
    assert QobuzScraper().determine_rls_type({"title": title, "rls_type": "EP"}) == (title, "EP")


def qobuz_scraped_title_and_type(title: str, release_type: str, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """The title and release type of a whole Qobuz scrape of a one-track release."""
    album = {
        "id": "x",
        "title": title,
        "release_type": release_type,
        "tracks_count": 1,
        "artist": {"name": "Alice"},
        "artists": [{"name": "Alice", "roles": ["main-artist"]}],
        "genres_list": ["Pop/Rock"],
        "release_date_original": "2026-01-01",
        "tracks": {"items": [{"track_number": 1, "title": "Space"}]},
    }
    scraper = QobuzScraper()

    async def fetch_data(*_args: object, **_kwargs: object) -> dict:
        return album

    monkeypatch.setattr(scraper, "fetch_data", fetch_data)
    scraped = anyio.run(scraper.scrape_release, "https://www.qobuz.com/album/space/x")
    return scraped["title"], scraped["rls_type"]


@pytest.mark.parametrize(
    ("title", "release_type"),
    [("Space (Single)", ""), ("Space - Single", ""), ("Space - Single", "single"), ("Space (Single)", "single")],
)
def test_a_qobuz_scrape_returns_a_single_s_title_without_its_marker(
    title: str, release_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    scraped = qobuz_scraped_title_and_type(title, release_type, monkeypatch)

    assert scraped == ("Space", "Single")


def test_as_before_a_qobuz_single_named_with_single_in_it_keeps_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    scraped = qobuz_scraped_title_and_type("Forever Single", "single", monkeypatch)

    assert scraped == ("Forever Single", "Single")
