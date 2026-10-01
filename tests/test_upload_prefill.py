"""Prompts whose default comes from the files when they know the answer (#536).

Every prompt stays and any typed answer still wins; only the answer an empty reply gives changes. Files
that contradict themselves give no default. The runs here go against the local fake tracker of
test_uploader_dry_run and a fake image host, never a real one.
"""

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anyio
import asyncclick as click
import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _album,
    _returning,
    _returning_async,
    _run_up,
    _write_flac,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.checks.source import detect_source
from salmon.errors import RequestError
from salmon.search.base import IdentData
from salmon.tagger import metadata as metadata_mod
from salmon.tagger import review
from salmon.uploader import dupe_checker

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

QOBUZ_URL = "https://www.qobuz.com/gb-en/album/album-artist/abc123xyz"
DEEZER_URL = "https://www.deezer.com/album/322064097"
ANOTHER_TRACKER = "Would you like to upload to another tracker?"
DOWNCONVERSION = "Would you like to check downconversion options?"


def _prompts(monkeypatch, *answers: str) -> list[Any]:
    """Answer click.prompt with `answers` in turn (an empty one takes the default); give the defaults offered."""
    defaults: list[Any] = []
    queue = list(answers)

    async def prompt(*_args: Any, **kwargs: Any) -> Any:
        defaults.append(kwargs.get("default"))
        answer = queue.pop(0) if queue else ""
        return answer or kwargs.get("default")

    monkeypatch.setattr(click, "prompt", prompt)
    return defaults


# The store URL in the tags, and the matching search result: the metadata prompt


def _tagged_album(folder: Path, *tags: dict[str, str]) -> Path:
    """An album of one FLAC per tag set."""
    folder.mkdir(parents=True)
    for number, file_tags in enumerate(tags, 1):
        _write_flac(folder / f"0{number}.flac", title=f"Track {number}", **file_tags)
    return folder


def test_the_store_url_under_a_source_key_is_the_files_store_url(tmp_path) -> None:
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": QOBUZ_URL})

    assert metadata_mod.files_store_url(str(album)) == QOBUZ_URL


@pytest.mark.parametrize(
    "tags",
    [
        # Two different store albums.
        ({"SOURCE": QOBUZ_URL}, {"SOURCE": DEEZER_URL}),
        # A second store album under another key.
        ({"SOURCE": QOBUZ_URL, "COMMENT": DEEZER_URL}, {"SOURCE": QOBUZ_URL}),
        # A store's track page, not its album.
        ({"SOURCE": "https://www.deezer.com/track/12345"},),
        # Not a store.
        ({"SOURCE": "https://artist.example/album/abc"},),
        # A store album, but not under a source key.
        ({"COMMENT": QOBUZ_URL},),
    ],
)
def test_files_that_name_no_single_store_album_give_no_store_url(tmp_path, tags) -> None:
    album = _tagged_album(tmp_path / "a", *tags)

    assert metadata_mod.files_store_url(str(album)) is None


def _rls_data(**overrides: Any) -> dict[str, Any]:
    return {
        "artists": [("Artist", "main"), ("Guest", "guest")],
        "title": "Album",
        "year": "2020",
        "group_year": "2020",
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "edition_title": None,
        "label": None,
        "catno": None,
        "upc": None,
        "genres": [],
        "rls_type": None,
        "comment": None,
        "urls": [],
        "tracks": {},
        **overrides,
    }


def _search(*entries: tuple[str, str, IdentData]) -> tuple[dict[int, tuple[str, str]], dict[str, Any]]:
    """Numbered choices and search results, from (source, release ID, ident) entries."""
    choices: dict[int, tuple[str, str]] = {}
    results: dict[str, Any] = {}
    for number, (source, rls_id, ident) in enumerate(entries, 1):
        choices[number] = (source, rls_id)
        results.setdefault(source, {})[rls_id] = (ident, "display")
    return choices, results


def _ident(artist: str = "Artist", album: str = "Album", year: Any = 2020, tracks: int | None = 2) -> IdentData:
    return IdentData(artist, album, year, tracks, "WEB")


def test_the_store_url_is_starred_and_the_matching_result_of_another_source_added() -> None:
    choices, results = _search(("MusicBrainz", "mb", _ident(album="Other")), ("Deezer", "dz", _ident()))

    suggestion = metadata_mod.suggest_choice(choices, results, _rls_data(), 2, QOBUZ_URL)
    assert suggestion == f"*{QOBUZ_URL} 2"


def test_a_store_url_is_not_starred_for_a_release_that_is_not_web() -> None:
    suggestion = metadata_mod.suggest_choice({}, {}, _rls_data(source="CD"), 2, QOBUZ_URL)
    assert suggestion == QOBUZ_URL


def test_the_matching_result_of_the_store_urls_own_source_is_left_out() -> None:
    choices, results = _search(("Deezer", "dz", _ident()))

    suggestion = metadata_mod.suggest_choice(choices, results, _rls_data(), 2, DEEZER_URL)
    assert suggestion == f"*{DEEZER_URL}"


def test_a_store_suffix_on_the_title_and_a_joint_artist_credit_still_match() -> None:
    rls_data = _rls_data(artists=[("Artist", "main"), ("Other", "main")])
    choices, results = _search(("Apple Music", "am", _ident(artist="Artist & Other", album="Album - EP")))

    assert metadata_mod.suggest_choice(choices, results, rls_data, 2, None) == "1"


@pytest.mark.parametrize(
    "ident",
    [
        _ident(tracks=3),  # Another track count.
        _ident(year=2015),  # Another year.
        _ident(artist="Guest"),  # Only a guest artist.
        _ident(album="Album Two"),
    ],
)
def test_a_result_the_files_contradict_is_not_the_default(ident) -> None:
    choices, results = _search(("Deezer", "dz", ident))

    assert metadata_mod.suggest_choice(choices, results, _rls_data(), 2, None) is None


def test_two_matching_results_of_one_source_give_none_of_it() -> None:
    choices, results = _search(("Deezer", "explicit", _ident()), ("Deezer", "clean", _ident()))

    assert metadata_mod.suggest_choice(choices, results, _rls_data(), 2, None) is None


def test_the_metadata_prompt_offers_the_files_store_url_and_the_matching_result(monkeypatch, tmp_path) -> None:
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": QOBUZ_URL})
    choices_found = {"Deezer": {"dz": (_ident(), "Artist - Album")}}
    monkeypatch.setattr(metadata_mod, "run_metasearch", _returning_async(choices_found))
    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", _returning({"tracks": {}, "genres": []}))
    # A [m]anual answer: what the prompt offered is all this checks.
    defaults = _prompts(monkeypatch, "m")

    anyio.run(metadata_mod.get_metadata, str(album), {"01.flac": None, "02.flac": None}, _rls_data())
    assert defaults == [f"*{QOBUZ_URL} 1"]


def test_with_no_store_url_and_no_match_the_metadata_prompt_has_no_default(monkeypatch, tmp_path) -> None:
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": DEEZER_URL})
    choices_found = {"Deezer": {"dz": (_ident(tracks=9), "Artist - Album")}}
    monkeypatch.setattr(metadata_mod, "run_metasearch", _returning_async(choices_found))
    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", _returning({"tracks": {}, "genres": []}))
    defaults = _prompts(monkeypatch, "m")

    anyio.run(metadata_mod.get_metadata, str(album), {"01.flac": None, "02.flac": None}, _rls_data())
    assert defaults == [None]


def test_one_store_url_gives_both_the_media_default_and_the_metadata_default(monkeypatch, tmp_path) -> None:
    # The source prompt's default is #537's detection; the metadata prompt's comes from the same tag.
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": QOBUZ_URL})
    monkeypatch.setattr(metadata_mod, "run_metasearch", _returning_async({}))
    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", _returning({"tracks": {}, "genres": []}))
    defaults = _prompts(monkeypatch, "", "m")

    source = anyio.run(salmon.uploader._prompt_source, detect_source(str(album)))
    anyio.run(metadata_mod.get_metadata, str(album), {"01.flac": None, "02.flac": None}, _rls_data(source=source))
    assert source == "WEB"
    assert defaults == ["WEB", f"*{QOBUZ_URL}"]


# The release type, from the track count


@pytest.mark.parametrize(
    ("title", "durations", "expected"),
    [
        ("Album", [200], "Single"),
        ("Album", [200, 200, 200], "Single"),
        ("Album", [200] * 4, "EP"),
        ("Album", [200] * 6, "EP"),
        ("Album", [200] * 7, "Album"),
        # Past 30 minutes, an album; a single's track past 10 minutes makes it an EP.
        ("Album", [400] * 5, "Album"),
        ("Album", [700, 200], "EP"),
        # Unknown lengths: the count alone.
        ("Album", [0, 0], "Single"),
        # The title agrees.
        ("Album EP", [200] * 4, "EP"),
        ("Album (Single)", [200], "Single"),
        # The title names another type.
        ("Album EP", [200] * 12, None),
        ("Album (Single)", [200] * 5, None),
        ("Album", [], None),
    ],
)
def test_the_release_type_follows_the_track_count(title, durations, expected) -> None:
    assert review.suggest_release_type(title, durations) == expected


def test_the_release_type_prompt_offers_the_hint(monkeypatch) -> None:
    defaults = _prompts(monkeypatch, "")
    metadata = {"rls_type": None}

    anyio.run(review._check_for_empty_release_type, metadata, "EP")
    assert metadata["rls_type"] == "EP"
    assert defaults == ["EP"]


def test_an_upload_offers_the_release_type_its_files_imply(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    hints: list[Any] = []

    async def review_with_ai(metadata, _tag_baseline, _source_url, _validator, manual_review, **_kw):
        hints.append(manual_review.keywords["rls_type_hint"])
        return metadata

    four_tracks = {f"0{n}.flac": {"duration": 200} for n in range(1, 5)}
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        input="\n\n",
        gather_audio_info=_returning(four_tracks),
        review_metadata_with_ai=review_with_ai,
    )

    assert run.result.exit_code == 0, run.result.output
    assert hints == ["EP"]


# The group: a search result with our artist, title and year


def _result(group_id: int, name: str = "Album", year: Any = 2020, artist: str = "Artist", **kw: Any) -> dict:
    return {
        "groupId": group_id,
        "groupName": name,
        "groupYear": year,
        "artist": artist,
        "releaseType": "Album",
        "tags": [],
        "torrents": [],
        **kw,
    }


def test_the_one_result_with_our_artist_title_and_year_is_the_default() -> None:
    results = [_result(1, year=2015), _result(2), _result(3, name="Album Two")]

    assert dupe_checker.suggest_group(results, _rls_data()) == "2"


def test_names_match_as_the_tracker_escapes_them_and_credits_joint_artists() -> None:
    release = _rls_data(title="Rock & Roll", artists=[("A", "main"), ("B", "main")])

    assert dupe_checker.suggest_group([_result(9, name="Rock &amp; Roll", artist="A &amp; B")], release) == "1"


@pytest.mark.parametrize(
    ("results", "release"),
    [
        ([_result(1, year=2015)], _rls_data()),  # The group's year differs.
        ([_result(1)], _rls_data(year=None)),  # Our year is unknown.
        ([_result(1), _result(2)], _rls_data()),  # Two groups match.
        ([_result(1, artist="Guest")], _rls_data()),
        ([{**_result(1), "groupId": None}], _rls_data()),
        ([], _rls_data()),
        ([_result(1)], None),
    ],
)
def test_no_single_matching_group_leaves_a_new_group_the_default(results, release) -> None:
    assert dupe_checker.suggest_group(results, release) == ""


def _site(**kw: Any) -> Any:
    # No torrentgroup: a group picked from the results must be printed and checked from them, without a request.
    return cast(
        "Any",
        SimpleNamespace(
            base_url="https://tracker.test", site_string="TRK", site_code="TRK", has_session_cookie=False, **kw
        ),
    )


def test_the_group_prompt_offers_the_matching_group(monkeypatch) -> None:
    defaults = _prompts(monkeypatch, "", "")
    results = [_result(100, year=2015), _result(200)]

    group_id = anyio.run(
        dupe_checker.resolve_existing_group, _site(), ["artist album"], results, None, True, _rls_data()
    )
    assert group_id == 200
    # The group, then its confirmation, which holds nothing of ours: yes.
    assert defaults == ["2", "Y"]


def test_without_our_release_the_group_prompt_defaults_to_a_new_group(monkeypatch) -> None:
    defaults = _prompts(monkeypatch, "")

    group_id = anyio.run(dupe_checker.resolve_existing_group, _site(), ["artist album"], [_result(200)], None)
    assert group_id is None
    assert defaults == [""]


# An exact format match in the chosen group


def _torrent(**kw: Any) -> dict[str, Any]:
    return {"id": 5, "media": "WEB", "format": "FLAC", "encoding": "Lossless", "remastered": False, **kw}


@pytest.mark.parametrize("answer", ["", "a"])
def test_an_exact_format_match_is_flagged_and_abort_is_the_default(monkeypatch, capsys, answer) -> None:
    defaults = _prompts(monkeypatch, "200", answer)
    results = [_result(200, torrents=[_torrent()])]

    with pytest.raises(click.Abort):
        anyio.run(dupe_checker.resolve_existing_group, _site(), ["s"], results, None, True, _rls_data())
    assert defaults[1] == "a"
    assert "DUPE RISK: this edition already has OR / WEB / FLAC / Lossless" in capsys.readouterr().out


def test_an_explicit_yes_still_uploads_into_a_group_with_an_exact_match(monkeypatch) -> None:
    _prompts(monkeypatch, "200", "y")
    results = [_result(200, torrents=[_torrent()])]
    release = _rls_data()

    group_id = anyio.run(dupe_checker.resolve_existing_group, _site(), ["s"], results, None, True, release)
    assert group_id == 200


@pytest.mark.parametrize(
    "torrent",
    [
        _torrent(encoding="24bit Lossless"),
        _torrent(media="CD"),
        _torrent(format="MP3", encoding="320"),
        # Another edition: a remaster of another year.
        _torrent(remastered=True, remasterYear=2015),
    ],
)
def test_a_group_without_our_format_in_our_edition_keeps_yes_the_default(monkeypatch, capsys, torrent) -> None:
    defaults = _prompts(monkeypatch, "200", "")
    results = [_result(200, torrents=[torrent])]
    release = _rls_data()

    group_id = anyio.run(dupe_checker.resolve_existing_group, _site(), ["s"], results, None, True, release)
    assert group_id == 200
    assert defaults[1] == "Y"
    assert "DUPE RISK" not in capsys.readouterr().out


def test_a_pasted_group_is_checked_against_the_group_fetched_to_print_it(monkeypatch, capsys) -> None:
    fetched: list[int] = []

    async def torrentgroup(group_id: int) -> dict[str, Any]:
        fetched.append(group_id)
        group = {"id": group_id, "name": "Album", "year": 2020, "musicInfo": {"artists": [{"name": "Artist"}]}}
        return {"group": group, "torrents": [_torrent()]}

    defaults = _prompts(monkeypatch, "777", "")
    release = _rls_data()

    with pytest.raises(click.Abort):
        anyio.run(dupe_checker.resolve_existing_group, _site(torrentgroup=torrentgroup), ["s"], [], None, True, release)
    # One request: the one that prints the group.
    assert fetched == [777]
    assert defaults[1] == "a"
    assert "DUPE RISK" in capsys.readouterr().out


# Trackers named with -t


def test_t_takes_a_comma_separated_list_or_repeats(monkeypatch) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])

    named = anyio.run(salmon.trackers.validate_trackers, None, "trackers", ("red, OPS", "dic", "RED"))
    assert named == ("RED", "OPS", "DIC")


def test_no_t_runs_the_first_time_choice(monkeypatch) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["OPS"])

    assert anyio.run(salmon.trackers.validate_trackers, None, "trackers", ()) == ("OPS",)


def test_no_tracker_chosen_aborts(monkeypatch) -> None:
    monkeypatch.setattr(salmon.trackers, "choose_tracker_first_time", _returning_async(None))

    with pytest.raises(click.Abort):
        anyio.run(salmon.trackers.validate_trackers, None, "trackers", ())


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a dot_torrents_dir, configured."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, torrents


def _uploads_by_tracker(monkeypatch) -> list[str]:
    """Record the tracker of each upload (its transcodes count apart), which runs as it does in salmon."""
    sites: list[str] = []
    real = salmon.uploader.upload_and_report

    async def recording(gazelle_site, *args: Any, **kwargs: Any) -> Any:
        if not sites or sites[-1] != gazelle_site.site_code:
            sites.append(gazelle_site.site_code)
        return await real(gazelle_site, *args, **kwargs)

    monkeypatch.setattr(salmon.uploader, "upload_and_report", recording)
    return sites


@pytest.mark.parametrize("multi_tracker_upload", [True, False])
def test_named_trackers_are_uploaded_to_in_order_without_asking(monkeypatch, tmp_path, dirs, multi_tracker_upload):
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])
    sites = _uploads_by_tracker(monkeypatch)

    # `-t RED` comes from _run_up: with `-t OPS`, two named trackers. A new group on each.
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        args=("-t", "OPS"),
        input="\n\n",
        multi_tracker_upload=multi_tracker_upload,
    )

    assert run.result.exit_code == 0, run.result.output
    assert sites == ["RED", "OPS"]
    assert ANOTHER_TRACKER not in run.result.output
    assert "Next tracker: OPS" in run.result.output


def test_one_named_tracker_still_offers_another(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    sites = _uploads_by_tracker(monkeypatch)

    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\nn\n")

    assert run.result.exit_code == 0, run.result.output
    assert sites == ["RED"]
    assert ANOTHER_TRACKER in run.result.output


def test_a_skipped_named_tracker_moves_on_to_the_next(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    sites = _uploads_by_tracker(monkeypatch)
    real = salmon.uploader.resolve_cover_url

    async def no_cover_on_red(gazelle_site, *args: Any) -> tuple[bool, str | None]:
        if gazelle_site.site_code == "RED":
            return False, None
        return await real(gazelle_site, *args)

    monkeypatch.setattr(salmon.uploader, "resolve_cover_url", no_cover_on_red)

    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-t", "OPS"), input="\n\n")

    assert run.result.exit_code == 0, run.result.output
    assert "Skipping upload to RED" in run.result.output
    assert sites == ["OPS"]
    assert ANOTHER_TRACKER not in run.result.output


def test_a_failed_named_tracker_moves_on_to_the_next(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    sites = _uploads_by_tracker(monkeypatch)
    recording = salmon.uploader.upload_and_report

    async def red_fails(gazelle_site, *args: Any, **kwargs: Any) -> Any:
        if gazelle_site.site_code == "RED":
            raise RequestError("refused")
        return await recording(gazelle_site, *args, **kwargs)

    monkeypatch.setattr(salmon.uploader, "upload_and_report", red_fails)

    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-t", "OPS"), input="\n\n")

    assert run.result.exit_code == 0, run.result.output
    assert "Upload to RED failed: refused" in run.result.output
    assert sites == ["OPS"]


def test_aborting_at_a_named_tracker_keeps_what_is_up(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    sites = _uploads_by_tracker(monkeypatch)

    # A new group on RED, then [a]bort at OPS's group prompt.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-t", "OPS"), input="\na\n")

    assert run.result.exit_code == 0, run.result.output
    assert sites == ["RED"]
    assert "Aborting: nothing more is uploaded. Already uploaded:" in run.result.output


def test_skip_flac_upload_takes_one_named_tracker(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])

    run = _run_up(
        monkeypatch, _album(tmp_path / "Album"), torrents, args=("-t", "OPS", "-g", "55", "--skip-flac-upload")
    )

    assert run.result.exit_code == 2
    assert "--skip-flac-upload uploads to one tracker" in run.result.output
    assert run.tracker.sent == []


def test_naming_the_trackers_sends_what_choosing_them_did(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])

    def requests(run) -> list[tuple[str, str, str | None]]:
        return [(sent.method, sent.path, sent.query.get("action")) for sent in run.tracker.sent]

    # RED, then OPS picked at the "another tracker?" question, as before.
    chosen = _run_up(monkeypatch, _album(tmp_path / "chosen" / "Album"), torrents, input="\nOPS\n\n")
    (Path(cfg.directory.download_directory) / "Artist - Album (2020) [WEB FLAC]").rename(tmp_path / "first-run")
    named = _run_up(monkeypatch, _album(tmp_path / "named" / "Album"), torrents, args=("-t", "OPS"), input="\n\n")

    assert chosen.result.exit_code == 0, chosen.result.output
    assert named.result.exit_code == 0, named.result.output
    assert requests(named) == requests(chosen)


# The downconversion question


def test_no_downconversion_question_when_nothing_can_be_converted(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    mp3 = {"format": "MP3", "encoding": "320", "artists": [("Artist", "main")], "title": "Album", "catno": "CAT1"}

    # A new group, keep the folder name, upload, no lossy report comment. No other tracker.
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        input="\ny\ny\n\n",
        yes_all=False,
        construct_rls_data=_returning(mp3),
    )

    assert run.result.exit_code == 0, run.result.output
    assert "Successfully uploaded" in run.result.output
    assert DOWNCONVERSION not in run.result.output


def test_the_downconversion_question_stays_when_there_is_something_to_convert(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])

    # As above, then no to the downconversion question.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\ny\ny\n\nn\n", yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert DOWNCONVERSION in run.result.output


# --yes-all


def test_yes_all_still_asks_the_prompts_with_a_pre_filled_default(monkeypatch) -> None:
    # yes_all never answered the group, metadata or release type prompts: they still wait for a reply,
    # and only what an empty reply gives has changed.
    monkeypatch.setattr(cfg.upload, "yes_all", True)
    defaults = _prompts(monkeypatch, "", "")

    group_id = anyio.run(
        partial(dupe_checker.resolve_existing_group, _site(), ["s"], [_result(200)], None, release=_rls_data())
    )
    metadata = {"rls_type": None}
    anyio.run(review._check_for_empty_release_type, metadata, "Single")
    assert group_id == 200
    assert defaults == ["1", "Y", "Single"]
