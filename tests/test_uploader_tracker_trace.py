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

import pytest
from helpers.tracker_trace import (
    CD_FLAC,
    GROUP_ROW,
    Scenario,
    Site,
    World,
    order_across_trackers,
    run_scenario,
)
from test_run_exit_status import dirs  # noqa: F401 (dirs is a fixture)

TRACES = Path(__file__).parent / "fixtures" / "tracker_trace"
TWO = ("RED", "OPS")
THREE = ("RED", "OPS", "DIC")
HOLDS_THE_CD = Site(browse=(GROUP_ROW,), group_torrents=(CD_FLAC,))
RED_REFUSES = {"RED": Site(uploads=("refused",))}


# Each scenario is a row of #982's table: S2 is "s02...", its variants "s02_...". Where the same scenario runs through
# salmon web, its CLI row is named here and the web one ends in "_web".
SCENARIOS: dict[str, Scenario] = {
    "s01_two_trackers_ops_offered": Scenario(
        "Two trackers, RED the default, OPS offered, both up", defaults=("RED",), stdin="\nOPS\n\nn\n"
    ),
    "s02_first_refused": Scenario("RED refuses the FLAC POST, OPS goes on", defaults=TWO, sites=RED_REFUSES),
    "s01_web_job": Scenario("S1 as a salmon web upload job", web=True),
    "s03_answer_lost": Scenario(
        "RED's POST answers 500: looked up twice by infohash, then OPS", sites={"RED": Site(uploads=("lost",))}
    ),
    "s12_seedbox_copy_fails": Scenario("Everything up, the seedbox copy fails", copy_exit=1),
    "s15_first_search_fails": Scenario(
        "RED's group search fails: RED skipped, OPS up", sites={"RED": Site(fail={"browse": 502})}
    ),
    "s24a_three_second_skipped": Scenario(
        "Three trackers: OPS's browse finds a group, answered s",
        trackers=THREE,
        sites={"OPS": HOLDS_THE_CD},
        stdin="\ns\n\nN\n",
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
