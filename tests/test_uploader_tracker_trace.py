"""What each tracker sees during `salmon up`, kept as a trace: one file per scenario in tests/fixtures/tracker_trace.

The scenarios run through run.main against a fake server per tracker, a recording image host and seedbox, and a fake
clock (helpers.tracker_trace), and their trace is today's behaviour, right or wrong, established by running it. Each
tracker's own request sequence is compared strictly, as is everything else in the trace. The order of requests
across trackers is printed beside it and never compared.

A change to `salmon up` that means to change what a tracker or the user sees updates the traces it changes, and says
why. When one changes, the failure names the scenario and section and prints the new trace, ready to paste, or run
this file with UPDATE_TRACES=1 to rewrite them.
"""

import difflib
import itertools
import os
import re
from pathlib import Path
from typing import Any

import pytest
from helpers.tracker_trace import (
    ANY_STDIN,
    CD_FLAC,
    GROUP_ROW,
    REQUEST_ROW,
    Scenario,
    Site,
    World,
    order_across_trackers,
    run_scenario,
)
from helpers.uploader_dry_run import METADATA, RLS_DATA, _lossy_with_spectrals, _returning_async, _transcode
from test_run_exit_status import _forbidden_on, dirs  # noqa: F401 (dirs is a fixture)

from salmon import cfg
from salmon.errors import UploadError
from salmon.uploader import spectrals

TRACES = Path(__file__).parent / "fixtures" / "tracker_trace"
TWO = ("RED", "OPS")
THREE = ("RED", "OPS", "DIC")
HOLDS_THE_CD = Site(browse=(GROUP_ROW,), group_torrents=(CD_FLAC,))
RED_REFUSES = {"RED": Site(uploads=("refused",))}
# A title that cleans to nothing leaves no search string (#931), and no artists to search by either.
NO_SEARCH = {"artists": [("Various Artists", "main")], "title": "(Remastered Edition)", "catno": ""}


def _no_search_after_review() -> dict[str, Any]:
    metadata = {**METADATA, **NO_SEARCH}
    return {"get_metadata": _returning_async((metadata, None)), "review_metadata_with_ai": _returning_async(metadata)}


def _no_search_at_all() -> dict[str, Any]:
    return {"construct_rls_data": _returning_async({**RLS_DATA, **NO_SEARCH}), **_no_search_after_review()}


def _no_v0_on_the_second_tracker() -> dict[str, Any]:
    turns = itertools.count()

    async def transcode(path: str, bitrate: str, *args: Any, **kwargs: Any) -> str:
        if bitrate == "V0" and next(turns) == 1:
            raise UploadError("Not overwriting existing folder.")
        return await _transcode(path, bitrate, *args, **kwargs)

    return {"transcode_folder": transcode}


def _sixteen_bit_refused_by(site_code: str) -> dict[str, Any]:
    return {"_sixteen_bit_refusal": lambda tracker, *_args, **_kwargs: tracker == site_code}


def _no_cover(_monkeypatch, album: Path) -> None:
    (album / "cover.jpg").unlink()


def _last_minute_check_only(monkeypatch, _album_path: Path) -> None:
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", True)
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", False)


def _holds_both_mp3s() -> tuple[dict[str, Any], ...]:
    return (
        {**CD_FLAC, "id": 21, "media": "WEB"},
        {**CD_FLAC, "id": 22, "media": "WEB", "format": "MP3", "encoding": "320"},
        {**CD_FLAC, "id": 23, "media": "WEB", "format": "MP3", "encoding": "V0 (VBR)"},
    )


def _spectrals_found_after_the_upload(monkeypatch, _album_path: Path) -> None:
    monkeypatch.setattr(spectrals, "check_spectrals", _lossy_with_spectrals)


# Each scenario is a row of #982's table: S2 is "s02...", its variants "s02_...". Where the same scenario runs through
# salmon web, its CLI row is named here and the web one ends in "_web_job".
SCENARIOS: dict[str, Scenario] = {
    "s01_two_trackers_ops_offered": Scenario(
        "Two trackers, RED the default, OPS offered, both up", defaults=("RED",), stdin="\nOPS\n\nn\n"
    ),
    "s01_multi_tracker_off": Scenario(
        "One tracker: the default RED, with multi_tracker_upload off", defaults=("RED",), multi_tracker=False
    ),
    "s02_first_refused": Scenario("RED refuses the FLAC POST, OPS goes on", defaults=TWO, sites=RED_REFUSES),
    "s02_multi_tracker_off": Scenario(
        "RED refuses, multi_tracker_upload off: no other tracker",
        defaults=("RED",),
        sites=RED_REFUSES,
        multi_tracker=False,
    ),
    "s01_web_job": Scenario("S1 as a salmon web upload job", web=True),
    "s02_web_job": Scenario("S2 as a salmon web upload job", sites=RED_REFUSES, web=True),
    "s03_answer_lost": Scenario(
        "RED's POST answers 500: looked up twice by infohash, then OPS", sites={"RED": Site(uploads=("lost",))}
    ),
    "s04_first_listed": Scenario(
        "Do-Not-Upload on RED: nothing sent to RED, OPS up", fakes=lambda: _forbidden_on("RED")
    ),
    "s05_two_named": Scenario("-t RED -t OPS -yyy, both up"),
    "s06_group_found_on_first": Scenario(
        "RED's browse finds group 55 (a CD), picked and confirmed",
        defaults=("RED",),
        sites={"RED": HOLDS_THE_CD},
        stdin="1\n\nOPS\n\nn\n",
    ),
    "s07_group_found_on_second_skipped": Scenario(
        "OPS's browse finds a group, answered s",
        defaults=("RED",),
        sites={"OPS": HOLDS_THE_CD},
        stdin="\nOPS\ns\n",
    ),
    "s08_request_filled_on_first": Scenario(
        "RED's requests search has 901, picked: the FLAC fills it",
        defaults=("RED",),
        sites={"RED": Site(requests=(REQUEST_ROW,))},
        stdin="\n1\nOPS\n\nn\n",
    ),
    "s08_web_job": Scenario(
        "S8 as a salmon web upload job",
        sites={"RED": Site(requests=(REQUEST_ROW,))},
        web=True,
        web_answers=("", {"picks": ["1"], "texts": [], "mark": None}),
    ),
    "s09_named_request_refused": Scenario(
        "--request on RED, whose POST fails: OPS does not get it",
        defaults=TWO,
        args=("--request", "4242"),
        sites=RED_REFUSES,
    ),
    "s10_no_search_on_later_tracker": Scenario(
        "-g 7 on RED, no search string: OPS skipped under -yyy",
        args=("-g", "7"),
        sites={"RED": Site(group_torrents=(CD_FLAC,))},
        fakes=_no_search_after_review,
    ),
    "s10_no_search_dic_third": Scenario(
        "The same with DIC third: DIC skipped the same way",
        trackers=THREE,
        args=("-g", "7"),
        sites={"RED": Site(group_torrents=(CD_FLAC,))},
        fakes=_no_search_after_review,
    ),
    "s11_no_search_on_first": Scenario(
        "No search string on the first tracker, -yyy: nothing up", fakes=_no_search_at_all
    ),
    "s12_seedbox_copy_fails": Scenario("Everything up, the seedbox copy fails", copy_exit=1),
    "s13_conversions_only_on_second": Scenario(
        "RED's group already holds both MP3s: only OPS gets them",
        sites={"RED": Site(group_torrents=_holds_both_mp3s())},
    ),
    "s14_conversion_fails_before_its_post": Scenario(
        "On OPS the V0 can't be made: the FLAC and 320 up, the run fails", fakes=_no_v0_on_the_second_tracker
    ),
    "s15_first_search_fails": Scenario(
        "RED's group search fails: RED skipped, OPS up", sites={"RED": Site(fail={"browse": 502})}
    ),
    "s16_no_group_id": Scenario(
        "RED's answer names no group: no conversions there, OPS up", sites={"RED": Site(uploads=("no group",))}
    ),
    "s17_no_cover_on_new_group": Scenario(
        "No cover image: RED into its group, OPS's new group skipped",
        sites={"RED": HOLDS_THE_CD},
        setup=_no_cover,
        stdin="1\n\n\n" + ANY_STDIN,
    ),
    "s18_repeat_run": Scenario(
        "S5 twice on a folder at its final name: the second run sends it again", at_final_name=True, runs=2
    ),
    "s19_skip_flac_upload": Scenario(
        "--skip-flac-upload into group 55: transcodes only",
        trackers=("RED",),
        args=("-g", "55", "--skip-flac-upload"),
    ),
    "s20_last_minute_log_unreadable": Scenario(
        "Last-minute check on, its site log unreadable, -yyy",
        trackers=("RED",),
        sites={"RED": Site(cookie="test-session", fail={"/log.php": 502})},
        setup=_last_minute_check_only,
    ),
    "s21_first_refused_by_rate": Scenario(
        "16-bit refusal on RED, OPS up", fakes=lambda: _sixteen_bit_refused_by("RED")
    ),
    "s22_abort_at_second_group": Scenario("RED up, abort at OPS's group prompt", defaults=("RED",), stdin="\nOPS\na\n"),
    "s23_spectrals_after": Scenario(
        "--spectrals-after, two trackers: the check after RED's FLAC",
        args=("--spectrals-after",),
        setup=_spectrals_found_after_the_upload,
    ),
    "s24a_three_second_skipped": Scenario(
        "Three trackers: OPS's browse finds a group, answered s",
        trackers=THREE,
        sites={"OPS": HOLDS_THE_CD},
        stdin="\ns\n\nN\n",
    ),
    "s24b_three_no_cover_on_second": Scenario(
        "Three, no cover: RED and DIC into group 55, OPS's new group skipped",
        trackers=THREE,
        sites={"RED": HOLDS_THE_CD, "DIC": HOLDS_THE_CD},
        setup=_no_cover,
        stdin="1\n\n\n1\n\nN\n",
    ),
    "s25_abort_at_conversion_prompt": Scenario(
        "No -yyy: stdin runs out at RED's conversion question after its FLAC is up",
        yes_all=False,
        trackers=("RED",),
        stdin="\n" * 5,
    ),
    "s26_second_listed_three": Scenario(
        "Three, Do-Not-Upload on OPS: nothing sent to OPS", trackers=THREE, fakes=lambda: _forbidden_on("OPS")
    ),
    "s26_second_rate_refused_three": Scenario(
        "Three, 16-bit refusal on OPS: nothing sent to OPS",
        trackers=THREE,
        fakes=lambda: _sixteen_bit_refused_by("OPS"),
    ),
    "s27_second_search_fails_three": Scenario(
        "Three, OPS's group search fails: DIC goes on", trackers=THREE, sites={"OPS": Site(fail={"browse": 502})}
    ),
    "s28_first_refused_then_stdin_ends": Scenario(
        "RED refused, then stdin runs out at OPS's group prompt", defaults=TWO, sites=RED_REFUSES, stdin="\n"
    ),
    "s29_dry_run": Scenario("--dry-run, two trackers", args=("--dry-run",)),
    "s29_dry_run_aborted": Scenario("A dry run aborted at OPS's group prompt", args=("--dry-run",), stdin="\na\n"),
    "s30_declined_another_tracker": Scenario('"Another tracker?" answered n', defaults=("RED",), stdin="\nn\n"),
    "s30_another_tracker_by_letter": Scenario('"Another tracker?" answered o', defaults=("RED",), stdin="\no\n\n"),
    "s31a_found_request_refused": Scenario(
        "A found request on RED, its POST refused: OPS searches its own",
        defaults=TWO,
        sites={"RED": Site(requests=(REQUEST_ROW,), uploads=("refused",))},
        stdin="\n1\n" + ANY_STDIN,
    ),
    "s31b_named_request_first_listed": Scenario(
        "--request, RED refused before the loop: not filled, OPS up",
        args=("--request", "4242"),
        fakes=lambda: _forbidden_on("RED"),
    ),
    "s32_conversion_answer_lost": Scenario(
        "The 320's POST answers 500: V0 not tried, OPS up", sites={"RED": Site(uploads=(None, "lost"))}
    ),
    "s33_group_read_fails_after_upload": Scenario(
        "RED's torrentgroup GET fails after a good FLAC POST",
        sites={"RED": Site(fail={"torrentgroup": 502})},
    ),
    "s34_request_search_fails": Scenario(
        "RED's requests search fails: RED marked failed, OPS up", sites={"RED": Site(fail={"requests": 502})}
    ),
    "s35_spectrals_after_first_refused": Scenario(
        "--spectrals-after, RED refuses the FLAC: OPS goes on",
        args=("--spectrals-after",),
        sites=RED_REFUSES,
        setup=_spectrals_found_after_the_upload,
    ),
}


def _sections(text: str) -> dict[str, str]:
    """A trace's sections, by name."""
    parts = re.split(r"^== (.+) ==$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _changes(name: str, expected: str, got: str) -> str:
    """What failed: which sections of which scenario changed, how, and the new trace to paste."""
    old, new = _sections(expected), _sections(got)
    changed = [section for section in dict.fromkeys([*old, *new]) if old.get(section) != new.get(section)]
    diffs = []
    for section in changed:
        lines = difflib.unified_diff(
            old.get(section, "").splitlines(), new.get(section, "").splitlines(), "stored", "this run", lineterm=""
        )
        diffs.append(f"[{section}]\n" + "\n".join(itertools.islice(lines, 60)))
    return (
        f"{name}: the trace changed in: {', '.join(changed)}\n\n" + "\n\n".join(diffs) + f"\n\nIf the change is meant, "
        f"set {TRACES / f'{name}.txt'} to the text below, or run with UPDATE_TRACES=1:\n\n{got}"
    )


def _check(name: str, trace: str, world: World) -> None:
    path = TRACES / f"{name}.txt"
    if os.environ.get("UPDATE_TRACES"):
        path.write_text(trace, encoding="utf-8")
    expected = path.read_text(encoding="utf-8") if path.exists() else ""
    print(f"order across trackers: {order_across_trackers(world)} (reported, not compared)")
    if trace != expected:
        pytest.fail(_changes(name, expected, trace), pytrace=False)


@pytest.mark.parametrize("name", [name for name, scenario in SCENARIOS.items() if not scenario.web])
def test_trace(monkeypatch, capsys, dirs, name: str) -> None:  # noqa: F811
    trace, world = run_scenario(monkeypatch, capsys, SCENARIOS[name], dirs)
    _check(name, trace, world)


@pytest.mark.parametrize("name", [name for name, scenario in SCENARIOS.items() if scenario.web])
def test_trace_of_a_web_job(monkeypatch, dirs, name: str) -> None:  # noqa: F811
    trace, world = run_scenario(monkeypatch, None, SCENARIOS[name], dirs)
    _check(name, trace, world)


def test_every_trace_file_has_a_scenario() -> None:
    assert {path.stem for path in TRACES.glob("*.txt")} == set(SCENARIOS)
