"""The cross-upload form is the one chodeus's fork sends on the live trackers, but for the differences listed here.

tests/fixtures/cross_upload/fork-compiled.json holds what the fork's _compile_data gave for each OPS fixture (OPS
to RED). Our form must equal it field for field, once each difference listed in DIFFERENCES is applied: a field
the fork sends that we drop, one we add, or a value we change without listing it here fails.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from salmon.cross_upload import compile_data
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.upload import upload_footer

FIXTURES = Path(__file__).parent / "fixtures" / "cross_upload"
FORK = json.loads((FIXTURES / "fork-compiled.json").read_text(encoding="utf-8"))
SAMPLES = sorted(FORK["compiled"])

# The fork's header, before the source description, and its footer, after it.
FORK_HEADER = re.compile(r"\A\[align=center\].*?\[/align\]\n\n", re.DOTALL)
FORK_FOOTER = (
    f"\n\n[hr]Uploaded with [url=https://github.com/chodeus/smoked-salmon][b]smoked-salmon[/b] "
    f"v{FORK['fork_version']} (chodeus fork)[/url] of [url=https://github.com/smokin-salmon/smoked-salmon]"
    "smokin-salmon[/url]"
)


def _without_header_and_with_our_footer(value: str) -> str:
    description = FORK_HEADER.sub("", value, count=1).removesuffix(FORK_FOOTER)
    return f"{description}\n\n{upload_footer()}" if description else upload_footer()


def _with_size_closed(value: str) -> str:
    return value.replace("[b][size=4]Tracklist[/b]", "[b][size=4]Tracklist[/size][/b]")


# field: (why ours differs from the fork's, what turns the fork's value into ours)
DIFFERENCES: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "release_desc": (
        "No header naming the source tracker, linking to the source torrent or naming its uploader: it is not "
        "confirmed yet that a description may (design claim R9, kept on its safe side). Upstream's footer instead "
        "of the fork's, and no blank lines before it when the source description is empty.",
        _without_header_and_with_our_footer,
    ),
    "album_desc": (
        "The one broken shape the old description generator left, [b][size=4]Tracklist[/b] with no [/size], "
        "is repaired (design section 10, #597). Nothing else in it is rewritten.",
        _with_size_closed,
    ),
}


def _response(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["response"]


def _ours(name: str) -> dict[str, Any]:
    return compile_data(_response(name), OpsApi(), RedApi())


@pytest.mark.parametrize("name", SAMPLES)
def test_the_form_is_the_forks_but_for_the_differences_listed(name: str) -> None:
    fork = FORK["compiled"][name]
    ours = _ours(name)

    assert list(ours) == list(fork)
    expected = {key: DIFFERENCES[key][1](value) if key in DIFFERENCES else value for key, value in fork.items()}
    assert ours == expected


@pytest.mark.parametrize("key", sorted(DIFFERENCES))
def test_each_listed_difference_shows_in_some_sample(key: str) -> None:
    # A listed difference no sample shows any more is out of date: take it off the list.
    differing = [name for name in SAMPLES if _ours(name)[key] != FORK["compiled"][name][key]]
    assert differing


def test_the_samples_cover_a_cd_with_a_log_and_web_editions() -> None:
    media = sorted(_response(name)["torrent"]["media"] for name in SAMPLES)
    assert media == ["CD", "WEB", "WEB"]
