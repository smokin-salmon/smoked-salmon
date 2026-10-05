"""The cross-upload form is the one chodeus's fork sends on the live trackers, but for the differences listed here.

tests/fixtures/cross_upload/fork-compiled.json holds what the fork's _compile_data gave for each torrent fixture: the
OPS ones OPS to RED, the RED ones RED to OPS. Our form must equal it field for field, once each difference listed in
DIFFERENCES is applied: a field the fork sends that we drop, one we add, or a value we change without listing it here
fails.
"""

import html
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from salmon.cross_upload import compile_data, without_tracker_links
from salmon.release_notification import get_version
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.upload import upload_footer

FIXTURES = Path(__file__).parent / "fixtures" / "cross_upload"
FORK = json.loads((FIXTURES / "fork-compiled.json").read_text(encoding="utf-8"))
SAMPLES = sorted(FORK["compiled"])

# The fork's credit line in its header, and its footer, after the source description.
FORK_CREDIT = (
    "Cross-uploaded with [url=https://github.com/chodeus/smoked-salmon]smoked-salmon[/url] "
    f"v{FORK['fork_version']} (chodeus fork)"
)
FORK_FOOTER = (
    f"\n\n[hr]Uploaded with [url=https://github.com/chodeus/smoked-salmon][b]smoked-salmon[/b] "
    f"v{FORK['fork_version']} (chodeus fork)[/url] of [url=https://github.com/smokin-salmon/smoked-salmon]"
    "smokin-salmon[/url]"
)
SITES = (OpsApi(), RedApi())


def _upstream_credit_no_tracker_links_and_our_footer(value: str, _sample: str) -> str:
    header, _, description = value.partition("[/align]\n\n")
    credit = (
        f"Cross-uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]smoked-salmon[/url] v{get_version()}"
    )
    footer = ""
    if description.endswith(FORK_FOOTER):
        description, footer = description.removesuffix(FORK_FOOTER), f"\n\n{upload_footer()}"
    return f"{header.replace(FORK_CREDIT, credit)}[/align]\n\n{without_tracker_links(description, SITES)}{footer}"


def _reds_unescaped_no_tracker_links_and_size_closed(value: str, sample: str) -> str:
    value = without_tracker_links(html.unescape(value) if sample.startswith("red-") else value, SITES)
    return value.replace("[b][size=4]Tracklist[/b]", "[b][size=4]Tracklist[/size][/b]")


# field: (why ours differs from the fork's, what turns the fork's value for a sample into ours)
DIFFERENCES: dict[str, tuple[str, Callable[[Any, str], Any]]] = {
    "release_desc": (
        'The fork\'s header, but its credit line links to upstream, with no "(chodeus fork)". Links to either '
        "tracker's site, and Gazelle's tags that open the site's own pages ([torrent], [pl], [collage], [forum], "
        "[thread]; [user] and [rule] keep their text), are taken out of the source description, their text kept: "
        "on the target they would name the source tracker's pages. Upstream's footer instead of the fork's.",
        _upstream_credit_no_tracker_links_and_our_footer,
    ),
    "album_desc": (
        "RED's comes HTML-escaped (bbBody): HTML entities decoded, as the fork does for the torrent description. "
        "OPS's (wikiBBcode) is the text as written and is not decoded. Links to either tracker's site and Gazelle's "
        "site tags are taken out, as in release_desc. The one broken shape the old description generator left, "
        "[b][size=4]Tracklist[/b] with no [/size], is repaired (design section 10, #597). Nothing else in it is "
        "rewritten.",
        _reds_unescaped_no_tracker_links_and_size_closed,
    ),
}


def _response(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["response"]


def _ours(name: str) -> dict[str, Any]:
    source, target = (OpsApi(), RedApi()) if name.startswith("ops-") else (RedApi(), OpsApi())
    return compile_data(_response(name), source, target)


@pytest.mark.parametrize("name", SAMPLES)
def test_the_form_is_the_forks_but_for_the_differences_listed(name: str) -> None:
    fork = FORK["compiled"][name]
    ours = _ours(name)

    assert list(ours) == list(fork)
    expected = {key: DIFFERENCES[key][1](value, name) if key in DIFFERENCES else value for key, value in fork.items()}
    assert ours == expected


@pytest.mark.parametrize("key", sorted(DIFFERENCES))
def test_each_listed_difference_shows_in_some_sample(key: str) -> None:
    # A listed difference no sample shows any more is out of date: take it off the list.
    differing = [name for name in SAMPLES if _ours(name)[key] != FORK["compiled"][name][key]]
    assert differing


def test_the_samples_cover_both_directions_with_a_cd_with_a_log_and_web_editions() -> None:
    media = sorted((name[:3], _response(name)["torrent"]["media"]) for name in SAMPLES)
    assert media == [("ops", "CD"), ("ops", "WEB"), ("ops", "WEB"), ("red", "CD"), ("red", "WEB")]
