"""A store link with a query or fragment stays its store's: Beatport, Qobuz and MusicBrainz read it as Deezer does."""

import pytest

from salmon.sources.beatport import BeatportBase
from salmon.sources.musicbrainz import MusicBrainzBase
from salmon.sources.qobuz import QobuzBase
from salmon.tagger.metadata import _album_identity
from salmon.tagger.sources import matching_source

BEATPORT = "https://www.beatport.com/release/some-release/1234567"
QOBUZ = "https://www.qobuz.com/us-en/album/some-album-some-artist/abc123xyz"
MUSICBRAINZ_ID = "0a1b2c3d-0000-0000-0000-000000000000"
MUSICBRAINZ = f"https://musicbrainz.org/release/{MUSICBRAINZ_ID}"

LINKS = [
    pytest.param(BeatportBase.regex, BEATPORT, "1234567", id="beatport"),
    pytest.param(
        BeatportBase.regex,
        "https://classic.beatport.com/release/some-release/1234567",
        "1234567",
        id="beatport-classic",
    ),
    pytest.param(QobuzBase.regex, QOBUZ, "abc123xyz", id="qobuz"),
    pytest.param(QobuzBase.regex, "https://open.qobuz.com/album/abc123xyz", "abc123xyz", id="qobuz-open"),
    pytest.param(QobuzBase.regex, "https://play.qobuz.com/album/abc123xyz", "abc123xyz", id="qobuz-play"),
    pytest.param(QobuzBase.regex, "https://www.qobuz.com/album/-/abc123xyz", "abc123xyz", id="qobuz-search-form"),
    pytest.param(MusicBrainzBase.regex, MUSICBRAINZ, MUSICBRAINZ_ID, id="musicbrainz"),
    pytest.param(
        MusicBrainzBase.regex,
        f"https://www.musicbrainz.org/release/{MUSICBRAINZ_ID}",
        MUSICBRAINZ_ID,
        id="musicbrainz-www",
    ),
]


@pytest.mark.parametrize(("regex", "url", "release_id"), LINKS)
def test_a_link_with_a_query_or_fragment_reads_as_the_same_release(regex, url, release_id) -> None:
    # The last form: a slash and digits in the query must not be read as the release ID.
    forms = [url, f"{url}/", f"{url}?utm_source=x", f"{url}/?utm_source=x", f"{url}#tracks", f"{url}?from=/987"]

    assert [match[1] if (match := regex.match(form)) else None for form in forms] == [release_id] * len(forms)


@pytest.mark.parametrize(
    ("regex", "others"),
    [
        pytest.param(BeatportBase.regex, [f"https://example.com/?u={BEATPORT}"], id="beatport"),
        pytest.param(QobuzBase.regex, [f"https://example.com/?u={QOBUZ}"], id="qobuz"),
        pytest.param(
            MusicBrainzBase.regex,
            [
                f"https://example.com/?u={MUSICBRAINZ}",
                f"{MUSICBRAINZ}/cover-art",
                f"https://musicbrainzXorg/release/{MUSICBRAINZ_ID}",
            ],
            id="musicbrainz",
        ),
    ],
)
def test_a_link_inside_another_sites_query_a_lookalike_or_a_page_past_the_release_is_not_the_stores(
    regex, others
) -> None:
    assert [regex.match(other) for other in others] == [None] * len(others)


@pytest.mark.parametrize(("url", "source"), [(BEATPORT, "Beatport"), (QOBUZ, "Qobuz"), (MUSICBRAINZ, "MusicBrainz")])
def test_salmon_takes_a_link_with_a_query_or_fragment_as_the_same_album(url, source) -> None:
    forms = [url, f"{url}?utm_source=x", f"{url}#tracks"]

    assert [matching_source(form) for form in forms] == [source] * len(forms)
    assert len({_album_identity(form) for form in forms}) == 1
