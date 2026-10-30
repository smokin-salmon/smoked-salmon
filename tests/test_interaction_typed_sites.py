"""The questions that describe their answers for salmon web ask the terminal exactly as before (ADR 0006, #760).

For each converted call site: the text and default asyncclick is given are master's, copied here; every key its
description lists, typed in the terminal, is accepted without the question coming back; and where the site has a
`-yyy` branch, asyncclick is not called and the result is master's. No tracker: the sites get fakes that answer from
memory.
"""

from collections.abc import Awaitable, Callable
from functools import partial
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest

import salmon.tagger.review as review
import salmon.uploader as uploader
from salmon import cfg, interaction, trackers
from salmon.config.validations import CONVERSION_KINDS
from salmon.errors import AbortAndDeleteFolder
from salmon.uploader import dupe_checker, request_checker, spectrals


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
            except (click.Abort, AbortAndDeleteFolder) as e:
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


@pytest.fixture(autouse=True)
def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.image, "default_spectral_ids", None)


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


def confirm_group(offer_deletion: bool = True) -> Callable[[], Awaitable[Any]]:
    return partial(dupe_checker._confirm_group_id, SITE, 7, [GROUP], offer_deletion)


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
    # Printed without its year: the CLI skips the row, the web leaves it out.
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


def review_metadata(monkeypatch: pytest.MonkeyPatch) -> tuple[Callable[[], Awaitable[Any]], list[str]]:
    """review_metadata with each field's editor recording its key instead of asking."""
    edited: list[str] = []
    for key, name in FIELDS.items():

        async def editor(_metadata: dict[str, Any], *_args: Any, key: str = key, **_kwargs: Any) -> None:
            edited.append(key)

        monkeypatch.setattr(review, name, editor)
    monkeypatch.setattr(review, "_print_metadata", lambda *_args, **_kwargs: None)
    metadata = {"artists": [("Artist", "main")], "rls_type": "Album", "genres": ["Pop"], "tracks": {}}
    return partial(review.review_metadata, metadata, lambda _metadata: None, False), edited


# --- No existing test changed: see the PR. Each site's text, default and keys follow. ---------------------------------


# #8 dupe_checker._confirm_group_id: upload to this group


@pytest.mark.parametrize(
    ("offer_deletion", "held", "text", "default"),
    [
        (
            True,
            False,
            "\nAre you sure you would you like to upload this torrent to this group? [Y]es, [n]ew group, [a]bort, "
            "[d]elete music folder",
            "Y",
        ),
        (
            False,
            True,
            "\nAre you sure you would you like to upload this torrent to this group? [Y]es, [n]ew group, [a]bort",
            "a",
        ),
    ],
)
def test_upload_to_this_group_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, offer_deletion: bool, held: bool, text: str, default: str
) -> None:
    if held:
        hold_the_edition(monkeypatch)
    result, terminal, _described = ask(monkeypatch, confirm_group(offer_deletion), ["y"])
    assert result is True
    assert terminal.calls == [prompt_call(text, default)]


@pytest.mark.parametrize("offer_deletion", [True, False])
def test_every_key_of_upload_to_this_group_is_accepted(monkeypatch: pytest.MonkeyPatch, offer_deletion: bool) -> None:
    _result, _terminal, described = ask(monkeypatch, confirm_group(offer_deletion), ["y"])
    expected = {"y": True, "n": False, "a": click.Abort} | ({"d": AbortAndDeleteFolder} if offer_deletion else {})
    assert keys(described.options[0]) == [
        ("y", "Upload to this group"),
        ("n", "New group"),
        ("a", "Abort"),
        *([("d", "Delete music folder")] if offer_deletion else []),
    ]
    options = described.options[0]
    assert options is not None
    assert [(o.action, o.danger) for o in options.items] == [(False, False), (False, False), (True, False)] + (
        [(True, True)] if offer_deletion else []
    )
    for key, outcome in expected.items():
        result, terminal, _described = ask(monkeypatch, confirm_group(offer_deletion), [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


# #10 spectrals.prompt_lossy_master: lossy master


@pytest.mark.parametrize(
    ("marks_found", "offer_deletion", "text", "default"),
    [
        (
            False,
            True,
            "\nIs this release lossy mastered? [y]es, [N]o, [r]eopen spectrals, [a]bort, [d]elete music folder",
            "n",
        ),
        (True, False, "\nIs this release lossy mastered? [Y]es, [n]o, [r]eopen spectrals, [a]bort", "y"),
    ],
)
def test_lossy_master_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, marks_found: bool, offer_deletion: bool, text: str, default: str
) -> None:
    site = partial(spectrals.prompt_lossy_master, False, offer_deletion, marks_found)
    result, terminal, _described = ask(monkeypatch, site, ["n"])
    assert result is False
    assert terminal.calls == [prompt_call(text, default, click.STRING)]


@pytest.mark.parametrize("offer_deletion", [True, False])
def test_every_key_of_lossy_master_is_accepted(monkeypatch: pytest.MonkeyPatch, offer_deletion: bool) -> None:
    site = partial(spectrals.prompt_lossy_master, False, offer_deletion, False)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    assert keys(described.options[0]) == [
        ("y", "Lossy mastered"),
        ("n", "Not lossy mastered"),
        ("r", "Reopen spectrals"),
        ("a", "Abort"),
        *([("d", "Delete music folder")] if offer_deletion else []),
    ]
    expected = {"y": True, "n": False, "r": None, "a": click.Abort} | (
        {"d": AbortAndDeleteFolder} if offer_deletion else {}
    )
    for key, outcome in expected.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


def test_lossy_master_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(spectrals.prompt_lossy_master, False, True, False)
    result, terminal, _described = ask(monkeypatch, site, [], yyy=True)
    assert result is False
    assert terminal.calls == []


# #11 spectrals.prompt_spectrals: spectral IDs

SPECTRALS_TEXT = (
    "What spectral IDs would you like to upload to catbox? "
    '(space-separated list of IDs, "0" for none, "*" for all, or "+" for a randomized selection)'
)


@pytest.mark.parametrize(("lossy_master", "default"), [(False, "+"), (True, "*")])
def test_spectral_ids_asks_as_on_master(monkeypatch: pytest.MonkeyPatch, lossy_master: bool, default: str) -> None:
    monkeypatch.setattr(cfg.image, "default_spectral_ids", None)
    result, terminal, _described = ask(monkeypatch, spectral_ids(lossy_master), ["2"])
    assert result == {2: "Song.flac"}
    assert terminal.calls == [prompt_call(SPECTRALS_TEXT, default)]


def test_every_key_of_spectral_ids_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _result, _terminal, described = ask(monkeypatch, spectral_ids(), ["1"])
    options = described.options[0]
    assert keys(options) == [
        ("1", "01 Intro.flac"),
        ("2", "02 Song.flac"),
        ("3", "03 Outro.flac"),
        ("*", "All"),
        ("0", "None"),
        ("+", "Random third"),
    ]
    assert options is not None and options.several
    assert [o.alone for o in options.items] == [False, False, False, True, True, True]
    for key, outcome in {"1": {1: "Intro.flac"}, "3": {3: "Outro.flac"}, "*": SPECTRAL_IDS, "0": None}.items():
        result, terminal, _described = ask(monkeypatch, spectral_ids(), [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
    result, terminal, _described = ask(monkeypatch, spectral_ids(), ["+"])
    assert len(result) == 1 and result.items() <= SPECTRAL_IDS.items()
    assert len(terminal.calls) == 1
    # Several picks make one line, in the order of the options.
    result, terminal, _described = ask(monkeypatch, spectral_ids(), ["1 3"])
    assert result == {1: "Intro.flac", 3: "Outro.flac"}


@pytest.mark.parametrize(
    ("configured", "lossy_master", "outcome"),
    [(None, True, SPECTRAL_IDS), ("1 3", False, {1: "Intro.flac", 3: "Outro.flac"}), ("0", False, None)],
)
def test_spectral_ids_with_yyy_is_not_asked(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, lossy_master: bool, outcome: Any
) -> None:
    monkeypatch.setattr(cfg.image, "default_spectral_ids", configured)
    result, terminal, _described = ask(monkeypatch, spectral_ids(lossy_master), [], yyy=True)
    assert result == outcome
    assert terminal.calls == []


# #14 review.review_metadata: fields to edit

FIELDS_TEXT = (
    "\nAre there any metadata fields you would like to edit? [a]rtists, "
    "artist a[L]iases (rename or split an artist on every track), [t]itle, [g]enres, "
    "[r]elease type, [y]ears, [e]dition info, [c]omment, trac[k]s, [u]rls, [n]othing"
)


def test_fields_to_edit_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    site, edited = review_metadata(monkeypatch)
    _result, terminal, _described = ask(monkeypatch, site, ["n"])
    assert terminal.calls == [prompt_call(FIELDS_TEXT, None)]
    assert edited == []


def test_every_key_of_fields_to_edit_is_accepted(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    site, edited = review_metadata(monkeypatch)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    assert keys(described.options[0]) == [
        ("a", "Artists"),
        ("l", "Artist aliases"),
        ("t", "Title"),
        ("g", "Genres"),
        ("r", "Release type"),
        ("y", "Years"),
        ("e", "Edition info"),
        ("c", "Comment"),
        ("k", "Tracks"),
        ("u", "URLs"),
        ("n", "Nothing"),
    ]
    for key in FIELDS:
        edited.clear()
        # A field's editor runs, then the review asks again, as it does after every edit; "n" ends it.
        _result, terminal, _described = ask(monkeypatch, site, [key, "n"])
        assert edited == [key]
        assert len(terminal.calls) == 2
    assert "is not a valid editing option" not in capsys.readouterr().out


# #26 uploader.edit_metadata: upload, or re-run the metadata step


def test_upload_or_rerun_asks_as_on_master(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reviews = edit_metadata_stubs(monkeypatch)
    result, terminal, described = ask(monkeypatch, edit_metadata(str(tmp_path)), [True])
    assert result[0] == str(tmp_path)
    assert terminal.calls == [
        (
            "confirm",
            click.style("\nWould you like to upload the torrent? (No to re-run metadata section)", fg="magenta"),
            {"default": True, "abort": False},
        )
    ]
    assert described.labels == [("Upload", "Re-run the metadata step")]
    assert reviews == ["reviewed"]


def test_both_answers_to_upload_or_rerun_are_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reviews = edit_metadata_stubs(monkeypatch)
    # No re-runs the metadata step, then asks again.
    _result, terminal, _described = ask(monkeypatch, edit_metadata(str(tmp_path)), [False, True])
    assert len(terminal.calls) == 2
    assert reviews == ["reviewed", "reviewed"]


def test_upload_or_rerun_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reviews = edit_metadata_stubs(monkeypatch)
    result, terminal, _described = ask(monkeypatch, edit_metadata(str(tmp_path)), [], yyy=True)
    assert result[0] == str(tmp_path)
    assert result[1]["tags"] == "Pop"
    assert terminal.calls == []
    assert reviews == ["reviewed"]


# #31 request_checker._prompt_for_request_id: fill a request


def test_fill_a_request_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, REQUESTS)
    result, terminal, _described = ask(monkeypatch, site, ["n"])
    assert result is None
    assert terminal.calls == [prompt_call("\nFill a request? Choose from results, paste a url, or do[n]t.", "N")]


def test_every_key_of_fill_a_request_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, REQUESTS)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    options = described.options[0]
    # The row printed without its year is left out; the others keep the numbers the CLI prints.
    assert keys(options) == [("1", "Artist - Album"), ("3", "Various Artists - Compilation"), ("n", "Don't fill")]
    assert options is not None
    assert options.items[0].group == "Requests on RED"
    assert options.items[0].card == interaction.Card(
        (
            ("Year", "2020"),
            ("Release type", "Album"),
            ("Requirements", "Lossless or 24bit Lossless / FLAC / WEB or CD"),
            ("Bounty", "100 MiB"),
        ),
        link="https://red.test/requests.php?action=view&id=11",
    )
    assert options.items[1].card is not None
    assert [label for label, _value in options.items[1].card.fields] == ["Year", "Release type", "Requirements"]
    assert options.free_text == interaction.FreeText("Request URL or ID", ids=True)
    for key, outcome in {"1": 11, "3": 13, "n": None}.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
    # What the web sends as a text: a request URL, or an ID above every numbered key.
    for text, outcome in {"https://red.test/requests.php?action=view&id=42": 42, "4": 4}.items():
        result, terminal, _described = ask(monkeypatch, site, [text])
        assert (text, result) == (text, outcome)
        assert len(terminal.calls) == 1


def test_fill_a_request_with_no_results_still_describes_its_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, [])
    result, _terminal, described = ask(monkeypatch, site, ["5"])
    assert result == 5
    assert keys(described.options[0]) == [("n", "Don't fill")]


# #31b request_checker._confirm_request_id: are you sure


def test_fill_this_request_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._confirm_request_id, request_site(), 11)
    result, terminal, _described = ask(monkeypatch, site, ["y"])
    assert result is True
    assert terminal.calls == [prompt_call("\nAre you sure you would you like to fill this request [Y]es, [n]o", "Y")]


def test_every_key_of_fill_this_request_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._confirm_request_id, request_site(), 11)
    _result, _terminal, described = ask(monkeypatch, site, ["y"])
    assert keys(described.options[0]) == [("y", "Fill this request"), ("n", "Don't fill it")]
    for key, outcome in {"y": True, "n": False}.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


def test_fill_this_request_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._confirm_request_id, request_site(), 11)
    result, terminal, _described = ask(monkeypatch, site, [], yyy=True)
    assert result is True
    assert terminal.calls == []


# #35 uploader.prompt_downconversion_choice: formats to convert

CONVERSIONS_TEXT = '\nSelect formats to convert (space-separated list of IDs, "0" for none, "*" for all)'


@pytest.mark.parametrize(
    ("held", "wanted", "default"),
    [(frozenset(), None, "*"), (frozenset({"MP3 320"}), None, "1 3"), (frozenset(), (), "0")],
)
def test_formats_to_convert_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, held: frozenset[str], wanted: tuple[str, ...] | None, default: str
) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", list(CONVERSION_KINDS))
    result, terminal, _described = ask(monkeypatch, conversions(held, wanted), ["0"])
    assert result == []
    assert terminal.calls == [prompt_call(CONVERSIONS_TEXT, default)]


def test_every_key_of_formats_to_convert_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", list(CONVERSION_KINDS))
    held = frozenset({"MP3 320"})
    _result, _terminal, described = ask(monkeypatch, conversions(held), ["0"])
    options = described.options[0]
    assert keys(options) == [
        ("1", "16bit 48.0 kHz"),
        ("2", "MP3 320"),
        ("3", "MP3 V0"),
        ("*", "All formats"),
        ("0", "None"),
    ]
    assert options is not None and options.several
    assert [o.card for o in options.items] == [
        None,
        interaction.Card(note="DUPE RISK: this edition already has it"),
        None,
        None,
        None,
    ]
    assert [o.alone for o in options.items] == [False, False, False, True, True]
    for key, outcome in {"*": CONVERSIONS, "0": []}.items():
        result, terminal, _described = ask(monkeypatch, conversions(held), [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
    for key, outcome in {
        "1": ["16bit 48.0 kHz"],
        "2": ["MP3 320"],
        "3": ["MP3 V0"],
        "1 3": ["16bit 48.0 kHz", "MP3 V0"],
    }.items():
        # A pick by number is confirmed by a plain yes or no, as on master.
        result, terminal, described = ask(monkeypatch, conversions(held), [key, True])
        assert (key, result) == (key, outcome)
        assert [call[0] for call in terminal.calls] == ["prompt", "confirm"]
        assert terminal.calls[1][1] == click.style("Confirm selection?", fg="magenta")
        assert described.labels == [None]


def test_formats_to_convert_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", ["16bit", "V0"])
    result, terminal, _described = ask(monkeypatch, conversions(), [], yyy=True)
    assert result == ["16bit 48.0 kHz", "MP3 V0"]
    assert terminal.calls == []


# #36 (and #1) trackers.choose_tracker: next tracker


def test_tracker_choice_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    result, terminal, _described = ask(monkeypatch, partial(trackers.choose_tracker, ["RED", "OPS"]), ["OPS"])
    assert result == "OPS"
    assert terminal.calls == [prompt_call("Your choices are RED , OPS or [n]one.", "RED")]


def test_every_key_of_tracker_choice_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(trackers.choose_tracker, ["OPS", "DIC"])
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    assert keys(described.options[0]) == [("OPS", "OPS"), ("DIC", "DIC"), ("n", "None")]
    for key, outcome in {"OPS": "OPS", "DIC": "DIC", "n": None}.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
