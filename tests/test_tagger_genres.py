"""Genre standardization: splitting combined genres without breaking whitelisted ones."""

from salmon.tagger.sources.base import standardize_genres
from salmon.uploader import convert_genres


def test_slash_combined_genres_are_split_into_whitelisted_parts():
    # Beatport ships "Dance / Pop"; "dancepop" is not in GENRE_LIST, so it used to survive whole.
    assert sorted(standardize_genres({"Dance / Pop"})) == ["Dance", "Pop"]
    assert sorted(standardize_genres({"Funk / Soul / Disco"})) == ["Disco", "Funk", "Soul"]


def test_ampersand_genres_are_left_whole():
    # These are single entries in GENRE_LIST; splitting them would invent "Drum" and "Bass".
    assert standardize_genres({"Drum & Bass"}) == ["Drum & Bass"]
    assert standardize_genres({"Rhythm & Blues"}) == ["Rhythm & Blues"]
    # "R&B" keys to "randb", which the whitelist canonicalizes rather than splits.
    assert standardize_genres({"R&B"}) == ["Rhythm & Blues"]


def test_splitting_no_longer_evicts_the_standalone_genre():
    # "Dance / Pop" used to discard a clean "Dance" via the generic-combination filter.
    assert sorted(standardize_genres({"Dance", "Dance / Pop"})) == ["Dance", "Pop"]


def test_unknown_genres_survive_and_whitespace_is_dropped():
    assert standardize_genres({"Bhangra"}) == ["Bhangra"]
    assert sorted(standardize_genres({"House /  / Folk"})) == ["Folk", "House"]


def test_convert_genres_normalizes_separators_to_dots():
    assert convert_genres(["Hip-Hop", "Deep_House", "Pop Rock"]) == "Hip.Hop,Deep.House,Pop.Rock"
    assert convert_genres([]) == ""


def test_convert_genres_spells_out_ampersands():
    # GENRE_LIST itself yields these, so "Drum.&.Bass" reached the tracker on every D&B upload.
    assert convert_genres(["Drum & Bass"]) == "Drum.and.Bass"
    assert convert_genres(["Rhythm & Blues"]) == "Rhythm.and.Blues"
    assert convert_genres(["Rock & Roll"]) == "Rock.and.Roll"
    assert convert_genres(["Singer & Songwriter"]) == "Singer.and.Songwriter"
    assert convert_genres(["R&B"]) == "R.and.B"


def test_convert_genres_collapses_slashes_and_runs():
    assert convert_genres(["Dance / Pop"]) == "Dance.Pop"
    assert convert_genres(["Electronica / Downtempo"]) == "Electronica.Downtempo"
