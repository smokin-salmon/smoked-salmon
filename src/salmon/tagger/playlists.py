"""Keeping an album's playlists (.m3u, .m3u8) and cue sheets (.cue) in step with the files salmon renames.

The trackers allow these files in a torrent, and they must name the files the torrent holds. After a batch of renames
each of them is rewritten to name the new paths, keeping its encoding, line endings and every line that names no file.
"""

import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

import asyncclick as click

from salmon.common.files import is_apple_double
from salmon.tagger.renames import Rename

PLAYLIST_EXTENSIONS = {".m3u", ".m3u8", ".cue"}
# What a playlist is warned about when its entry lost its file: the files a track is.
_AUDIO_EXTENSIONS = {
    ".aac",
    ".ac3",
    ".aif",
    ".aiff",
    ".ape",
    ".dff",
    ".dsf",
    ".dts",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wv",
}
_UTF8_BOM = b"\xef\xbb\xbf"
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
_CUE_QUOTED = re.compile(r'^(\s*FILE\s+")([^"]*)(".*)$', re.IGNORECASE | re.DOTALL)
_CUE_BARE = re.compile(r"^(\s*FILE\s+)(.+?)(\s+[A-Za-z]+\s*)$", re.IGNORECASE | re.DOTALL)
_SHOWN = 5


def _key(path: str) -> str:
    return unicodedata.normalize("NFC", os.path.normpath(path))


def _moved(path: str, renames: Sequence[Rename]) -> str:
    """``path`` after each rename in turn: a renamed file, or a file under a renamed folder, takes the new name.

    Names are matched in NFC, but the result keeps the spelling of the new names and of the untouched rest.
    """
    current = os.path.normpath(path)
    for old, new in renames:
        old_key = _key(old)
        if _key(current) == old_key:
            current = os.path.normpath(new)
        elif _key(current).startswith(old_key + os.sep):
            rest = current.split(os.sep)[len(old_key.split(os.sep)) :]
            current = os.path.join(os.path.normpath(new), *rest)
    return current


@dataclass
class _Outcome:
    lines: list[str]
    broken: list[str]
    unwritable: list[str]
    misread: list[str]
    changed: bool


def rewrite_playlists(path: str, renames: Sequence[Rename]) -> None:
    """Make every playlist and cue sheet under ``path`` name the files ``renames`` (old path, new path) moved.

    Call it once the renames are made and kept. A file that cannot be read or written is left and reported; this
    never raises for one. An entry that pointed to an audio file before the renames and now points to none, and a
    new name the file's encoding cannot hold, are reported in yellow, one line each per playlist, as are the non-ASCII
    entries naming no file in a file read as cp1252, which may be in another code page, when a renamed path, taken
    from the playlist's folder, is non-ASCII too. An entry a rename moved is held to that as well, when its new path
    names no file.
    """
    if not renames:
        return
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in PLAYLIST_EXTENSIONS and not is_apple_double(name):
                try:
                    _rewrite_file(path, os.path.join(root, name), renames)
                except (OSError, ValueError) as e:
                    click.secho(f"Could not update {os.path.join(root, name)}, left as it is: {e}", fg="yellow")


def _rewrite_file(album: str, playlist: str, renames: Sequence[Rename]) -> None:
    shown = os.path.relpath(playlist, album)
    try:
        with open(playlist, "rb") as f:
            data = f.read()
    except OSError as e:
        click.secho(f"Could not read {shown}, left as it is: {e}", fg="yellow")
        return
    decoded = _decode(data, os.path.splitext(playlist)[1].lower())
    if decoded is None:
        click.secho(f"Could not read {shown} as text, left as it is: its names may now be wrong.", fg="yellow")
        return
    bom, text, encoding = decoded
    folder = os.path.dirname(playlist)
    # The folder the playlist was in when written (it may have been moved, or renamed): the entries name files there.
    old_folder = os.path.dirname(_moved(playlist, [(new, old) for old, new in reversed(renames)]))
    outcome = _rewrite_lines(
        _LINE.findall(text), os.path.splitext(playlist)[1].lower() == ".cue", old_folder, folder, renames, encoding
    )
    if outcome.broken:
        click.secho(f"{shown}: no file now for {_names(outcome.broken)}", fg="yellow")
    if outcome.unwritable:
        click.secho(
            f"{shown}: left as they were, {encoding} cannot hold the new names: {_names(outcome.unwritable)}",
            fg="yellow",
        )
    if outcome.misread:
        click.secho(
            f"{shown}: read as {encoding}, which may not be its encoding: check {_names(outcome.misread)}", fg="yellow"
        )
    if not outcome.changed:
        return
    try:
        # Beside the playlist, then over it, so an interrupted write never leaves half a playlist.
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".salmon-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as f:
                f.write(bom + "".join(outcome.lines).encode(encoding))
            shutil.copymode(playlist, temporary)
            os.replace(temporary, playlist)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    except OSError as e:
        click.secho(f"Could not write {shown}, left as it is: {e}", fg="yellow")


def _decode(data: bytes, extension: str) -> tuple[bytes, str, str] | None:
    """The file's BOM, its text and its encoding; None when it is not text we can write back as it was."""
    if b"\x00" in data:
        return None
    bom = _UTF8_BOM if data.startswith(_UTF8_BOM) else b""
    body = data[len(bom) :]
    try:
        return bom, body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    if extension == ".m3u8":
        return None
    try:
        return bom, body.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return None


def _names(entries: list[str]) -> str:
    shown = ", ".join(entries[:_SHOWN])
    return shown + (f" and {len(entries) - _SHOWN} more" if len(entries) > _SHOWN else "")


def _rewrite_lines(
    lines: list[str], cue: bool, old_folder: str, folder: str, renames: Sequence[Rename], encoding: str
) -> _Outcome:
    outcome = _Outcome(list(lines), [], [], [], False)
    old_files = {_key(old).casefold() for old, _ in renames}
    # Another code page mostly reads as cp1252 without error, as nonsense (one with a byte cp1252 leaves undefined is
    # not read at all): a non-ASCII entry naming no file may be a renamed one, when the path an entry would name a
    # renamed file by is non-ASCII, from the playlist's folder ("..\Björk\01.flac"), not the folders above it.
    unsure = encoding == "cp1252" and any(not os.path.relpath(old, old_folder).isascii() for old, _ in renames)
    for index, line in enumerate(lines):
        parts = _cue_entry(line) if cue else _m3u_entry(line)
        if parts is None:
            continue
        before, entry, after = parts
        if not entry.strip() or _URL.match(entry):
            continue
        new_entry = _new_entry(entry, old_folder, folder, renames)
        if new_entry is None:
            if _was_audio(entry, old_folder, folder, old_files):
                outcome.broken.append(entry)
            elif unsure and not entry.isascii() and not any(map(os.path.lexists, _candidates(entry, folder))):
                outcome.misread.append(entry)
            continue
        try:
            new_entry.encode(encoding)
        except UnicodeEncodeError:
            outcome.unwritable.append(entry)
            continue
        # A folder rename moves every entry under it, even one whose file was renamed too, and names it wrongly
        # when the sheet was misread: the new path names no file. Test the unquoted path.
        misread = unsure and not new_entry.isascii() and not any(map(os.path.lexists, _candidates(new_entry, folder)))
        if cue and not before.endswith('"') and " " in new_entry:
            # A bare name with a space needs its quotes.
            new_entry = f'"{new_entry}"'
        outcome.lines[index] = before + new_entry + after
        outcome.changed = True
        if misread:
            outcome.misread.append(entry)
    return outcome


def _m3u_entry(line: str) -> tuple[str, str, str] | None:
    content = line.rstrip("\r\n")
    if content.lstrip().startswith("#") or not content.strip():
        return None
    entry = content.strip()
    start = content.index(entry)
    return content[:start], entry, content[start + len(entry) :] + line[len(content) :]


def _cue_entry(line: str) -> tuple[str, str, str] | None:
    content = line.rstrip("\r\n")
    ending = line[len(content) :]
    match = _CUE_QUOTED.match(content) or _CUE_BARE.match(content)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3) + ending


def _candidates(entry: str, old_folder: str) -> list[str]:
    """The paths an entry may mean: as written, and with backslashes read as separators."""
    names = [entry]
    if "\\" in entry:
        names.append(entry.replace("\\", "/"))
    return [os.path.join(old_folder, name) for name in names]


def _new_entry(entry: str, old_folder: str, folder: str, renames: Sequence[Rename]) -> str | None:
    """The entry naming the file ``entry`` pointed to, in its separator style; None if no rename touched that file."""
    olds = [_key(old) for old, _ in renames]
    relocated = _key(old_folder) != _key(folder)
    for candidate in _candidates(entry, old_folder):
        resolved = _key(candidate)
        touched = any(resolved == old or resolved.startswith(old + os.sep) for old in olds)
        # A playlist that moved names even a file that stayed from its new folder (an absolute entry stays as it is);
        # a file that is not there is not ours.
        stayed = relocated and not os.path.isabs(entry) and os.path.lexists(os.path.normpath(candidate))
        if not touched and not stayed:
            continue
        moved = _moved(candidate, renames)
        relative = os.path.relpath(moved, os.path.normpath(folder))
        if "\\" in entry and "/" not in entry:
            return relative.replace(os.sep, "\\")
        return relative.replace(os.sep, "/")
    return None


def _was_audio(entry: str, old_folder: str, folder: str, old_files: set[str]) -> bool:
    """Whether an entry no rename touched named an audio file that existed before them, and no file has now."""
    if os.path.splitext(entry)[1].lower() not in _AUDIO_EXTENSIONS:
        return False
    if any(os.path.lexists(candidate) for candidate in _candidates(entry, folder)):
        return False
    # It matched a renamed file only in another case or form: the file moved, the entry did not follow.
    return any(_key(candidate).casefold() in old_files for candidate in _candidates(entry, old_folder))
