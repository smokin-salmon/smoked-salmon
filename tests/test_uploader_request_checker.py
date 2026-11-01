from typing import TYPE_CHECKING, cast

import anyio
import asyncclick as click
import pytest

from salmon.trackers import validate_request
from salmon.uploader import request_checker
from salmon.uploader.request_checker import _prompt_for_request_id, print_request_results

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


class FakeSite:
    site_string = "RED"
    base_url = "https://tracker.test"

    def request_url(self, request_id: int) -> str:
        return f"{self.base_url}/requests.php?action=view&id={request_id}"


SITE = cast("BaseGazelleApi", cast("object", FakeSite()))


@pytest.mark.parametrize(
    ("given", "expected"), [(" 1234 ", "1234"), ("https://tracker.test/requests.php?action=view&id=99", "99")]
)
def test_a_request_is_an_id_or_this_trackers_request_url(given: str, expected: str) -> None:
    assert validate_request(SITE, given) == expected


@pytest.mark.parametrize(
    "given",
    [
        "not-a-request",
        "https://other.test/requests.php?action=view&id=99",
        "https://tracker.test/requests.php?action=view",
        "4²",
    ],
    ids=["text", "another-tracker", "no-id", "not-ascii"],
)
def test_anything_else_is_refused(given: str) -> None:
    with pytest.raises(click.BadParameter, match="requires a request"):
        validate_request(SITE, given)


def _request(request_id: int, **fields) -> dict:
    return {
        "requestId": request_id,
        "artists": [[{"name": "Artist"}]],
        "title": "Title",
        "year": 2020,
        "releaseType": "Album",
        "bitrateList": ["Lossless"],
        "formatList": ["FLAC"],
        "mediaList": ["WEB"],
        **fields,
    }


def test_a_malformed_request_is_skipped_whole(capsys) -> None:
    no_title = _request(2)
    del no_title["title"]

    print_request_results(SITE, [_request(1), no_title, _request(3, artists=[])], "s")

    out = capsys.readouterr().out
    assert " 01 >> " in out
    assert " 02 >> " not in out and " 03 >> " not in out


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        ("https://tracker.test/requests.php?action=view", "That URL names no request"),
        ("maybe", "is not a request"),
        ("4²", "is not a request"),
    ],
    ids=["no-id", "text", "superscript"],
)
def test_an_answer_naming_no_request_says_so_and_asks_again(monkeypatch, capsys, answer: str, said: str) -> None:
    answers = [answer, "n"]

    async def fake_prompt(*_args, **_kwargs) -> str:
        return answers.pop(0)

    monkeypatch.setattr(request_checker.click, "prompt", fake_prompt)

    assert anyio.run(_prompt_for_request_id, SITE, []) is None
    assert answers == []
    assert said in capsys.readouterr().out
