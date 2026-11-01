"""Helpers of tests/test_interaction_typed_sites.py that other test modules use too."""

from collections.abc import Awaitable, Callable
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest

import salmon.tagger.review as review
import salmon.uploader as uploader
from salmon import interaction
from salmon.errors import AbortAndDeleteFolder, PickOtherResults, SkipTracker
from salmon.uploader import dupe_checker, spectrals


class AskedAgain(Exception):
    """The terminal was asked once more than the script answers."""


class Terminal:
    """asyncclick's prompt and confirm, patched: what each was given, answered from a script."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        monkeypatch.setattr(click, "prompt", self._prompt)
        monkeypatch.setattr(click, "confirm", self._confirm)

    def _answer(self, kind: str, text: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((kind, text, kwargs))
        if not self.answers:
            raise AskedAgain(text)
        return self.answers.pop(0)

    async def _prompt(self, text: str, **kwargs: Any) -> Any:
        return self._answer("prompt", text, kwargs)

    def _confirm(self, text: str, **kwargs: Any) -> bool:
        return self._answer("confirm", text, kwargs)


class Described:
    """The terminal, keeping the description each question came with."""

    def __init__(self) -> None:
        self.terminal = interaction.TerminalInteraction()
        self.options: list[interaction.Options | None] = []
        self.labels: list[tuple[str, str] | None] = []

    async def prompt(self, text: str, **kwargs: Any) -> Any:
        self.options.append(kwargs.get("options"))
        return await self.terminal.prompt(text, **kwargs)

    async def confirm(self, text: str, **kwargs: Any) -> bool:
        self.labels.append(kwargs.get("labels"))
        return await self.terminal.confirm(text, **kwargs)

    async def edit(self, text: str, **kwargs: Any) -> str | None:
        return await self.terminal.edit(text, **kwargs)

    async def show_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        await self.terminal.show_spectrals(spectrals_path, all_spectral_ids)

    async def assume_defaults(self) -> bool:
        return await self.terminal.assume_defaults()


def ask(
    monkeypatch: pytest.MonkeyPatch, site: Callable[[], Awaitable[Any]], answers: list[Any], yyy: bool = False
) -> tuple[Any, Terminal, Described]:
    """Run `site` in the terminal with asyncclick answering `answers`: its result (or the abort raised), the
    terminal's calls, and the descriptions it was given."""
    terminal = Terminal(monkeypatch, answers)
    described = Described()

    async def run() -> Any:
        with interaction.using(described), interaction.assuming_defaults(yyy):
            try:
                return await site()
            except (click.Abort, AbortAndDeleteFolder, SkipTracker, PickOtherResults) as e:
                return type(e)

    return anyio.run(run), terminal, described


def prompt_call(text: str, default: Any, type: Any = None) -> tuple[str, str, dict[str, Any]]:
    """What asyncclick's prompt is given on master for a magenta question."""
    return (
        "prompt",
        click.style(text, fg="magenta"),
        {"default": default, "type": type, "value_proc": None, "show_default": True},
    )


def keys(options: interaction.Options | None) -> list[tuple[str, str]]:
    assert options is not None
    return [(option.key, option.label) for option in options.items]


# The fakes the sites get: a tracker, a group, request results. Nothing reaches a network.
SITE: Any = SimpleNamespace(
    site_string="RED",
    base_url="https://red.test",
    request_url=lambda id_: f"https://red.test/requests.php?action=view&id={id_}",
)


GROUP = {
    "groupId": 7,
    "artist": "Artist",
    "groupName": "Album",
    "groupYear": 2020,
    "torrents": [{"id": 70, "media": "WEB", "format": "FLAC", "encoding": "Lossless"}],
}


def hold_the_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    """The group's edition already holds our release."""
    monkeypatch.setattr(dupe_checker, "held_in_group", lambda rset, _release: rset["torrents"])
    monkeypatch.setattr(dupe_checker, "_catno_note", lambda *_args: "")


def confirm_group(offer_deletion: bool = True, offer_skip: bool = False) -> Callable[[], Awaitable[Any]]:
    return partial(dupe_checker._confirm_group_id, SITE, 7, [GROUP], offer_deletion, offer_skip=offer_skip)


SPECTRAL_IDS = {1: "Intro.flac", 2: "Song.flac", 3: "Outro.flac"}


def spectral_ids(lossy_master: bool = False) -> Callable[[], Awaitable[Any]]:
    return partial(spectrals.prompt_spectrals, SPECTRAL_IDS, lossy_master, True, hosts="catbox")


REQUESTS: list[dict[str, Any]] = [
    {
        "requestId": 11,
        "artists": [[{"name": "Artist"}]],
        "title": "Album",
        "year": 2020,
        "releaseType": "Album",
        "bitrateList": ["Lossless", "24bit Lossless"],
        "formatList": ["FLAC"],
        "mediaList": ["WEB", "CD"],
        "bounty": 104857600,
    },
    # Without its year: the CLI does not print it, but reads 2 as this row; the web lists it by number.
    {"requestId": 12, "artists": [[{"name": "Artist"}]], "title": "Other"},
    {
        "requestId": 13,
        "artists": [[{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}]],
        "title": "Compilation",
        "year": 2019,
        "releaseType": "Compilation",
        "bitrateList": ["Any"],
        "formatList": ["Any"],
        "mediaList": ["Any"],
    },
]


def request_site() -> Any:
    async def api_call(action: str, params: dict[str, Any]) -> dict[str, Any]:
        assert action == "request"
        return {
            "requestId": params["id"],
            "musicInfo": {"artists": [{"name": "Artist"}]},
            "title": "Album",
            "year": 2020,
            "requestorName": "someone",
            "bounty": 104857600,
            "bitrateList": ["Lossless"],
            "formatList": ["FLAC"],
            "mediaList": ["WEB"],
            "bbDescription": "Please.",
        }

    return SimpleNamespace(**vars(SITE), api_call=api_call)


TRACKS_24BIT_48K = {"01.flac": {"sample rate": 48000}}


RELEASE_24BIT = {"format": "FLAC", "encoding": "24bit Lossless"}


CONVERSIONS = ["16bit 48.0 kHz", "MP3 320", "MP3 V0"]


def conversions(held: frozenset[str] = frozenset(), wanted: tuple[str, ...] | None = None) -> Callable[[], Any]:
    async def run() -> Any:
        tasks = await uploader.prompt_downconversion_choice(RELEASE_24BIT, TRACKS_24BIT_48K, held, wanted)
        return [task["name"] for task in tasks]

    return run


def edit_metadata_stubs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub every step of edit_metadata around its question; returns the list each metadata review appends to."""
    reviews: list[str] = []

    async def returns(value: Any = None) -> Any:
        return value

    async def review_metadata_with_ai(metadata: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        reviews.append("reviewed")
        return metadata

    monkeypatch.setattr(uploader, "review_metadata_with_ai", review_metadata_with_ai)
    monkeypatch.setattr(uploader, "tag_files", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "check_tags", lambda *a, **k: returns({}))
    monkeypatch.setattr(uploader, "rename_folder", lambda path, *a, **k: returns(path))
    monkeypatch.setattr(uploader, "rename_files", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "check_folder_structure", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "gather_tags", lambda *a, **k: {})
    monkeypatch.setattr(uploader, "gather_audio_info", lambda *a, **k: {})
    return reviews


def edit_metadata(path: str) -> Callable[[], Awaitable[Any]]:
    metadata = {"scene": False, "genres": ["Pop"]}
    return partial(
        uploader.edit_metadata, path, {}, metadata, None, "WEB", {}, False, False, None, skip_integrity_check=True
    )


FIELDS = {
    "a": "_edit_artists",
    "l": "_alias_artists",
    "t": "_edit_title",
    "g": "_edit_genres",
    "r": "_edit_release_type",
    "y": "_edit_years",
    "e": "_edit_edition_info",
    "c": "_edit_comment",
    "k": "_edit_tracks",
    "u": "_edit_urls",
}


def review_metadata(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[Callable[[], Awaitable[Any]], list[str]]:
    """review_metadata, given `kwargs`, with each field's editor recording its key instead of asking."""
    edited: list[str] = []
    for key, name in FIELDS.items():

        async def editor(_metadata: dict[str, Any], *_args: Any, key: str = key, **_kwargs: Any) -> None:
            edited.append(key)

        monkeypatch.setattr(review, name, editor)
    monkeypatch.setattr(review, "_print_metadata", lambda *_args, **_kwargs: None)
    metadata = {"artists": [("Artist", "main")], "rls_type": "Album", "genres": ["Pop"], "tracks": {}}
    if kwargs.pop("offer_other_results", False):
        # The metadata as the results gave it, before this review.
        kwargs["picked"] = deepcopy(metadata)
    return partial(review.review_metadata, metadata, lambda _metadata: None, False, **kwargs), edited


SEARCH_RESULTS: list[dict[str, Any]] = [
    {
        "groupId": 21,
        "artist": "Tom &amp; Jerry",
        "groupName": "Cat &amp; Mouse",
        "groupYear": 2020,
        "releaseType": "Album",
        "tags": ["pop", "rock &amp; roll"],
    },
    {"groupId": 22, "artist": "Broken"},
    {"groupId": 23, "artist": "B", "groupName": "Two", "groupYear": 2019, "releaseType": "EP", "tags": []},
]


RECENT_UPLOADS = [(31, "Artist", "Album"), (32, "Other", "Thing &amp; More")]


def recent_site() -> Any:
    async def redirect(torrent_id: Any) -> int:
        return int(torrent_id) + 1000

    return SimpleNamespace(site_string="RED", base_url="https://red.test", get_redirect_torrentgroupid=redirect)


def search_group(
    results: list[dict[str, Any]] | None = None,
    offer_deletion: bool = True,
    default: str = "",
    offer_skip: bool = False,
) -> Callable[[], Awaitable[Any]]:
    rows = SEARCH_RESULTS if results is None else results
    return partial(dupe_checker._prompt_for_group_id, SITE, rows, offer_deletion, default, offer_skip)


def recent_group(
    recent: list[tuple] | None = None, offer_deletion: bool = True, offer_skip: bool = False
) -> Callable[[], Awaitable[Any]]:
    rows = RECENT_UPLOADS if recent is None else recent
    return partial(
        dupe_checker._prompt_for_recent_upload_results, recent_site(), rows, "some search", offer_deletion, offer_skip
    )
