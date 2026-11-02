"""Which conversions an upload makes: the options a source allows, the held ones, and the choice."""

from typing import Any

import asyncclick as click

from salmon import cfg, interaction
from salmon.config.validations import CONVERSION_KINDS, canonical_conversions
from salmon.uploader.dupe_checker import (
    held_formats,
)


def validate_conversions(ctx, param, value: str | None) -> tuple[str, ...] | None:
    """Parse --conversions: comma-separated kinds in any case, or none, or all. None when not given.

    Raises:
        click.BadParameter: A value is not a conversion kind; the message lists the choices.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if text == "all":
        return CONVERSION_KINDS
    if text == "none":
        return ()
    try:
        return canonical_conversions(entry.strip() for entry in value.split(","))
    except ValueError as e:
        raise click.BadParameter(f"{e}, or use none or all.") from None


def get_downconversion_options(rls_data, track_data, quiet: bool = False):
    """
    Determine available downconversion options based on current format.
    Returns a list of downconversion tasks

    Tier hierarchy:
    1. 24bit 176.4 ~ 192 kHz
    2. 24bit 44.1 ~ 96 kHz
    3. 16bit 44.1 ~ 48 kHz
    4. mp3 320
    5. mp3 v0

    A release whose files differ in sample rate gets only the MP3 transcodes: a lossless
    downconversion has one target rate, and would upsample the files below it.
    `quiet` leaves out the message saying so, for a caller that only asks whether there are options.
    """
    if not track_data:
        return []

    rates = sorted({track["sample rate"] for track in track_data.values()})
    sample_rate = rates[0]
    encoding = rls_data["encoding"]
    mixed = len(rates) > 1
    if mixed and not quiet and encoding == "24bit Lossless":
        listed = ", ".join(f"{rate / 1000:g}" for rate in rates)
        click.secho(f"No lossless downconversion: the files have different sample rates ({listed} kHz).", fg="yellow")

    options = []

    # Tier 1: 24bit 176.4~192 kHz
    if encoding == "24bit Lossless" and not mixed and sample_rate >= 176400:
        # Can downconvert to 24bit lower sample rate
        target_rate = 96000 if sample_rate % 48000 == 0 else 88200
        options.append(
            {
                "name": f"24bit {target_rate / 1000:.1f} kHz",
                "action": "downconvert",
                "target_bitdepth": 24,
                "target_sample_rate": target_rate,
            }
        )

    # Tier 2: 24bit 44.1~96 kHz
    if encoding == "24bit Lossless" and not mixed and sample_rate >= 44100:
        # Can downconvert to 16bit
        target_rate = 48000 if sample_rate % 48000 == 0 else 44100
        options.append(
            {
                "name": f"16bit {target_rate / 1000:.1f} kHz",
                "action": "downconvert",
                "target_bitdepth": 16,
                "target_sample_rate": target_rate,
            }
        )

    # Tier 3: 16bit 44.1~48 kHz
    if (encoding == "Lossless") or (encoding == "24bit Lossless"):
        # Can transcode to MP3
        options.extend(
            [
                {"name": "MP3 320", "action": "transcode", "encoding": "320"},
                {"name": "MP3 V0", "action": "transcode", "encoding": "V0"},
            ]
        )

    return options


def downconversion_format(task: dict[str, Any]) -> tuple[str, str]:
    """Give the format and encoding of the torrent a downconversion task makes."""
    if task["action"] == "transcode":
        return "MP3", {"320": "320", "V0": "V0 (VBR)"}[task["encoding"]]
    return "FLAC", "Lossless" if task["target_bitdepth"] == 16 else "24bit Lossless"


def _held_conversions(
    group: dict[str, Any],
    release: dict[str, Any],
    source_flac: dict[str, Any],
    rls_data: dict[str, Any],
    track_data: dict[str, Any],
    *,
    uploaded: bool = False,
) -> set[str]:
    """The names of the conversions offered for this release that its edition in the group already holds.

    Args:
        group: The group, as the tracker's torrentgroup API returns it.
        release: The reviewed release metadata.
        source_flac: The FLAC the conversions are made from (the torrent just uploaded, or the one
            --skip-flac-upload transcodes), which never counts as held.
        rls_data: The release data the conversions are offered for.
        track_data: The tracks' audio info.
        uploaded: Whether this run sent the source FLAC (see held_formats).
    """
    formats = {
        option["name"]: downconversion_format(option)
        for option in get_downconversion_options(rls_data, track_data, quiet=True)
    }
    return held_formats(group, release, source_flac, formats, uploaded=uploaded)


def conversion_kind(task: dict[str, Any]) -> str:
    """Name a downconversion task by its kind, whatever the release's rates: 24bit, 16bit, 320 or V0."""
    if task["action"] == "transcode":
        return task["encoding"]
    return f"{task['target_bitdepth']}bit"


def wanted_conversions(
    options: list[dict[str, Any]],
    held: set[str] | frozenset[str] = frozenset(),
    conversions: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """The options selected by default: those of a kind in `conversions` (None: upload.conversions) not in `held`."""
    kinds = cfg.upload.conversions if conversions is None else conversions
    return [option for option in options if conversion_kind(option) in kinds and option["name"] not in held]


def _applicable_conversions(options: list[dict[str, Any]], conversions: tuple[str, ...] | None) -> bool:
    """Whether any offered option is of a kind in `conversions` (None: upload.conversions), held or not."""
    kinds = cfg.upload.conversions if conversions is None else conversions
    return any(conversion_kind(option) in kinds for option in options)


def _warn_held_conversions(held: set[str] | frozenset[str]) -> None:
    for name in sorted(held):
        click.secho(
            f"\nDUPE RISK: this edition already has {name}; the site removes exact duplicates.", fg="red", bold=True
        )


async def choose_conversions(
    rls_data,
    track_data,
    options: list[dict[str, Any]],
    held: set[str] | frozenset[str] = frozenset(),
    conversions: tuple[str, ...] | None = None,
    *,
    ask_first: bool = True,
) -> list[dict[str, Any]]:
    """Choose the conversions to make after an upload to one tracker, asking only what the run leaves open.

    A selection for the run (`conversions`, from --conversions) is final: what it names, minus `held`, is made
    without a question. A selection that names nothing offered (an empty one, or kinds this release cannot give,
    or the same from upload.conversions) makes none, also without a question. Only a run that relies on an
    upload.conversions that names something offered is asked: whether to check the options (unless `ask_first`
    is false: transcodes only), then which. -yyy takes the default choice.

    Args:
        rls_data: The release data the conversions are offered for.
        track_data: The tracks' audio info.
        options: The conversions offered for this upload.
        held: The names of the conversions the edition already holds.
        conversions: The kinds selected for this run (--conversions), or None to follow upload.conversions.
        ask_first: Whether to ask "check downconversion options?" before the choice.
    """
    if not _applicable_conversions(options, conversions):
        click.secho("\nNo conversions selected for this upload.", fg="yellow")
        return []
    if conversions is not None:
        _warn_held_conversions(held)
        return wanted_conversions(options, held, conversions)
    if (
        ask_first
        and not await interaction.assume_defaults()
        and not await interaction.confirm(
            click.style("\nWould you like to check downconversion options?", fg="magenta"),
            default=bool(wanted_conversions(options, held, conversions)),
        )
    ):
        return []
    return await prompt_downconversion_choice(rls_data, track_data, held, conversions)


async def prompt_downconversion_choice(
    rls_data,
    track_data,
    held: set[str] | frozenset[str] = frozenset(),
    conversions: tuple[str, ...] | None = None,
):
    """
    Prompt user to select downconversion formats.
    Returns a list of selected task dictionaries.
    Options named in `held` are already in the edition: they are flagged as a dupe risk and left out
    of the default choice and of -yyy, but can still be picked by number.
    `conversions` (None: upload.conversions) names the kinds selected by default and made with -yyy:
    every option is still listed, and any can be picked by number.
    """
    options = get_downconversion_options(rls_data, track_data)

    if not options:
        return []

    _warn_held_conversions(held)
    wanted = wanted_conversions(options, held, conversions)
    if await interaction.assume_defaults():
        return wanted

    click.secho("\nDownconversion Options", fg="cyan", bold=True)

    # Get current format info for display
    encoding = rls_data["encoding"]
    if track_data:
        sample_rate = next(iter(track_data.values()))["sample rate"]
        current_format = f"{encoding}"
        if encoding == "24bit Lossless" or encoding == "Lossless":
            current_format += f" ({sample_rate / 1000:.1f} kHz)"
    else:
        current_format = encoding

    click.secho(f"Current format: {current_format}", fg="yellow")
    kinds = cfg.upload.conversions if conversions is None else conversions
    if left_out := [option["name"] for option in options if conversion_kind(option) not in kinds]:
        click.secho(f"Not selected by default (upload.conversions / --conversions): {', '.join(left_out)}", fg="yellow")
    click.secho("Available downconversion formats:", fg="green")

    for i, option in enumerate(options, 1):
        click.secho(f"  {i}. {option['name']}", fg="white")

    click.secho("  0. Skip downconversion", fg="white")
    click.secho("  *. All formats", fg="white")
    if len(wanted) == len(options):
        default = "*"
    else:
        default = " ".join(str(i) for i, option in enumerate(options, 1) if option in wanted) or "0"

    selected_tasks = []

    while True:
        try:
            choices = await interaction.prompt(
                click.style(
                    '\nSelect formats to convert (space-separated list of IDs, "0" for none, "*" for all)', fg="magenta"
                ),
                default=default,
                options=interaction.Options(
                    (
                        *(
                            interaction.Option(
                                str(i),
                                option["name"],
                                card=(
                                    interaction.Card(note="DUPE RISK: this edition already has it")
                                    if option["name"] in held
                                    else None
                                ),
                            )
                            for i, option in enumerate(options, 1)
                        ),
                        interaction.Option("*", "All formats", alone=True),
                        interaction.Option("0", "None", alone=True),
                    ),
                    several=True,
                ),
            )

            if choices.strip() == "0":
                break

            if choices.strip() == "*":
                selected_tasks = options
                break

            # Parse choices - now using space separation
            choice_nums = [int(x.strip()) for x in choices.split() if x.strip().isdigit()]

            # Validate choices
            invalid_choices = [x for x in choice_nums if x < 1 or x > len(options)]
            if invalid_choices:
                click.secho(
                    f"Invalid choices: {invalid_choices}. Please enter numbers between 1-{len(options)}.", fg="red"
                )
                continue

            # Get selected tasks
            selected_tasks = [options[i - 1] for i in choice_nums]

            # Confirm selection
            if selected_tasks:
                display_names = [task["name"] for task in selected_tasks]
                click.secho(f"\nSelected formats: {', '.join(display_names)}", fg="green")
                if await interaction.confirm(click.style("Confirm selection?", fg="magenta"), default=True):
                    break
            else:
                break

        except (ValueError, IndexError):
            click.secho("Invalid input format, please enter numeric options", fg="red")
            continue

    return selected_tasks
