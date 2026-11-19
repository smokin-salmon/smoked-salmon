"""An upload into a group goes into the edition the group holds when it names our release in other words.

The runs here go against the local fake tracker of helpers.uploader_dry_run and a fake image host, never a real one.
"""

from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see test_a_flac_and_its_conversions_go_into_the_held_edition)
)
from test_uploader_reviewed_edition import GroupTracker, _group_fetches_before_the_upload, _torrent, _up

from salmon import cfg
from salmon.errors import RequestFailedError
from salmon.uploader.held_edition import edition_in_group, edition_of_source, held_editions, in_edition_of
from salmon.uploader.upload import generate_catno

# R3HAB "Running On Empty" as Qobuz gives it: the label bare, and the UPC as an EAN-13.
OURS: dict[str, Any] = {"year": 2025, "edition_title": None, "label": "SNK", "catno": None, "upc": "0199538185515"}
# As RED and OPS hold it.
HELD_TEXT = {"year": "2025", "edition_title": "", "label": "©SNK", "catno": "199538185515"}


def _held(year: int = 2025, title: str = "", label: str = "©SNK", catno: str = "199538185515", **extra: Any) -> dict:
    return {
        "id": 1,
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remastered": True,
        "remasterYear": year,
        "remasterTitle": title,
        "remasterRecordLabel": label,
        "remasterCatalogueNumber": catno,
        **extra,
    }


ORIGINAL = {
    "id": 2,
    "media": "CD",
    "format": "FLAC",
    "encoding": "Lossless",
    "remastered": False,
    "remasterYear": 0,
    "remasterTitle": "",
    "remasterRecordLabel": "",
    "remasterCatalogueNumber": "",
}


@pytest.fixture(autouse=True)
def _upc_as_catno(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", True)


@pytest.mark.parametrize(
    ("torrents", "ours", "expected"),
    [
        ([_held()], {}, [HELD_TEXT]),
        (
            [_held(2026, "feat. John Martin", "Spinnin&#39; Records", "0827568002822")],
            {"year": 2026, "label": "Spinnin' Records", "upc": "0827568002822"},
            [
                {
                    "year": "2026",
                    "edition_title": "feat. John Martin",
                    "label": "Spinnin' Records",
                    "catno": "0827568002822",
                }
            ],
        ),
        ([_held(year=2024)], {}, []),
        ([_held(catno="199538185516")], {}, []),
        ([_held(label="Armada")], {}, []),
        ([_held(title="Deluxe")], {"edition_title": "Remixes"}, []),
        ([_held(title="Deluxe", catno="")], {"upc": ""}, []),
        ([_held()], {"upc": ""}, []),
        ([ORIGINAL], {"year": 0, "label": "", "upc": ""}, []),
        ([_held(), _held(id=3, format="MP3", encoding="320")], {}, [HELD_TEXT]),
        (["not a torrent", None, _held()], {}, [HELD_TEXT]),
    ],
    ids=[
        "copyright sign and UPC-A for EAN-13",
        "a title only the site has, tied by the catno, unescaped",
        "another year",
        "another catno",
        "another label",
        "another title",
        "a title only the site has, nothing tying them",
        "a catno only the site has",
        "an original release",
        "one edition held twice",
        "malformed torrents skipped",
    ],
)
def test_held_editions_name_our_release_only_when_its_fields_agree(torrents, ours, expected) -> None:
    assert held_editions({"torrents": torrents}, {**OURS, **ours}) == expected


@pytest.mark.parametrize("group", [[], {"torrents": {"0": _held()}}, {"torrents": None}, {}])
def test_a_group_of_another_shape_holds_no_edition(group: Any) -> None:
    assert held_editions(group, OURS) == []


def _site(group: dict[str, Any] | Exception | None = None) -> Any:
    calls: list[Any] = []

    async def torrentgroup(group_id: Any) -> dict[str, Any]:
        calls.append(group_id)
        if isinstance(group, Exception):
            raise group
        assert group is not None, "no fetch expected"
        return group

    return SimpleNamespace(site_string="RED", torrentgroup=torrentgroup, calls=calls)


def _answer(monkeypatch, answer: bool | None) -> list[tuple[str, Any]]:
    """Answer click.confirm with `answer` (None: fail if asked). Returns the questions asked, with their default."""
    asked: list[tuple[str, Any]] = []

    def confirm(text: str, default: Any = False, **_kwargs: Any) -> bool:
        assert answer is not None, f"asked {text!r}"
        asked.append((click.unstyle(text), default))
        return answer

    monkeypatch.setattr(click, "confirm", confirm)
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    return asked


def _join(site: Any, metadata: dict[str, Any], group: dict[str, Any] | None, group_id: int | None = 555) -> dict:
    return anyio.run(edition_in_group, site, group_id, metadata, group)


def test_the_held_wording_is_used_when_the_user_agrees(monkeypatch, capsys) -> None:
    asked = _answer(monkeypatch, True)
    metadata = dict(OURS)

    joined = _join(_site(), metadata, {"torrents": [_held()]})

    assert asked == [("Use RED's edition fields?", True)]
    output = capsys.readouterr().out
    assert "RED files this release as: 2025 / ©SNK / 199538185515" in output
    assert "Ours reads: 2025 / SNK / 0199538185515" in output
    assert joined == {**OURS, "edition_title": "", "label": "©SNK", "catno": "199538185515"}
    assert metadata == OURS


def test_a_declined_wording_leaves_ours(monkeypatch) -> None:
    _answer(monkeypatch, False)

    assert _join(_site(), OURS, {"torrents": [_held()]}) is OURS


def test_yyy_uses_the_held_wording_without_asking(monkeypatch, capsys) -> None:
    _answer(monkeypatch, None)
    monkeypatch.setattr(cfg.upload, "yes_all", True)

    joined = _join(_site(), OURS, {"torrents": [_held()]})

    assert joined["label"] == "©SNK"
    assert joined["catno"] == "199538185515"
    assert "Using RED's edition fields (-yyy)." in capsys.readouterr().out


def test_an_edition_worded_as_ours_asks_nothing(monkeypatch, capsys) -> None:
    _answer(monkeypatch, None)

    held = _held(label="SNK", catno="0199538185515")

    assert _join(_site(), OURS, {"torrents": [held]}) is OURS
    assert capsys.readouterr().out == ""


def test_several_held_editions_naming_ours_leave_ours_and_say_so(monkeypatch, capsys) -> None:
    _answer(monkeypatch, None)
    torrents = [_held(), _held(id=3, label="SNK", catno="00199538185515")]

    assert _join(_site(), OURS, {"torrents": torrents}) is OURS
    assert "2 editions on RED look like this release: sending ours (2025 / SNK / 0199538185515)." in (
        capsys.readouterr().out
    )


def test_the_group_is_fetched_when_not_given(monkeypatch) -> None:
    _answer(monkeypatch, True)
    site = _site({"torrents": [_held()]})

    joined = _join(site, OURS, None)

    assert site.calls == [555]
    assert joined["label"] == "©SNK"


def test_a_new_group_fetches_nothing(monkeypatch) -> None:
    _answer(monkeypatch, None)
    site = _site()

    assert _join(site, OURS, None, group_id=None) is OURS
    assert site.calls == []


def test_a_failed_fetch_sends_ours_and_says_so(monkeypatch, capsys) -> None:
    _answer(monkeypatch, None)

    assert _join(_site(RequestFailedError("down")), OURS, None) is OURS
    assert (
        "Could not read the editions of group 555 on RED (down): sending ours (2025 / SNK / 0199538185515). "
        "Check the upload is not filed as a second edition."
    ) in capsys.readouterr().out


def test_transcodes_of_a_flac_without_a_catno_go_up_without_one(monkeypatch) -> None:
    _answer(monkeypatch, True)
    source = _held(catno="")

    metadata = anyio.run(edition_of_source, _site(), source, OURS)

    assert generate_catno(metadata) == ""
    assert in_edition_of(source, metadata)


@pytest.mark.usefixtures("image_uploads")
def test_a_flac_and_its_conversions_go_into_the_held_edition(monkeypatch, tmp_path) -> None:
    held = {**_torrent(13, media="CD"), "remasterRecordLabel": "©Label", "remasterCatalogueNumber": "CAT-1"}

    # Pick the group the search found, then upload into it (-yyy takes the held wording).
    run = _up(monkeypatch, tmp_path, GroupTracker([held], found=True), input="1\n\n")

    assert run.result.exit_code == 0, run.result.output
    assert "RED files this release as: 2020 / ©Label / CAT-1" in run.result.output
    posts = [dict(sent.fields) for sent in run.tracker.sent if sent.query.get("action") == "upload"]
    assert [(post["format"], post["remaster_record_label"], post["remaster_catalogue_number"]) for post in posts] == [
        ("FLAC", "©Label", "CAT-1"),
        ("MP3", "©Label", "CAT-1"),
        ("MP3", "©Label", "CAT-1"),
    ]
    # The group the review re-check fetched: its editions are not fetched again.
    assert _group_fetches_before_the_upload(run) == 1
