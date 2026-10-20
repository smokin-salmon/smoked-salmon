"""Which conversions salmon up selects by default after a FLAC: upload.conversions and --conversions (#690).

The setting changes only the default answer: every possible conversion is still listed and can be picked by number.
The end-to-end runs go against the local fake tracker of test_uploader_dry_run, never a real one.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import msgspec
import pytest
from asyncclick.testing import CliRunner
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    FIRST_TORRENT_ID,
    FakeTracker,
    _album,
    _returning,
    _returning_async,
    _run_up,
    _write_flac,
    image_uploads,  # noqa: F401 (a fixture)
)

import salmon.trackers
import salmon.uploader as uploader
from salmon import cfg, interaction
from salmon.config.validations import CONVERSION_KINDS, Upload

TRACKS_24BIT_48K = {"01.flac": {"sample rate": 48000}}
RELEASE_24BIT = {"format": "FLAC", "encoding": "24bit Lossless"}


# The cover goes to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")


@pytest.fixture
def torrents(monkeypatch, tmp_path) -> Path:
    """A download_directory and a dot_torrents_dir, configured. Gives the dot_torrents_dir."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return torrents


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload, "conversions", list(CONVERSION_KINDS))


# What names a conversion


def test_a_task_is_named_by_its_kind_whatever_the_rates() -> None:
    tracks = {"01.flac": {"sample rate": 192000}}
    options = uploader.get_downconversion_options(RELEASE_24BIT, tracks)

    assert [option["name"] for option in options] == ["24bit 96.0 kHz", "16bit 48.0 kHz", "MP3 320", "MP3 V0"]
    assert [uploader.conversion_kind(option) for option in options] == ["24bit", "16bit", "320", "V0"]


# The default answer


def _offered() -> list[dict[str, Any]]:
    return uploader.get_downconversion_options(RELEASE_24BIT, TRACKS_24BIT_48K, quiet=True)


def _prompt_yyy(held: frozenset[str] = frozenset(), conversions: tuple[str, ...] | None = None) -> list[str]:
    async def run() -> list[str]:
        with interaction.assuming_defaults():
            tasks = await uploader.prompt_downconversion_choice(RELEASE_24BIT, TRACKS_24BIT_48K, held, conversions)
        return [task["name"] for task in tasks]

    return anyio.run(run)


def test_the_offer_for_a_24bit_48khz_release(monkeypatch) -> None:
    assert [option["name"] for option in _offered()] == ["16bit 48.0 kHz", "MP3 320", "MP3 V0"]


def test_yyy_makes_every_conversion_by_default() -> None:
    assert _prompt_yyy() == ["16bit 48.0 kHz", "MP3 320", "MP3 V0"]


def test_yyy_makes_only_the_conversions_the_config_names(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", ["16bit"])

    assert _prompt_yyy() == ["16bit 48.0 kHz"]


def test_yyy_makes_none_when_the_config_is_empty(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", [])

    assert _prompt_yyy() == []
    assert "Selected formats" not in capsys.readouterr().out


def test_the_option_overrides_the_config(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", ["16bit"])

    assert _prompt_yyy(conversions=("V0",)) == ["MP3 V0"]
    assert _prompt_yyy(conversions=()) == []
    assert _prompt_yyy(conversions=CONVERSION_KINDS) == ["16bit 48.0 kHz", "MP3 320", "MP3 V0"]


def test_a_held_format_is_left_out_of_the_conversions_the_config_names(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", ["16bit", "V0"])

    assert _prompt_yyy(held=frozenset({"16bit 48.0 kHz"})) == ["MP3 V0"]


def _interactive_choice(monkeypatch, answer: str | None = None, **kwargs: Any) -> tuple[list[str], dict[str, Any]]:
    """Run the prompt interactively. Returns the names selected, and the default offered with the question."""
    asked: dict[str, Any] = {}

    async def fake_prompt(text: str, *_args, default: str = "", **_kwargs) -> str:
        asked["default"] = default
        return default if answer is None else answer

    async def run() -> list[str]:
        tasks = await uploader.prompt_downconversion_choice(RELEASE_24BIT, TRACKS_24BIT_48K, **kwargs)
        return [task["name"] for task in tasks]

    monkeypatch.setattr(click, "prompt", fake_prompt)
    monkeypatch.setattr(click, "confirm", _returning(True))
    return anyio.run(run), asked


@pytest.mark.parametrize(
    ("conversions", "default", "names"),
    [
        (("24bit", "16bit", "320", "V0"), "*", ["16bit 48.0 kHz", "MP3 320", "MP3 V0"]),
        (("16bit",), "1", ["16bit 48.0 kHz"]),
        (("24bit", "320"), "2", ["MP3 320"]),
        (("16bit", "V0"), "1 3", ["16bit 48.0 kHz", "MP3 V0"]),
        ((), "0", []),
    ],
)
def test_the_default_answer_is_the_conversions_wanted(
    monkeypatch, conversions: tuple[str, ...], default: str, names: list[str]
) -> None:
    selected, asked = _interactive_choice(monkeypatch, conversions=conversions)

    assert asked["default"] == default
    assert selected == names


def test_the_default_answer_follows_the_config(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", ["16bit"])

    selected, asked = _interactive_choice(monkeypatch)

    assert asked["default"] == "1"
    assert selected == ["16bit 48.0 kHz"]


def test_a_held_format_leaves_the_default_even_when_the_config_names_it(monkeypatch) -> None:
    selected, asked = _interactive_choice(monkeypatch, held=frozenset({"MP3 320"}))

    assert asked["default"] == "1 3"
    assert selected == ["16bit 48.0 kHz", "MP3 V0"]


def test_every_option_is_listed_and_the_excluded_are_named(monkeypatch, capsys) -> None:
    _selected, _asked = _interactive_choice(monkeypatch, conversions=("16bit",))

    out = capsys.readouterr().out
    assert "Not selected by default (upload.conversions / --conversions): MP3 320, MP3 V0" in out
    assert out.index("Not selected by default") < out.index("1. 16bit 48.0 kHz")
    assert "2. MP3 320" in out
    assert "3. MP3 V0" in out


def test_nothing_is_said_when_nothing_listed_is_excluded(monkeypatch, capsys) -> None:
    # 24bit is not offered for this release: naming only the kinds that are offered excludes nothing.
    _interactive_choice(monkeypatch, conversions=("16bit", "320", "V0"))

    assert "Not selected by default" not in capsys.readouterr().out


def test_an_excluded_format_can_still_be_picked_by_number(monkeypatch) -> None:
    selected, asked = _interactive_choice(monkeypatch, answer="2", conversions=("16bit",))

    assert asked["default"] == "1"
    assert selected == ["MP3 320"]


# The command


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("16bit,v0", ("16bit", "V0")),
        (" 24BIT , 320 ", ("24bit", "320")),
        ("V0", ("V0",)),
        ("none", ()),
        ("None", ()),
        ("all", CONVERSION_KINDS),
        ("ALL", CONVERSION_KINDS),
    ],
)
def test_the_option_is_parsed(value: str | None, expected: tuple[str, ...] | None) -> None:
    assert uploader.validate_conversions(None, None, value) == expected


@pytest.mark.parametrize("value", ["mp3", "16bit,mp3", "16bit,16bit", "16bit,", ""])
def test_the_option_refuses_what_is_not_a_conversion(value: str) -> None:
    with pytest.raises(click.UsageError):
        uploader.validate_conversions(None, None, value)


async def _invoke(monkeypatch, tmp_path: Path, *args: str):
    seen: list[Any] = []

    async def fake_run_up(options) -> None:
        seen.append(options)

    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    monkeypatch.setattr(uploader, "run_up", fake_run_up)
    result = await CliRunner().invoke(uploader.up, [str(tmp_path), "-t", "RED", "-s", "WEB", *args])
    return result, seen


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], None),
        (["--conversions", "16bit,v0"], ("16bit", "V0")),
        (["--conversions", "none"], ()),
        (["--conversions", "all"], CONVERSION_KINDS),
    ],
)
def test_the_option_reaches_the_upload(monkeypatch, tmp_path, args: list[str], expected) -> None:
    result, seen = anyio.run(lambda: _invoke(monkeypatch, tmp_path, *args))

    assert result.exit_code == 0, result.output
    assert [options.conversions for options in seen] == [expected]


def test_the_option_refuses_an_unknown_conversion_naming_the_choices(monkeypatch, tmp_path) -> None:
    result, seen = anyio.run(lambda: _invoke(monkeypatch, tmp_path, "--conversions", "mp3"))

    assert result.exit_code == 2
    assert '"24bit", "16bit", "320", "V0"' in result.output
    assert seen == []


# The config


def test_the_default_is_every_conversion() -> None:
    assert Upload().conversions == ["24bit", "16bit", "320", "V0"]


def test_the_config_takes_any_case_and_keeps_the_canonical_one() -> None:
    assert Upload(conversions=["v0", "16BIT"]).conversions == ["V0", "16bit"]
    assert msgspec.convert({"conversions": ["24Bit"]}, Upload).conversions == ["24bit"]


def test_the_config_takes_an_empty_list() -> None:
    assert Upload(conversions=[]).conversions == []


def test_the_config_refuses_an_unknown_conversion_naming_the_choices() -> None:
    with pytest.raises(ValueError, match=r"upload\.conversions: 'mp3' is not a conversion.*\"24bit\", \"16bit\""):
        Upload(conversions=["16bit", "mp3"])


def test_the_config_refuses_a_conversion_named_twice() -> None:
    with pytest.raises(ValueError, match=r"upload\.conversions: '16bit' is named twice"):
        Upload(conversions=["16bit", "16BIT"])


# End to end, against the fake tracker


def _run_conversion_up(
    monkeypatch,
    torrents: Path,
    args: tuple[str, ...],
    *,
    confirm=None,
    prompt=None,
    group_torrents: tuple[dict[str, Any], ...] = (),
    **kwargs: Any,
):
    """Run `salmon up -yyy` on a 24-bit 48 kHz FLAC against the fake tracker; its conversions are made-up files.

    The group the FLAC goes into holds `group_torrents`: none by default, so no conversion is held back.
    """
    album = _album(torrents.parent / "seeding" / "Album")
    names = ["01. one.flac", "02. two.flac"]

    def track(number: int, precision: int, rate: int) -> dict[str, Any]:
        tags = SimpleNamespace(tracknumber=str(number), discnumber=None, artist=["Artist"], title=str(number))
        return {"sample rate": rate, "precision": precision, "duration": 60, "bit rate": 1_000_000, "t": tags}

    rls_data = {**RELEASE_24BIT, "artists": [("Artist", "main")], "title": "Album", "catno": "CAT1"}
    source_tracks = {name: track(i, 24, 48000) for i, name in enumerate(names, 1)}

    async def convert_folder(path, bit_depth, sample_rate, output_dir):
        folder = Path(output_dir) / f"converted {bit_depth}"
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            _write_flac(folder / name)
        return sample_rate, str(folder)

    def gather_audio_info(path, *_args, **_kwargs):
        if Path(path).name.startswith("converted"):
            bits = int(Path(path).name.split()[-1])
            return {name: track(i, bits, 48000) for i, name in enumerate(names, 1)}
        return {}

    if confirm is not None:
        monkeypatch.setattr(click, "confirm", confirm)
    if prompt is not None:
        monkeypatch.setattr(click, "prompt", prompt)
    return _run_up(
        monkeypatch,
        album,
        torrents,
        args,
        tracker=FakeTracker(group_torrents=list(group_torrents)),
        construct_rls_data=_returning_async(rls_data),
        concat_track_data=_returning(source_tracks),
        check_spectrals=_returning_async((False, None)),
        convert_folder=convert_folder,
        gather_audio_info=gather_audio_info,
        **{"multi_tracker_upload": False, **kwargs},
    )


def _upload_posts(run) -> list[Any]:
    return [sent for sent in run.tracker.sent if sent.method == "POST" and sent.query.get("action") == "upload"]


def _formats(run) -> list[str]:
    """The format and encoding of each torrent the run uploaded, in order."""
    return [f"{dict(post.fields)['format']} {dict(post.fields)['bitrate']}" for post in _upload_posts(run)]


def test_yyy_with_one_conversion_sends_the_flac_and_that_conversion(monkeypatch, torrents: Path) -> None:
    run = _run_conversion_up(monkeypatch, torrents, ("--conversions", "16bit"))

    assert run.result.exit_code == 0, run.result.output
    # The FLAC, then its 16-bit downconversion (the made-up 24-bit FLAC reads as Lossless in the stubbed metadata).
    assert len(_upload_posts(run)) == 2
    assert "Processing: 16bit 48.0 kHz" in run.result.output
    assert "Processing: MP3" not in run.result.output


def test_yyy_with_the_default_sends_the_flac_and_every_conversion(monkeypatch, torrents: Path) -> None:
    run = _run_conversion_up(monkeypatch, torrents, ())

    assert run.result.exit_code == 0, run.result.output
    assert len(_upload_posts(run)) == 4
    assert _formats(run)[2:] == ["MP3 320", "MP3 V0 (VBR)"]


def test_yyy_with_a_lossy_conversion_only_converts_no_flac(monkeypatch, torrents: Path) -> None:
    run = _run_conversion_up(monkeypatch, torrents, ("--conversions", "v0"))

    assert run.result.exit_code == 0, run.result.output
    assert _formats(run)[1:] == ["MP3 V0 (VBR)"]
    assert "Processing: 16bit" not in run.result.output


def test_yyy_with_none_sends_only_the_flac(monkeypatch, torrents: Path) -> None:
    run = _run_conversion_up(monkeypatch, torrents, ("--conversions", "none"))

    assert run.result.exit_code == 0, run.result.output
    assert len(_upload_posts(run)) == 1
    assert "Processing:" not in run.result.output


# The group the FLAC went into already holds the edition's 16-bit FLAC (#717)

# The stubbed review's edition: WEB, 2020, Label.
EDITION_16BIT_FLAC = {
    "id": 11,
    "media": "WEB",
    "format": "FLAC",
    "encoding": "Lossless",
    "remastered": True,
    "remasterYear": 2020,
    "remasterTitle": "",
    "remasterRecordLabel": "Label",
    "remasterCatalogueNumber": "CAT1",
}
HELD_16BIT = "DUPE RISK: this edition already has 16bit 48.0 kHz; the site removes exact duplicates."


def test_yyy_leaves_out_the_16bit_flac_the_edition_holds(monkeypatch, torrents: Path) -> None:
    run = _run_conversion_up(monkeypatch, torrents, (), group_torrents=(EDITION_16BIT_FLAC,))

    assert run.result.exit_code == 0, run.result.output
    assert HELD_16BIT in run.result.output
    assert "Processing: 16bit" not in run.result.output
    # The 24-bit FLAC, then the MP3s.
    assert len(_upload_posts(run)) == 3
    assert _formats(run)[1:] == ["MP3 320", "MP3 V0 (VBR)"]


def test_yyy_converts_a_16bit_flac_held_only_under_another_catalogue_number(monkeypatch, torrents: Path) -> None:
    # The site files the uploaded FLAC (the fake's first torrent id) under the form's edition, CAT1; the held
    # 16-bit FLAC sits in CAT2, another pressing of the same year.
    uploaded = {**EDITION_16BIT_FLAC, "id": FIRST_TORRENT_ID, "encoding": "24bit Lossless"}
    other = {**EDITION_16BIT_FLAC, "remasterCatalogueNumber": "CAT2"}
    run = _run_conversion_up(monkeypatch, torrents, (), group_torrents=(uploaded, other))

    assert run.result.exit_code == 0, run.result.output
    assert "DUPE RISK" not in run.result.output
    assert "Processing: 16bit" in run.result.output
    assert _formats(run)[1:] == ["FLAC Lossless", "MP3 320", "MP3 V0 (VBR)"]


def test_yyy_leaves_out_a_16bit_flac_held_in_the_edition_the_flac_was_filed_under(monkeypatch, torrents: Path) -> None:
    uploaded = {**EDITION_16BIT_FLAC, "id": FIRST_TORRENT_ID, "encoding": "24bit Lossless"}
    same = {**EDITION_16BIT_FLAC, "remasterCatalogueNumber": "CAT-1"}
    run = _run_conversion_up(monkeypatch, torrents, (), group_torrents=(uploaded, same))

    assert run.result.exit_code == 0, run.result.output
    assert HELD_16BIT in run.result.output
    assert _formats(run)[1:] == ["MP3 320", "MP3 V0 (VBR)"]


def test_the_prompt_leaves_out_the_16bit_flac_the_edition_holds(monkeypatch, torrents: Path) -> None:
    asked: list[Any] = []

    def confirm(text: str, default: bool | None = False, **_kwargs: Any) -> bool:
        if "check downconversion options" in text:
            asked.append(default)
        return True if default is None else default

    async def prompt(text: str, default: Any = None, **_kwargs: Any) -> Any:
        if "Select formats to convert" in text:
            asked.append(default)
        return default

    run = _run_conversion_up(
        monkeypatch,
        torrents,
        (),
        yes_all=False,
        confirm=confirm,
        prompt=prompt,
        group_torrents=(EDITION_16BIT_FLAC,),
    )

    assert run.result.exit_code == 0, run.result.output
    # Asked to check, then 1. 16bit 48.0 kHz, 2. MP3 320, 3. MP3 V0: the MP3s are selected.
    assert asked == [True, "2 3"]
    assert HELD_16BIT in run.result.output
    assert _formats(run)[1:] == ["MP3 320", "MP3 V0 (VBR)"]


# The questions: a selection made for the run is final, so it is not asked about (#753)

NO_CONVERSIONS = "No conversions selected for this upload."


def _questions(monkeypatch, torrents: Path, *args: str, config: list[str] | None = None, **kwargs: Any):
    """Run the upload without -yyy, answering each question with its default. Gives the run and the questions asked.

    The questions are the default of "check downconversion options?", then of "Select formats to convert".
    """
    asked: list[Any] = []
    if config is not None:
        monkeypatch.setattr(cfg.upload, "conversions", config)

    def confirm(text: str, default: bool | None = False, **_kwargs: Any) -> bool:
        if "check downconversion options" in text:
            asked.append(default)
        return True if default is None else default

    async def prompt(text: str, default: Any = None, **_kwargs: Any) -> Any:
        if "Select formats to convert" in text:
            asked.append(default)
        return default

    kwargs.setdefault("yes_all", False)
    run = _run_conversion_up(monkeypatch, torrents, args, confirm=confirm, prompt=prompt, **kwargs)
    assert run.result.exit_code == 0, run.result.output
    return run, asked


@pytest.mark.parametrize("config", [[], ["24bit"]])
def test_a_config_naming_nothing_offered_asks_nothing_and_makes_nothing(
    monkeypatch, torrents: Path, config: list[str]
) -> None:
    run, asked = _questions(monkeypatch, torrents, config=config)

    assert asked == []
    assert len(_upload_posts(run)) == 1
    assert NO_CONVERSIONS in run.result.output


def test_a_run_selection_makes_those_conversions_without_asking(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, "--conversions", "320,V0")

    assert asked == []
    assert _formats(run)[1:] == ["MP3 320", "MP3 V0 (VBR)"]
    assert NO_CONVERSIONS not in run.result.output


def test_a_run_selection_of_none_asks_nothing_and_makes_nothing(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, "--conversions", "none")

    assert asked == []
    assert len(_upload_posts(run)) == 1
    assert NO_CONVERSIONS in run.result.output


def test_a_run_selection_of_kinds_the_release_cannot_give_makes_nothing(monkeypatch, torrents: Path) -> None:
    # A 24-bit 48 kHz release is offered no 24-bit downconversion.
    run, asked = _questions(monkeypatch, torrents, "--conversions", "24bit")

    assert asked == []
    assert len(_upload_posts(run)) == 1
    assert NO_CONVERSIONS in run.result.output


def test_a_run_selection_beats_a_config_that_would_ask(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, "--conversions", "16bit", config=list(CONVERSION_KINDS))

    assert asked == []
    assert len(_upload_posts(run)) == 2
    assert "Processing: 16bit 48.0 kHz" in run.result.output


def test_a_config_only_run_asks_both_questions(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, config=["V0"])

    # Asked to check (V0 is wanted), then 1. 16bit 48.0 kHz, 2. MP3 320, 3. MP3 V0.
    assert asked == [True, "3"]
    assert _formats(run)[1:] == ["MP3 V0 (VBR)"]


def test_yyy_asks_nothing_and_makes_what_the_config_names(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, config=["320"], yes_all=True)

    assert asked == []
    assert _formats(run)[1:] == ["MP3 320"]


def test_yyy_with_a_config_naming_nothing_offered_makes_nothing(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, config=[], yes_all=True)

    assert asked == []
    assert len(_upload_posts(run)) == 1
    assert NO_CONVERSIONS in run.result.output


def test_a_held_format_a_run_selection_names_is_left_out_with_its_dupe_risk(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, "--conversions", "16bit,V0", group_torrents=(EDITION_16BIT_FLAC,))

    assert asked == []
    assert HELD_16BIT in run.result.output
    assert "Processing: 16bit" not in run.result.output
    assert _formats(run)[1:] == ["MP3 V0 (VBR)"]


def test_a_run_selection_naming_only_held_formats_makes_nothing_and_says_why(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, "--conversions", "16bit", group_torrents=(EDITION_16BIT_FLAC,))

    assert asked == []
    assert HELD_16BIT in run.result.output
    assert len(_upload_posts(run)) == 1
    assert NO_CONVERSIONS not in run.result.output


def test_a_run_selection_asks_no_tracker_of_the_run(monkeypatch, torrents: Path) -> None:
    run, asked = _questions(monkeypatch, torrents, "--conversions", "320,V0", trackers=("RED", "OPS"))

    assert asked == []
    # Each tracker gets the FLAC and the two MP3s.
    assert _formats(run) == ["FLAC Lossless", "MP3 320", "MP3 V0 (VBR)"] * 2
