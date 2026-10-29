"""Remember what each converted folder was made from, so a later upload can describe the conversion.

The record is only a hint for the description: it never changes the format, bitrate or media of an upload. It also
lists the audio files the converter wrote, kept in step with the renames salmon makes in the folder afterwards, so a
converter finding its own output again knows it for complete whatever the folder check made of its names.
"""

import contextlib
import json
import os
import tempfile
from collections.abc import Sequence
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
    """Note how `output` was produced: its source folder plus the converter's settings, and the files it wrote.

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
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(facts, fh, indent=2, sort_keys=True)
        os.replace(temp, sidecar)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise


def conversion_of(folder: str) -> dict[str, Any] | None:
    """The recorded facts for a folder a converter produced; None when there are none or they are unusable."""
    return _read(folder, quiet=False)


def _read(folder: str, *, quiet: bool) -> dict[str, Any] | None:
    try:
        with open(_sidecar(folder), encoding="utf-8") as fh:
            data = json.load(fh)
    # Only these two mean "not converted"; any other OSError must reach the caller, since a folder
    # whose record cannot be read still owes the site a description.
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        if not quiet:
            click.secho(f"Ignoring an unreadable conversion record for {os.path.basename(folder)}.", fg="yellow")
        return None
    if not _usable(data, folder):
        if not quiet:
            click.secho(f"Ignoring a conversion record that does not fit {os.path.basename(folder)}.", fg="yellow")
        return None
    return data


def recorded_files(record: dict[str, Any]) -> dict[str, str] | None:
    """The audio files a conversion wrote, as recorded: each one's path in the folder, by its source file's path.

    Both paths are relative to their folders. None for a record from before salmon kept them, or one whose entries
    are not relative paths.
    """
    files = record.get("files")
    if not isinstance(files, dict) or not all(_relative(path) for item in files.items() for path in item):
        return None
    return files


def missing_files(folder: str, record: dict[str, Any]) -> list[str] | None:
    """The recorded audio files that are not in folder, in order; None when the record does not list them."""
    files = recorded_files(record)
    if files is None:
        return None
    return sorted(path for path in set(files.values()) if not os.path.isfile(os.path.join(folder, path)))


def note_renamed(folder: str, renames: Sequence[tuple[str, str]]) -> None:
    """Keep the record of a converted folder in step with renames salmon made inside it, in the order made.

    Each rename is the path of a file or sub-folder of folder before and after it. Nothing happens to a folder
    without a record, or with one that does not list its files.
    """
    record = _read(folder, quiet=True) if renames else None
    files = None if record is None else recorded_files(record)
    if record is None or files is None:
        return
    renamed = dict(files)
    for old, new in renames:
        old_rel, new_rel = os.path.relpath(old, folder), os.path.relpath(new, folder)
        for source, path in renamed.items():
            if path == old_rel:
                renamed[source] = new_rel
            elif path.startswith(old_rel + os.sep):
                renamed[source] = new_rel + path[len(old_rel) :]
    if renamed != files:
        record_conversion(folder, **{**_without_output(record), "files": renamed})


def _without_output(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "output"}


def _relative(path: Any) -> bool:
    """Whether path names something inside a folder: a relative path that never goes up a level."""
    if not isinstance(path, str) or not path or os.path.isabs(path):
        return False
    return os.pardir not in path.replace("\\", "/").split("/")


def carry_conversion(old: str, new: str) -> None:
    """Hand a renamed or copied folder's record to its new name; the old record goes once the old folder has."""
    facts = conversion_of(old)
    if facts is None or os.path.abspath(old) == os.path.abspath(new):
        return
    record_conversion(new, **_without_output(facts))
    if not os.path.isdir(old):
        forget_conversion(old)


def forget_conversion(folder: str) -> None:
    """Remove the record of a folder that is gone."""
    with contextlib.suppress(OSError):
        os.remove(_sidecar(folder))


def _usable(data: Any, folder: str) -> bool:
    from salmon.converter.downconverting import SOX_DEPTH_ARGS  # local: both converters import this module
    from salmon.converter.transcoding import LAME_COMMAND_MAP

    if not isinstance(data, dict) or not isinstance(data.get("source"), str):
        return False
    output = data.get("output")
    if not isinstance(output, str) or not _names_folder(output, folder):
        return False
    # Hashable before any membership test: a list or object from a hand-edited file must read as unusable.
    kind, bitrate, bit_depth = data.get("kind"), data.get("bitrate"), data.get("bit_depth")
    if not isinstance(kind, str) or kind not in KINDS:
        return False
    if kind == "transcode":
        return isinstance(bitrate, str) and bitrate in LAME_COMMAND_MAP
    return _positive_int(bit_depth) and bit_depth in SOX_DEPTH_ARGS and _usable_rates(data.get("sample_rate"))


def _names_folder(output: str, folder: str) -> bool:
    """Whether a record's output name is the folder's: its own name, or another case of it leading to the same folder.

    A case-insensitive volume opens the record of "Album" for "album", and that is the same folder; on a
    case-sensitive one they are two folders, and one's record is not the other's.
    """
    folder = os.path.abspath(folder)
    name = os.path.basename(folder)
    if output == name:
        return True
    if output.casefold() != name.casefold():
        return False
    try:
        return os.path.samefile(os.path.join(os.path.dirname(folder), output), folder)
    except OSError:
        return False


def _usable_rates(rates: Any) -> bool:
    if rates is None or _positive_int(rates):
        return True
    return isinstance(rates, list) and bool(rates) and all(map(_positive_int, rates))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
