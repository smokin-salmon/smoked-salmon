"""Which folders salmon web may list and start jobs on (ADR 0004, section 3).

The roots are ``download_directory`` and each of ``library_dirs``, nothing else. A path is resolved (``realpath``)
before it is checked against them, and checked before anything else probes it: an answer that differs between a
missing folder and a refused one would tell a browser what exists outside the roots.

Ported from the fork's ``webui/validation.py`` and ``routers/browse.py``: the confinement from chodeus (cdacf355,
3c69045a), the roots offered side by side from chodeus (a18e0e25), a job refused on a root from styx-techno
(6e91041a). The fork's roots also held ``dottorrents_dir`` and ``tmp_dir``; these are not reachable here.
"""

import os
from dataclasses import dataclass
from typing import Any

from salmon import cfg
from salmon.webui.jobs import JobError

AUDIO_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a"})
# Entries of one folder looked at, and folders listed, at most: a listing past them says it was cut.
MAX_SCANNED = 5000
MAX_LISTED = 500
# Entries looked at to mark a listed folder as holding audio (it or its disc folders).
MAX_MARK_SCAN = 200
# Folders walked to find an audio file in an album folder: past them, it is not an album.
MAX_ALBUM_WALK = 500


class PathRefused(JobError):
    """A path salmon web will not list or start a job on, with the HTTP status that says why."""


@dataclass(frozen=True)
class Root:
    path: str
    library: bool

    def shown(self) -> dict[str, Any]:
        return {"path": self.path, "name": os.path.basename(self.path) or self.path, "library": self.library}


def roots() -> list[Root]:
    """``download_directory``, then each of ``library_dirs``, resolved; read from the config at each call."""
    found = [Root(os.path.realpath(cfg.directory.download_directory), False)]
    found += [Root(os.path.realpath(entry), True) for entry in cfg.directory.library_dirs]
    unique: dict[str, Root] = {}
    for root in found:
        unique.setdefault(root.path, root)
    return list(unique.values())


def _inside(path: str, folder: str) -> bool:
    """Whether the resolved path is the resolved folder or inside it. commonpath: /music-old is not in /music."""
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # Different drives on Windows.
        return False


def _within(path: str, found: list[Root]) -> bool:
    return any(_inside(path, root.path) for root in found)


def _resolve(raw: str) -> str:
    """`raw` resolved, if it is an absolute path.

    Raises:
        PathRefused: A relative path, which would be resolved against the server's working folder, or a NUL.
    """
    if not raw or "\0" in raw or not os.path.isabs(raw):
        raise PathRefused(400, "Give an absolute path.")
    return os.path.realpath(raw)


def _named_in_library(given: str) -> bool:
    """Whether a path, as given (not resolved), names a folder in one of library_dirs."""
    return any(
        _inside(given, os.path.abspath(entry)) or _inside(given, os.path.realpath(entry))
        for entry in cfg.directory.library_dirs
    )


def _is_audio(name: str) -> bool:
    return os.path.splitext(name.lower())[1] in AUDIO_EXTENSIONS


def holds_audio(path: str, max_folders: int = MAX_ALBUM_WALK) -> bool:
    """Whether an audio file is in the folder or below it, as salmon finds an album's files, within `max_folders`."""
    for walked, (_root, _dirs, files) in enumerate(os.walk(path)):
        if walked >= max_folders:
            return False
        if any(_is_audio(name) for name in files):
            return True
    return False


def album_folder(raw: str) -> str:
    """The album folder a job may work on: `raw` resolved, the rule every job kind on a folder applies.

    It must resolve inside a root, must not be a root itself or a folder holding a library, must not be a symlink
    (the job would work on what it leads to, which the CLI never does), and must hold audio.

    Raises:
        PathRefused: The folder is refused, with a status and a reason a person can act on.
    """
    path = _resolve(raw)
    found = roots()
    # Confined before anything probes the path.
    if not _within(path, found):
        raise PathRefused(403, "Refusing a folder outside download_directory and library_dirs.")
    if any(path == root.path for root in found):
        raise PathRefused(403, "Refusing a root folder: pick one album folder in it.")
    if cfg.directory.library_inside(path) is not None:
        raise PathRefused(403, "Refusing a folder that holds a library directory: pick one album folder in it.")
    given = os.path.abspath(raw)
    if os.path.join(os.path.realpath(os.path.dirname(given)), os.path.basename(given)) != path:
        raise PathRefused(403, "Refusing an album folder that is a symlink: open the folder it leads to.")
    # Reached through a link from a library to outside it, the folder would lose the copy salmon works on.
    if _named_in_library(given) and not cfg.directory.is_library_path(path):
        raise PathRefused(403, "Refusing an album linked into a library from outside it: open the folder itself.")
    if not os.path.isdir(path):
        raise PathRefused(404, "No such folder.")
    if not holds_audio(path):
        raise PathRefused(422, "No audio file (FLAC, MP3, M4A) in this folder.")
    return path


def _marked_audio(path: str) -> bool:
    """Whether the folder holds audio files, or its disc folders do: the folders the picker offers as albums."""
    try:
        with os.scandir(path) as entries:
            subfolders = []
            for seen, entry in enumerate(entries):
                if seen >= MAX_MARK_SCAN:
                    break
                if entry.is_file(follow_symlinks=False) and _is_audio(entry.name):
                    return True
                if entry.is_dir(follow_symlinks=False):
                    subfolders.append(entry.path)
    except OSError:
        return False
    for subfolder in subfolders[:MAX_MARK_SCAN]:
        try:
            with os.scandir(subfolder) as entries:
                for seen, entry in enumerate(entries):
                    if seen >= MAX_MARK_SCAN:
                        break
                    if entry.is_file(follow_symlinks=False) and _is_audio(entry.name):
                        return True
        except OSError:
            continue
    return False


def listing(raw: str | None) -> dict[str, Any]:
    """What the folder browser shows: the roots, or the folders inside one folder of a root. Changes nothing.

    Folders whose name starts with a dot and symlinks are left out. Each folder listed says whether it holds
    audio files (or its disc folders do). At most ``MAX_LISTED`` folders are listed, out of the first
    ``MAX_SCANNED`` entries; ``truncated`` says when the folder holds more.

    Raises:
        PathRefused: A path outside the roots, or not a folder.
    """
    found = roots()
    shown_roots = [root.shown() for root in found]
    if not raw:
        return {
            "path": None,
            "parent": None,
            "library": False,
            "audio": False,
            "folders": [{**root.shown(), "audio": _marked_audio(root.path)} for root in found],
            "truncated": False,
            "roots": shown_roots,
        }
    path = _resolve(raw)
    if not _within(path, found):
        raise PathRefused(403, "Refusing to list a folder outside download_directory and library_dirs.")
    if not os.path.isdir(path):
        raise PathRefused(404, "No such folder.")
    names: list[tuple[str, str]] = []
    truncated = False
    audio = False
    try:
        with os.scandir(path) as entries:
            for seen, entry in enumerate(entries):
                if seen >= MAX_SCANNED:
                    truncated = True
                    break
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    names.append((entry.name, entry.path))
                elif not audio and entry.is_file(follow_symlinks=False) and _is_audio(entry.name):
                    audio = True
    except PermissionError:
        raise PathRefused(403, "salmon may not read this folder.") from None
    names.sort(key=lambda item: item[0].lower())
    if len(names) > MAX_LISTED:
        truncated = True
        names = names[:MAX_LISTED]
    parent = os.path.dirname(path)
    return {
        "path": path,
        "parent": parent if _within(parent, found) and parent != path else None,
        "library": cfg.directory.is_library_path(path),
        "audio": audio or _marked_audio(path),
        "folders": [{"name": name, "path": full, "audio": _marked_audio(full)} for name, full in names],
        "truncated": truncated,
        "roots": shown_roots,
    }
