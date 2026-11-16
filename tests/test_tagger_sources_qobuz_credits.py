"""Qobuz's credits as its API states them (#869 step 3): read, never decided, with the ones salmon guessed marked."""

from typing import Any

import anyio
import msgspec
import pytest

from salmon import cfg
from salmon.tagger.sources.qobuz import Scraper


def album(
    *tracks: dict[str, Any],
    artist: str | None = "Alice",
    artists: list[dict[str, Any]] | None = None,
    genres: list[str] | None = None,
) -> dict[str, Any]:
    """An API album holding these tracks, its `artists[]` Alice alone unless given."""
    data: dict[str, Any] = {
        "title": "Album",
        "artist": {"name": artist},
        "artists": [{"name": artist, "roles": ["main-artist"]}] if artists is None else artists,
        "genres_list": genres or ["Pop/Rock"],
        "tracks": {"items": [{"track_number": n, **track} for n, track in enumerate(tracks, 1)]},
    }
    return data


def mains(*names: str) -> list[dict[str, Any]]:
    return [{"name": name, "roles": ["main-artist"]} for name in names]


def track(performers: str | None = None, title: str = "Song", **fields: Any) -> dict[str, Any]:
    """A track, with no performers line unless given."""
    return {"title": title, **({} if performers is None else {"performers": performers}), **fields}


def parsed(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Each track as the parser hands it on, in order."""
    tracks = anyio.run(Scraper().parse_tracks, data)
    return [tracks["1"][str(n)] for n in range(1, len(tracks["1"]) + 1)]


def credits(data: dict[str, Any]) -> list[list[tuple[str, str]]]:
    return [t["artists"] for t in parsed(data)]


def guessed(data: dict[str, Any]) -> list[list[tuple[str, str]]]:
    return [t.get("assumed_artists", []) for t in parsed(data)]


def test_qobuz_states_its_credits() -> None:
    assert Scraper.states_credits is True


# The role words


def test_a_spaced_role_word_reads_as_its_unspaced_spelling() -> None:
    data = album(track("Alice, Associated Performer, Main Artist\r - Bob, Featured Artist\r - Carol, Guitar"))

    got = credits(data)

    assert got == [[("Alice", "main"), ("Bob", "guest")]]


@pytest.mark.parametrize(
    ("main", "featured"),
    [("MAINARTIST", "featured-artist"), ("mainartist", "Featured  Artist"), ("Main Artist", "FeaturedArtist")],
)
def test_a_role_word_reads_whatever_its_case_spacing_and_punctuation(main: str, featured: str) -> None:
    data = album(track(f"Bob, {main} - Carol, {featured}"), artists=mains("Alice", "Bob"))

    got = credits(data)

    assert got == [[("Bob", "main"), ("Carol", "guest")]]


def test_a_name_holding_a_comma_stays_whole_where_qobuz_gives_it_as_a_name() -> None:
    """The album's artist, an `artists[]` name of any role, and the track's performer are names Qobuz gives as such."""
    data = album(
        track("Earth, Wind & Fire, Associated Performer, Main Artist"),
        track("Earth, Wind & Fire, MainArtist - Orchestre de Paris, Choir, Orchestra, FeaturedArtist"),
        track(
            "Earth, Wind & Fire, MainArtist - Crosby, Stills & Nash, MainArtist",
            performer={"name": "Crosby, Stills & Nash"},
        ),
        artist="Earth, Wind & Fire",
        artists=[*mains("Earth, Wind & Fire"), {"name": "Orchestre de Paris, Choir", "roles": ["orchestra"]}],
    )

    got = credits(data)

    assert got == [
        [("Earth, Wind & Fire", "main")],
        [("Earth, Wind & Fire", "main"), ("Orchestre de Paris, Choir", "guest")],
        [("Earth, Wind & Fire", "main"), ("Crosby, Stills & Nash", "guest")],
    ]


@pytest.mark.parametrize("name", ["Sammy Davis, Jr.", "Harry Connick, Jr", "Johann Strauss, II", "Hank Williams, Sr."])
def test_a_name_ending_in_a_suffix_after_a_comma_stays_whole(name: str) -> None:
    data = album(track(f"Alice, MainArtist - {name}, Vocals, FeaturedArtist"))

    got = credits(data)

    assert got == [[("Alice", "main"), (name, "guest")]]


def test_as_before_a_name_holding_a_comma_qobuz_gives_nowhere_else_is_its_first_part() -> None:
    data = album(track("Alice, MainArtist - Tyler, The Creator, FeaturedArtist"))

    got = credits(data)

    assert got == [[("Alice", "main"), ("Tyler", "guest")]]


@pytest.mark.parametrize("word", ["Harpsichord", "Mezzo-soprano", "Turntables", "Guitar (acoustic)"])
def test_as_before_a_role_word_salmon_has_never_seen_is_not_part_of_the_name(word: str) -> None:
    """Qobuz's words are an open list: "Sigur Rós, Harpsichord, MainArtist" is Sigur Rós, the album's main."""
    data = album(track(f"Sigur Rós, {word}, MainArtist - Bob, {word}, FeaturedArtist"), artist="Sigur Rós")

    first = parsed(data)[0]

    assert first["artists"] == [("Sigur Rós", "main"), ("Bob", "guest")]


@pytest.mark.parametrize("word", ["Guitar", "Bass Guitar", "Vocals", "MusicPublisher", "Band Member", "StudioMusician"])
def test_as_before_an_instrument_or_other_credit_word_ends_a_name(word: str) -> None:
    data = album(track(f"Alice, MainArtist - Bob, {word}, FeaturedArtist"))

    got = credits(data)

    assert got == [[("Alice", "main"), ("Bob", "guest")]]


def test_a_stray_comma_after_a_name_is_not_part_of_it() -> None:
    data = album(track("Alice, MainArtist - Bob,, FeaturedArtist"))

    got = credits(data)

    assert got == [[("Alice", "main"), ("Bob", "guest")]]


def test_a_role_word_with_no_name_before_it_credits_no_one() -> None:
    """Reiner's Vienna: some lines end on a role word alone, "- Reissue Producer"."""
    data = album(track("Alice, MainArtist - Conductor"))

    got = credits(data)

    assert got == [[("Alice", "main")]]


def test_names_with_no_letters_or_digits_stay_apart() -> None:
    data = album(track("!!!, MainArtist - ???, FeaturedArtist"), artist="!!!")

    got = credits(data)

    assert got == [[("!!!", "main"), ("???", "guest")]]


def test_a_remixer_and_a_conductor_are_stated() -> None:
    data = album(track("Alice, MainArtist - Bob, Remixer - Carol, Conductor"), genres=["Classique"])

    first = parsed(data)[0]

    assert first["artists"] == [("Alice", "main"), ("Bob", "remixer"), ("Carol", "conductor")]
    assert first["assumed_artists"] == []


def test_as_before_a_producer_an_arranger_and_an_unmapped_word_credit_no_one() -> None:
    data = album(track("Alice, MainArtist - Bob, Producer - Carol, Arranger - Dan, Writer - Eve, Lyricist"))

    got = credits(data)

    assert got == [[("Alice", "main")]]


# A track's MainArtist (ruling 2)


def test_a_track_s_main_artist_the_album_lists_as_main_is_a_stated_main() -> None:
    data = album(track("Alice, MainArtist - Bob, MainArtist"), artists=mains("Alice", "Bob"))

    first = parsed(data)[0]

    assert first["artists"] == [("Alice", "main"), ("Bob", "main")]
    assert first["assumed_artists"] == []


def test_a_track_s_main_artist_the_album_does_not_list_is_an_assumed_guest() -> None:
    data = album(track("Alice, MainArtist - Bob, MainArtist"))

    first = parsed(data)[0]

    assert first["artists"] == [("Alice", "main"), ("Bob", "guest")]
    assert first["assumed_artists"] == [("Bob", "guest")]


def test_a_track_s_mains_are_the_album_s_main_artists_it_names() -> None:
    """The Graduate: Simon & Garfunkel and Dave Grusin are both main-artist, each on their own tracks."""
    data = album(
        track("Simon & Garfunkel, MainArtist, AssociatedPerformer", performer={"name": "Simon & Garfunkel"}),
        track("Dave Grusin, MainArtist, AssociatedPerformer", performer={"name": "Dave Grusin"}),
        artist="Simon & Garfunkel",
        artists=mains("Simon & Garfunkel", "Dave Grusin"),
    )

    got = credits(data)

    assert got == [[("Simon & Garfunkel", "main")], [("Dave Grusin", "main")]]


def test_a_track_naming_none_of_the_album_s_main_artists_has_them_all() -> None:
    """Distant Relatives: every track's performer is the pair's joint name, which neither main-artist matches. The
    joint name holds an album main, so it is the album's credit, not a guest."""
    joint = 'Nas & Damian "Jr. Gong" Marley'
    data = album(
        track(f"{joint}, MainArtist - K'Naan, FeaturedArtist", performer={"name": joint}),
        artist="Damian Marley",
        artists=mains("Damian Marley", "Nas"),
    )

    first = parsed(data)[0]

    assert first["artists"] == [("Damian Marley", "main"), ("Nas", "main"), ("K'Naan", "guest")]
    assert first["assumed_artists"] == []


def test_as_before_the_album_s_artist_is_main_on_a_track_that_names_no_one() -> None:
    data = album(track(), track(""))

    got = credits(data)

    assert got == [[("Alice", "main")], [("Alice", "main")]]


def test_the_album_s_artist_on_a_track_that_names_no_one_is_stated() -> None:
    data = album(track())

    assert guessed(data) == [[]]


def test_the_album_s_artists_on_a_track_that_names_only_others_are_assumed() -> None:
    """DJ-Kicks and a compilation: each track names its own artists, none of them the album's. The album's credit
    stands in for the track's main, but Qobuz didn't say so: salmon did."""
    data = album(track("Bob, MainArtist"), artist="Various Artists", artists=mains("Various Artists"))

    first = parsed(data)[0]

    assert first["artists"] == [("Various Artists", "main"), ("Bob", "guest")]
    assert first["assumed_artists"] == [("Various Artists", "main"), ("Bob", "guest")]


def test_another_artist_s_track_featuring_the_album_s_artist_is_led_by_its_own_main_artist() -> None:
    """A bonus track: Bob's song featuring Alice, on Alice's album. Qobuz states Alice as its guest, so Bob leads it,
    by salmon's guess, and the album's artist is not its main."""
    data = album(track("Bob, MainArtist - Alice, FeaturedArtist", performer={"name": "Bob feat. Carol"}))

    first = parsed(data)[0]

    assert first["artists"] == [("Bob", "main"), ("Carol", "guest"), ("Alice", "guest")]
    assert first["assumed_artists"] == [("Bob", "main"), ("Carol", "guest")]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("Alice, FeaturedArtist", id="no other artist's track"),
        pytest.param("Alice, MainArtist - Bob, MainArtist - Carol, FeaturedArtist", id="naming an album main"),
    ],
)
def test_as_before_the_album_s_artist_leads_a_track_that_is_not_another_artist_s_featuring_it(line: str) -> None:
    data = album(track(line), track("Bob feat. Alice, MainArtist"), artists=mains("Alice", "Carol"))

    got = credits(data)

    assert [artists[0] for artists in got] == [("Alice", "main"), ("Alice", "main")]


def test_an_album_main_is_credited_under_the_album_s_spelling() -> None:
    """Getz/Gilberto: the album's "João Gilberto" is "Joao Gilberto" in the performers; Sigur Rós has a trailing
    space and no accent there."""
    data = album(
        track("Stan Getz, MainArtist - Joao Gilberto, MainArtist"),
        track("Sigur Ros , MainArtist"),
        artist="Stan Getz",
        artists=mains("Stan Getz", "João Gilberto", "Sigur Rós"),
    )

    got = credits(data)

    assert got == [[("Stan Getz", "main"), ("João Gilberto", "main")], [("Sigur Rós", "main")]]


def test_the_album_s_artist_takes_its_spelling_in_the_artists_list() -> None:
    data = album(track("Joao Gilberto, MainArtist"), artist="Joao Gilberto", artists=mains("João Gilberto"))

    got = credits(data)

    assert got == [[("João Gilberto", "main")]]


def test_as_before_an_album_main_the_album_also_lists_as_featured_is_not_its_own_guest() -> None:
    data = album(track(), artists=[{"name": "Alice", "roles": ["main-artist", "featured-artist"]}])

    got = credits(data)

    assert got == [[("Alice", "main")]]


def test_one_artist_in_two_spellings_is_credited_once_on_a_track() -> None:
    """Chung: the track's performer "Kyung-Wha Chung" is the performers line's "Kyung Wha Chung"."""
    data = album(track("Kyung Wha Chung, Violin, MainArtist", performer={"name": "Kyung-Wha Chung"}))

    first = parsed(data)[0]

    assert first["artists"] == [("Alice", "main"), ("Kyung-Wha Chung", "guest")]
    assert first["assumed_artists"] == [("Alice", "main"), ("Kyung-Wha Chung", "guest")]


def test_a_main_artist_named_with_a_featured_credit_gives_the_main_and_assumed_guests() -> None:
    """Santana's MainArtist "Santana featuring Eagle-Eye Cherry", and a performer shaped as Doris's are (its
    performers line has the same name as "Performer", a word salmon doesn't map)."""
    doris = "Earl Sweatshirt feat. Vince Staples & Casey Veggies"
    data = album(
        track("Santana featuring Eagle-Eye Cherry, MainArtist"),
        track(f"{doris}, Performer", performer={"name": doris}),
        artist="Santana",
        artists=mains("Santana", "Earl Sweatshirt"),
    )

    got = parsed(data)

    assert [t["artists"] for t in got] == [
        [("Santana", "main"), ("Eagle-Eye Cherry", "guest")],
        [("Earl Sweatshirt", "main"), ("Vince Staples", "guest"), ("Casey Veggies", "guest")],
    ]
    assert [t["assumed_artists"] for t in got] == [
        [("Eagle-Eye Cherry", "guest")],
        [("Vince Staples", "guest"), ("Casey Veggies", "guest")],
    ]


def test_a_guest_read_from_the_title_is_assumed() -> None:
    data = album(track("Alice, MainArtist - Bob, FeaturedArtist", title="Song (feat. Bob & Carol)"))

    first = parsed(data)[0]

    assert first["artists"] == [("Alice", "main"), ("Bob", "guest"), ("Carol", "guest")]
    assert first["assumed_artists"] == [("Carol", "guest")]


def test_as_before_an_album_featured_artist_is_a_guest_on_every_track() -> None:
    data = album(track(), track(), artists=[*mains("Alice"), {"name": "Bob", "roles": ["featured-artist"]}])

    got = credits(data)

    assert got == [[("Alice", "main"), ("Bob", "guest")], [("Alice", "main"), ("Bob", "guest")]]


def test_an_album_featured_artist_is_stated_where_the_track_s_line_names_them_and_guessed_elsewhere() -> None:
    """Qobuz says the album features Bob, not that every track does: on a track whose line doesn't name Bob, in any
    role it maps, Bob is salmon's guess."""
    data = album(
        track("Alice, MainArtist - Bob, FeaturedArtist"),
        track("Alice, MainArtist - Bob, Vocals, Composer"),
        track("Alice, MainArtist - Bob, Vocals"),
        track("Alice, MainArtist"),
        artists=[*mains("Alice"), {"name": "Bob", "roles": ["featured-artist"]}],
    )

    got = parsed(data)

    assert [t["artists"] for t in got] == [[("Alice", "main"), ("Bob", "guest")]] * 4
    assert [t["assumed_artists"] for t in got] == [[], [], [("Bob", "guest")], [("Bob", "guest")]]


# Composers (ruling 1)


@pytest.mark.parametrize("genres", [["Classique"], ["Classique→Opéra"], ["Classical"], ["Klassische Musik"]])
def test_a_classical_release_credits_each_track_s_composer(genres: list[str]) -> None:
    data = album(
        track("Alice, MainArtist - Bela Bartok, Composer", composer={"name": "Béla Bartók"}),
        track("Alice, MainArtist - Bela Bartok, ComposerLyricist"),
        genres=genres,
    )

    got = credits(data)

    assert got == [[("Alice", "main"), ("Béla Bartók", "composer")], [("Alice", "main"), ("Bela Bartok", "composer")]]


def test_a_classical_release_credits_its_composers_whatever_no_genres_from_qobuz_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg.metadata, "qobuz", msgspec.structs.replace(cfg.metadata.qobuz, no_genres_from_qobuz=True))
    data = album(track("Alice, MainArtist", composer={"name": "Béla Bartók"}), genres=["Classique"])

    got = credits(data)

    assert got == [[("Alice", "main"), ("Béla Bartók", "composer")]]


@pytest.mark.parametrize("genres", [["Pop/Rock"], ["Jazz"], ["Électronique→Dance"], ["Bandes originales de films"]])
def test_as_before_a_release_that_is_not_classical_credits_no_composer_or_conductor(genres: list[str]) -> None:
    """Composers and conductors are read on a classical release alone (EXTRA_ROLES), as RED credits them."""
    data = album(
        track("Alice, MainArtist - Bob, Composer, ComposerLyricist - Carol, Conductor", composer={"name": "Bob"}),
        genres=genres,
    )

    got = credits(data)

    assert got == [[("Alice", "main")]]


# Guards


def test_a_track_whose_performers_line_is_null_takes_the_album_s_artist() -> None:
    data = album({"title": "Song", "performers": None, "performer": None})

    got = credits(data)

    assert got == [[("Alice", "main")]]


def test_a_release_with_no_artists_list_takes_its_artist_as_main() -> None:
    data = album(track("Alice, MainArtist - Bob, MainArtist"))
    del data["artists"]

    first = parsed(data)[0]

    assert first["artists"] == [("Alice", "main"), ("Bob", "guest")]
    assert first["assumed_artists"] == [("Bob", "guest")]


def test_a_release_with_no_artist_credits_no_empty_name() -> None:
    data = album(track("Bob, MainArtist"), track(), artist="", artists=[])

    got = credits(data)

    assert got == [[("Bob", "guest")], []]
