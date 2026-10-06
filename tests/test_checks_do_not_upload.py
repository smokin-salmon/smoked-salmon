"""The trackers' Do-Not-Upload lists (#533): what an entry matches, and the lists salmon ships."""

from pathlib import Path

import pytest

from salmon.checks import do_not_upload
from salmon.checks.do_not_upload import LISTS, Candidate, do_not_upload_reason, load_list


def _release(*artists: str, title: str = "Album", edition_title: str = "", labels=(), media: str = "WEB"):
    return Candidate(artists=artists, title=title, edition_title=edition_title, labels=tuple(labels), media=media)


def write_lists(monkeypatch, folder: Path, **lists: str) -> None:
    """Make salmon read its lists from folder: the TOML text given for each tracker, and empty lists otherwise."""
    folder.mkdir(exist_ok=True)
    for tracker, name in LISTS.items():
        (folder / name).write_text(lists.get(tracker, ""), encoding="utf-8")
    monkeypatch.setattr(do_not_upload, "LISTS_DIR", folder)


# What the shipped lists match


def test_an_artist_entry_forbids_the_whole_discography() -> None:
    reason = do_not_upload_reason("RED", _release("Nicole 12", title="Any Album At All"))

    assert reason is not None
    assert reason.startswith("Nicole 12 (the whole discography) is on RED's Do-Not-Upload list: ")
    assert reason.endswith(
        "If yours is a legitimate copy, RED wants a message to its staff, with proof, before it is uploaded."
    )


def test_an_album_entry_forbids_that_release_only() -> None:
    assert do_not_upload_reason("RED", _release("Dr. Dre", title="Detox")) is not None
    assert do_not_upload_reason("RED", _release("Dr. Dre", title="2001")) is None
    assert do_not_upload_reason("RED", _release("Someone Else", title="Detox")) is None


def test_an_album_only_entry_forbids_that_compilation_whatever_its_artists() -> None:
    title = "Encyclopedia of Jazz: The World's Greatest Jazz Collection"
    reason = do_not_upload_reason("RED", _release("Miles Davis", "John Coltrane", "Billie Holiday", title=title))

    assert reason is not None
    assert reason.startswith(f"{title} (whatever the artist) is on RED's")
    assert do_not_upload_reason("OPS", _release(title="The Ultimate 500 CD Jazz Collection")) is not None
    # A release with no artist at all is matched too.
    assert do_not_upload_reason("RED", _release(title=title)) is not None


@pytest.mark.parametrize("tracker", ["RED", "OPS"])
def test_a_label_entry_forbids_every_release_on_it(tracker: str) -> None:
    reason = do_not_upload_reason(tracker, _release("Whoever", labels=["Sandero Classic Sound"]))

    assert reason is not None
    assert reason.startswith(f"the label Sandero Classic Sound is on {tracker}'s")
    # Either of the release's labels.
    assert do_not_upload_reason(tracker, _release("Whoever", labels=["Real Records", "Sip It & Trip It Records"]))
    assert do_not_upload_reason(tracker, _release("Whoever", labels=["Real Records"])) is None


def test_a_web_only_entry_forbids_the_web_release_and_not_a_cd() -> None:
    web = do_not_upload_reason("RED", _release("Glen Porter", title="Blessed by a Young Death", media="WEB"))

    assert web is not None
    assert web.startswith("Glen Porter - Blessed by a Young Death (WEB only) is on RED's")
    assert do_not_upload_reason("RED", _release("Glen Porter", title="Blessed by a Young Death", media="CD")) is None
    assert do_not_upload_reason("RED", _release(title="Yes Means Nein", media="Vinyl")) is None
    assert do_not_upload_reason("RED", _release("A", "B", title="Yes Means Nein", media="WEB")) is not None


def test_an_artist_is_matched_whole_and_in_a_collaboration() -> None:
    assert do_not_upload_reason("RED", _release("Viper UK")) is None
    assert do_not_upload_reason("RED", _release("Viperish")) is None
    assert do_not_upload_reason("RED", _release("Viper", "Someone Else")) is not None


def _metadata(artists, **changes):
    return {"artists": artists, "title": "Album", "edition_title": None, "label": None, "source": "WEB", **changes}


def test_a_guest_artist_is_not_matched() -> None:
    metadata = _metadata([("Someone", "main"), ("Nicole 12", "guest")])

    assert do_not_upload_reason("RED", Candidate.from_metadata(metadata)) is None
    metadata["artists"][1] = ("Nicole 12", "main")
    assert do_not_upload_reason("RED", Candidate.from_metadata(metadata)) is not None


def test_case_accents_punctuation_and_ampersands_do_not_count() -> None:
    assert do_not_upload_reason("RED", _release("dr dre", title="DETOX")) is not None
    assert do_not_upload_reason("RED", _release("Jean Michel Jarré", title="Music For Supermarkets")) is not None
    # RED spells it with "&", OPS with "and": each matches both.
    for title in ("Cigarettes & Valentines", "Cigarettes and Valentines"):
        assert do_not_upload_reason("RED", _release("Green Day", title=title)) is not None
        assert do_not_upload_reason("OPS", _release("Green Day", title=title)) is not None
    assert do_not_upload_reason("RED", _release("Green Day", title="Cigarettes")) is None


def test_the_album_words_may_be_in_the_edition_title() -> None:
    assert do_not_upload_reason("RED", _release("Fleet Foxes", title="Shore", edition_title="Stems Edition"))
    assert do_not_upload_reason("RED", _release("Fleet Foxes", title="Shore")) is None


def test_each_trackers_list_forbids_only_its_own_uploads() -> None:
    nicole = _release("Nicole 12")
    super_mix = _release("Michael Jackson", title="Super Mix")

    assert do_not_upload_reason("RED", nicole) is not None
    assert do_not_upload_reason("OPS", nicole) is None
    assert do_not_upload_reason("OPS", super_mix) is not None
    assert do_not_upload_reason("RED", super_mix) is None


def test_dic_has_no_list_and_forbids_nothing() -> None:
    assert "DIC" not in LISTS
    for release in (_release("Nicole 12"), _release("Wu-Tang Clan", title="Once Upon a Time in Shaolin")):
        assert do_not_upload_reason("DIC", release) is None
    assert load_list("DIC") == []


def test_an_upload_form_is_matched_on_its_main_artists_title_edition_and_labels() -> None:
    form = {
        "artists[]": ["Someone", "Viper"],
        "importance[]": [1, 2],
        "title": "Album",
        "remaster_title": "",
        "record_label": "Sandero Classic Sound",
        "remaster_record_label": "",
        "media": "CD",
    }
    candidate = Candidate.from_form(form)

    assert candidate == Candidate(("Someone",), "Album", "", ("Sandero Classic Sound",), "CD")
    assert do_not_upload_reason("OPS", candidate) is not None


# A list salmon cannot read


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("[[entry]\nartist = 'x'", "Expected"),
        ("[[entry]]\nartist = 'Someone'\n", "Object missing required field `note`"),
        ("[[entry]]\nartits = 'Someone'\nnote = 'x'\n", "unknown field `artits`"),
        ("[[entry]]\nnote = 'x'\n", "an entry needs an artist, an album or a label"),
        ("[[entry]]\nartist = '...'\nnote = 'x'\n", "has no word to match"),
        ("[[entry]]\nartist = 'Someone'\nnote = ' '\n", "an entry needs a note"),
        ("[[entry]]\nlabel = 'Label'\nartist = 'Someone'\nnote = 'x'\n", "a label entry names no artist or album"),
        ("[[entry]]\nartist = 'Someone'\nmedia = 'Web'\nnote = 'x'\n", "the media 'Web' is none of"),
    ],
    ids=["toml", "no note", "typo", "nothing named", "no word", "blank note", "label and artist", "media"],
)
def test_a_list_that_cannot_be_read_forbids_every_upload_to_its_tracker(monkeypatch, tmp_path, text, said) -> None:
    write_lists(monkeypatch, tmp_path, RED=text)

    reason = do_not_upload_reason("RED", _release("Radiohead", title="In Rainbows"))

    assert reason is not None
    assert reason.startswith(f"salmon cannot read its copy of RED's Do-Not-Upload list, {tmp_path / 'red.toml'} (")
    assert said in reason
    # The other tracker's list is fine.
    assert do_not_upload_reason("OPS", _release("Radiohead", title="In Rainbows")) is None


def test_a_missing_list_forbids_every_upload_to_its_tracker(monkeypatch, tmp_path) -> None:
    write_lists(monkeypatch, tmp_path)
    (tmp_path / "ops.toml").unlink()

    reason = do_not_upload_reason("OPS", _release("Radiohead", title="In Rainbows"))

    assert reason is not None
    assert str(tmp_path / "ops.toml") in reason
    assert do_not_upload_reason("RED", _release("Radiohead", title="In Rainbows")) is None


# The lists salmon ships


@pytest.mark.parametrize("tracker", sorted(LISTS))
def test_every_shipped_entry_reads_has_a_note_and_forbids_its_own_release(tracker: str) -> None:
    entries = load_list(tracker)

    assert entries
    for entry in entries:
        assert entry.note.strip()
        release = _release(
            *([entry.artist] if entry.artist else []),
            title=entry.album or "Any Album",
            labels=[entry.label] if entry.label else [],
            media=entry.media or "CD",
        )
        assert do_not_upload_reason(tracker, release) is not None, entry


def test_the_shipped_lists_have_the_wiki_entries() -> None:
    # RED's article lists 25 music lines, one of them naming 7 albums; OPS's has 12.
    assert len(load_list("RED")) == 31
    assert len(load_list("OPS")) == 12


@pytest.mark.parametrize("tracker", sorted(LISTS))
def test_each_shipped_list_says_where_it_comes_from_and_when_it_was_checked(tracker: str) -> None:
    text = (do_not_upload.LISTS_DIR / LISTS[tracker]).read_text(encoding="utf-8")

    assert text.startswith(f"# {tracker}'s Do-Not-Upload list")
    assert "wiki article" in text
    assert "Checked against that article on " in text
