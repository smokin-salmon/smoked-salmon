"""Remember what each converted folder was made from, so a later upload can describe the conversion.

The record is only a hint for the description: it never changes the format, bitrate or media of an upload.
"""

import contextlib
import json
import os
import tempfile
from typing import Any

import asyncclick as click

from salmon import cfg, dryrun

# One hidden directory per parent with one file per converted folder: never inside the album, never shared by writers.
REGISTRY_DIR = ".salmon-conversions"
KINDS = {"downconvert", "transcode"}


def _sidecar(folder: str) -> str:
    folder = os.path.abspath(folder)
    return os.path.join(os.path.dirname(folder), REGISTRY_DIR, os.path.basename(folder) + ".json")


def _inside_scratch(path: str) -> bool:
    """Whether path is inside the scratch directory of the dry run upload that is running."""
    try:
        scratch = os.path.realpath(dryrun.scratch_dir())
    except RuntimeError:
        return False
    return os.path.commonpath([scratch, os.path.realpath(path)]) == scratch


def record_conversion(output: str, **facts: Any) -> None:
    """Note how `output` was produced: its source folder plus the converter's settings.

    Nothing is written into a library_dirs entry, nor, in a dry run, outside the run's scratch directory.
    """
    sidecar = _sidecar(output)
    record_dir = os.path.dirname(sidecar)
    # Both: the record directory (a symlink may lead it into a library) and its parent, which may hold one.
    if cfg.directory.protects(record_dir) or cfg.directory.protects(os.path.dirname(record_dir)):
        click.secho(f"Not recording how {os.path.basename(output)} was made: library_dirs is there.", fg="yellow")
        return
    if dryrun.active() and not _inside_scratch(record_dir):
        return
    # The record names its folder, so one that ends up under another folder's name is recognised as foreign.
    facts = {**facts, "output": os.path.basename(os.path.abspath(output))}
    os.makedirs(record_dir, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=record_dir, prefix=os.path.basename(sidecar), suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=2, sort_keys=True)
    os.replace(temp, sidecar)


def conversion_of(folder: str) -> dict[str, Any] | None:
    """The recorded facts for a folder a converter produced; None when there are none or they are unusable."""
    try:
        with open(_sidecar(folder), encoding="utf-8") as fh:
            data = json.load(fh)
    # Only these two mean "not converted"; any other OSError must reach the caller, since a folder
    # whose record cannot be read still owes the site a description.
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        click.secho(f"Ignoring an unreadable conversion record for {os.path.basename(folder)}.", fg="yellow")
        return None
    if not _usable(data, os.path.basename(os.path.abspath(folder))):
        click.secho(f"Ignoring a conversion record that does not fit {os.path.basename(folder)}.", fg="yellow")
        return None
    return data


def carry_conversion(old: str, new: str) -> None:
    """Hand a renamed or copied folder's record to its new name; the old record goes once the old folder has."""
    facts = conversion_of(old)
    if facts is None or os.path.abspath(old) == os.path.abspath(new):
        return
    record_conversion(new, **{key: value for key, value in facts.items() if key != "output"})
    if not os.path.isdir(old):
        with contextlib.suppress(OSError):
            os.remove(_sidecar(old))


def _usable(data: Any, name: str) -> bool:
    from salmon.converter.downconverting import SOX_DEPTH_ARGS  # local: both converters import this module
    from salmon.converter.transcoding import LAME_COMMAND_MAP

    if not isinstance(data, dict) or data.get("kind") not in KINDS or not isinstance(data.get("source"), str):
        return False
    if data.get("output") != name:
        return False
    if data["kind"] == "transcode":
        return data.get("bitrate") in LAME_COMMAND_MAP
    return data.get("bit_depth") in SOX_DEPTH_ARGS and _usable_rates(data.get("sample_rate"))


def _usable_rates(rates: Any) -> bool:
    if rates is None or _positive_int(rates):
        return True
    return isinstance(rates, list) and bool(rates) and all(map(_positive_int, rates))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
