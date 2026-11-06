"""Which folders salmon web may list and start jobs on (ADR 0004, section 3).

The roots are ``download_directory`` and each of ``library_dirs``, nothing else. A path is resolved (``realpath``)
before it is checked against them, and checked before anything else probes it: an answer that differs between a
missing folder and a refused one would tell a browser what exists outside the roots.

Ported from the fork's ``webui/validation.py`` and ``routers/browse.py``: the confinement from chodeus (cdacf355,
3c69045a), the roots offered side by side from chodeus (a18e0e25), a job refused on a root from styx-techno
(6e91041a). The fork's roots also held ``dottorrents_dir`` and ``tmp_dir``; these are not reachable here.
"""

import os
import shutil
import stat
from dataclasses import dataclass
from typing import Any

from salmon import cfg
from salmon.images.files import NotAnImage, check_image
from salmon.webui.jobs import JobError

AUDIO_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a"})
# The files the picker offers as images; what a file really is, the upload checks (``image_file``).
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
# Entries of one folder looked at, and folders listed, at most: a listing past them says it was cut.
MAX_SCANNED = 5000
MAX_LISTED = 500
# The orders the folder browser lists folders in: the most recently changed or copied in first, or by name.
SORTS = ("newest", "name")
# Entries looked at to mark a listed folder as holding audio (it or its disc folders).
MAX_MARK_SCAN = 200
# Folders walked in an album folder, to find any link in it: past them, it is not an album.
MAX_ALBUM_WALK = 500
# Folders walked in all, to check the album folders of one listing as a job would: past them, a folder is offered
# by its place and its audio only, and the check at Start answers for the rest.
MAX_OFFER_WALK = 2000
# A Windows junction's reparse tag: stat.IO_REPARSE_TAG_MOUNT_POINT, which the stat module defines only on Windows.
_JUNCTION = 0xA0000003


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


def free_space(path: str) -> dict[str, int | None]:
    """Free and total bytes of the disk holding a folder, or None for both when it is missing or unreadable."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"free_bytes": None, "total_bytes": None}
    return {"free_bytes": usage.free, "total_bytes": usage.total}


def _inside(path: str, folder: str) -> bool:
    """Whether the resolved path is the resolved folder or inside it. commonpath: /music-old is not in /music."""
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # Different drives on Windows.
        return False


def _same_or_inside(path: str, folder: str) -> bool:
    """Whether the resolved path is the resolved folder or inside it, by the folder itself, however it is spelled.

    The same folder in another case on a volume that ignores case, or through a second mount of it, counts. It walks
    up past a root on purpose, as a withheld folder can sit above one. For the withheld folders, where a True refuses;
    confinement stays with `_inside`, by text, or a second mount of a root would count as inside it. Looks at the disk,
    so only for a path already confined.
    """
    if _inside(path, folder):
        return True
    try:
        target = os.stat(folder)
    except OSError:
        return False
    while True:
        try:
            if os.path.samestat(os.stat(path), target):
                return True
        except OSError:  # Not there, or not to be looked at: what holds it may still be the folder.
            pass
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


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


def _is_link(entry: os.DirEntry[str]) -> bool:
    """Whether a folder entry is a symlink, or a Windows junction (which is_symlink() doesn't count before 3.12)."""
    if entry.is_symlink():
        return True
    try:
        reparse_tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
    except OSError:
        return True  # Unreadable: it could hide a link.
    return reparse_tag == _JUNCTION


def holds_audio(path: str, max_entries: int | None = None) -> bool:
    """Whether an audio file is in the folder or in one of its direct subfolders (disc folders): the one rule for an
    album folder. The picker offers such a folder and ``album_folder`` refuses a job on any other, so a folder of
    albums is never worked on whole, and a box set still is.

    A job's check (`max_entries` None) reads each folder whole; ``_refuse_links`` has already bounded how many
    folders there are. The picker, marking many folders at once, looks at `max_entries` entries in each and as many
    disc folders, so it may leave a very large album folder unmarked: it can still be typed in. An audio file is a
    regular file with an audio extension: not a folder, a link or a named pipe so named. A disc folder that is a
    symlink is not looked into. A folder that cannot be read holds none.
    """
    subfolders: list[str] = []
    try:
        with os.scandir(path) as entries:
            for seen, entry in enumerate(entries):
                if max_entries is not None and seen >= max_entries:
                    break
                if entry.is_dir(follow_symlinks=False):
                    subfolders.append(entry.path)
                elif _is_audio(entry.name) and entry.is_file(follow_symlinks=False):
                    return True
    except OSError:
        return False
    for subfolder in subfolders[:max_entries]:
        try:
            with os.scandir(subfolder) as entries:
                for seen, entry in enumerate(entries):
                    if max_entries is not None and seen >= max_entries:
                        break
                    if _is_audio(entry.name) and entry.is_file(follow_symlinks=False):
                        return True
        except OSError:
            continue
    return False


def _audio_below(path: str) -> bool:
    """Whether a regular audio file is anywhere below the folder: only to say why a folder is refused, on a folder
    ``_refuse_links`` has passed."""
    for root, _dirs, files in os.walk(path):
        if any(_is_audio(name) and _is_regular(os.path.join(root, name)) for name in files):
            return True
    return False


def _is_regular(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


@dataclass
class _Walked:
    """Folders walked for links so far, counted also when a walk refuses."""

    count: int = 0


def _refuse_links(path: str, walked: _Walked | None = None) -> None:
    """Refuse a folder holding a link of any kind, anywhere in it (#898).

    salmon's copy of an album (``uploader/staging.py``) and the torrent made from it follow links, so a link would
    bring in what it leads to: a log, a scan or a folder from outside the roots would go into the upload.

    Args:
        path: The folder.
        walked: Counts the folders this walk reads, for a caller that bounds several walks.

    Raises:
        PathRefused: The folder holds a symlink (or a junction), named by its path in the folder; more than
            ``MAX_ALBUM_WALK`` folders, past which salmon doesn't look; or a folder that can't be read, which could
            hide one.
    """
    pending = [path]
    seen = 0
    while pending:
        if seen >= MAX_ALBUM_WALK:
            raise PathRefused(
                422, f"Refusing a folder holding more than {MAX_ALBUM_WALK} folders: pick one album folder in it."
            )
        folder = pending.pop()
        seen += 1
        if walked is not None:
            walked.count += 1
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if _is_link(entry):
                        name = os.path.relpath(entry.path, path)
                        raise PathRefused(
                            403,
                            f"Refusing a folder that holds a link ({name}): salmon web works only on the files in "
                            "download_directory and library_dirs. Put the file itself there, or use salmon up.",
                        )
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
        except OSError:
            name = os.path.relpath(folder, path)
            raise PathRefused(403, f"salmon may not read the folder {name} in this album.") from None


def album_folder(raw: str) -> str:
    """The album folder a job may work on: `raw` resolved, the rule every job kind on a folder applies.

    It must resolve inside a root, must not be a root itself or a folder holding a library, must not be a symlink
    (the job would work on what it leads to, which the CLI never does), must not be, be in or hold a folder of
    .torrent files or a seedbox's (see withheld_folders), must hold no symlink at all (#898), and must hold audio in
    it or in its disc folders (``holds_audio``), checked before the job copies or changes anything.

    Raises:
        PathRefused: The folder is refused, with a status and a reason a person can act on.
    """
    path = _resolve(raw)
    found = roots()
    # Confined before anything probes the path.
    if not _within(path, found):
        raise PathRefused(403, "Refusing a folder outside download_directory and library_dirs.")
    _check_album(path, found, _withheld_from_albums(found), given=os.path.abspath(raw))
    _refuse_without_audio(path)
    return path


def _check_album(
    path: str,
    found: list[Root],
    withheld: list[str],
    *,
    given: str | None = None,
    libraries: bool = True,
    links: bool = True,
    walked: _Walked | None = None,
) -> None:
    """``album_folder``'s rule on `path`, resolved and inside a root, but for its audio (``_refuse_without_audio``):
    the one answer for Start and the picker.

    Args:
        path: The folder, resolved.
        found: The roots.
        withheld: ``_withheld_from_albums(found)``, or those of them that may concern `path`.
        given: The path as given, to refuse a symlink; None for a folder the browser lists, which is none.
        libraries: Whether to look for a library directory in it; not for a folder in one that holds none.
        links: Whether to walk the folder for links (#898); a listing past ``MAX_OFFER_WALK`` does not.
        walked: Counts the folders walked for links.

    Raises:
        PathRefused: The folder is refused, with a status and a reason a person can act on.
    """
    if any(path == root.path for root in found):
        raise PathRefused(403, "Refusing a root folder: pick one album folder in it.")
    if libraries and cfg.directory.library_inside(path) is not None:
        raise PathRefused(403, "Refusing a folder that holds a library directory: pick one album folder in it.")
    if given is not None:
        if os.path.join(os.path.realpath(os.path.dirname(given)), os.path.basename(given)) != path:
            raise PathRefused(403, "Refusing an album folder that is a symlink: open the folder it leads to.")
        # Reached through a link from a library to outside it, the folder would lose the copy salmon works on.
        if _named_in_library(given) and not cfg.directory.is_library_path(path):
            raise PathRefused(403, "Refusing an album linked into a library from outside it: open the folder itself.")
    if any(_same_or_inside(path, folder) or _same_or_inside(folder, path) for folder in withheld):
        raise PathRefused(403, "Refusing a folder of .torrent files or a seedbox's, or one that holds one.")
    if not os.path.isdir(path):
        raise PathRefused(404, "No such folder.")
    if links:
        _refuse_links(path, walked)


def _refuse_without_audio(path: str) -> None:
    """Refuse a folder with no audio in it or its disc folders (``holds_audio``), saying so when there is some deeper.

    Raises:
        PathRefused: The folder holds no audio an album folder would.
    """
    if not holds_audio(path):
        if _audio_below(path):
            raise PathRefused(
                422,
                "No audio file in this folder or its disc folders, but there are some deeper down. Pick the album "
                "folder that holds them.",
            )
        raise PathRefused(422, "No audio file (FLAC, MP3, M4A) in this folder or its disc folders.")


class _Offers:
    """Which folders of one listing the picker offers as albums: those ``album_folder`` accepts.

    A folder is first looked at as the picker can afford for each of hundreds (``holds_audio`` within
    ``MAX_MARK_SCAN`` entries), so a very large album folder may be left unoffered: it can still be typed in. One
    that passes is then checked as a job checks it, walking it for links, until the listing has walked
    ``MAX_OFFER_WALK`` folders; past that, only by its place and its audio.

    What the folders in a folder are, are in or hold, the folder listed is, is in or holds too: the withheld folders
    and library directories are looked for once, from the folder listed, not again for each folder in it.
    """

    def __init__(self, found: list[Root], path: str) -> None:
        """`path`: the folder listed, resolved."""
        self._found = found
        self._withheld = [
            folder
            for folder in _withheld_from_albums(found)
            if _same_or_inside(path, folder) or _same_or_inside(folder, path)
        ]
        self._libraries = cfg.directory.library_inside(path) is not None
        self._walked = _Walked()

    def __call__(self, path: str) -> bool:
        if not holds_audio(path, MAX_MARK_SCAN):
            return False
        links = self._walked.count < MAX_OFFER_WALK
        try:
            _check_album(path, self._found, self._withheld, libraries=self._libraries, links=links, walked=self._walked)
        except PathRefused:
            return False
        return True

    def here(self, path: str, audio: bool) -> tuple[bool, str | None]:
        """Whether the folder listed is offered (checked whole, as at Start), and if not, why, when it holds audio.

        A root is never offered, and says nothing: its albums are what its listing shows. `audio`: whether the
        listing found an audio file in it, which saves looking again.
        """
        if any(path == root.path for root in self._found) or not (audio or holds_audio(path, MAX_MARK_SCAN)):
            return False, None
        # Holding audio within the bound, it holds some read whole: _refuse_without_audio would pass.
        try:
            _check_album(path, self._found, self._withheld, libraries=self._libraries)
        except PathRefused as e:
            return False, e.detail
        return True, None


def summary(raw: str) -> dict[str, str]:
    """What a job on `raw` would work on: the folder ``album_folder`` resolves it to (or its refusal), and one line
    counting the regular audio files in it and below it and the folders holding them. Changes nothing.

    Raises:
        PathRefused: ``album_folder`` refuses the folder, as it does one of more than ``MAX_ALBUM_WALK`` folders.
    """
    path = album_folder(raw)
    files = folders = 0
    for root, _dirs, names in os.walk(path):
        found = sum(1 for name in names if _is_audio(name) and _is_regular(os.path.join(root, name)))
        if found:
            files += found
            folders += 1
    line = f"{files} audio file{'' if files == 1 else 's'} in {folders} folder{'' if folders == 1 else 's'}"
    return {"path": path, "summary": line}


def withheld_folders() -> list[str]:
    """The folders no file may be sent from: where .torrent files are kept (they carry the passkey) and the
    folders of the local seedboxes, resolved. A seedbox folder that is a root or holds one (the default is
    download_directory) is the music itself and is left out of the list: the content check stands there."""
    folders = [cfg.directory.dottorrents_dir]
    folders += [
        tracker.dottorrents_dir
        for tracker in (cfg.tracker.red, cfg.tracker.ops, cfg.tracker.dic)
        if tracker is not None and tracker.dottorrents_dir
    ]
    found = roots()
    for seedbox in cfg.seedbox:
        if seedbox.type != "local" or not seedbox.directory:
            continue
        folder = os.path.realpath(seedbox.directory)
        if not any(_same_or_inside(root.path, folder) for root in found):
            folders.append(folder)
    return [os.path.realpath(folder) for folder in folders if folder]


def _is_withheld(path: str) -> bool:
    return any(_same_or_inside(path, folder) for folder in withheld_folders())


def _withheld_from_albums(found: list[Root]) -> list[str]:
    """The withheld folders an album folder may not be, be in or hold: those that are not a root and hold none.

    A dottorrents_dir set to download_directory, or above it, would otherwise refuse every album; the .torrent files
    there are beside the albums, not in them.
    """
    return [folder for folder in withheld_folders() if not any(_same_or_inside(root.path, folder) for root in found)]


def image_file(raw: str, *, content: bool = True) -> str:
    """The image file a job may send to an image host: `raw` resolved, the rule the image upload applies.

    The file must resolve inside a root, must not be a symlink or in a folder of .torrent files or a seedbox's, must
    be a regular file, and (with `content`) must be an image by what is in it: Pillow reads it as a whole JPEG, PNG,
    GIF or WebP, whatever its name says. An image host is public: nothing that is not an image goes there.

    Args:
        raw: The path as given.
        content: Whether to read the file to check it. That decodes the whole image: a caller on the server's loop
            leaves it to a worker thread.

    Raises:
        PathRefused: The file is refused, with a status and a reason that names it.
    """
    # The name as given: the only thing an error may say before the path is known to be inside a root.
    name = os.path.basename(raw.rstrip("/\\")) or raw
    path = _resolve(raw)
    found = roots()
    # Confined before anything probes the path.
    if not _within(path, found):
        raise PathRefused(403, f"Refusing {name}: it is outside download_directory and library_dirs.")
    given = os.path.abspath(raw)
    if os.path.join(os.path.realpath(os.path.dirname(given)), os.path.basename(given)) != path:
        raise PathRefused(403, f"Refusing {name}: it is a symlink. Pick the file it leads to.")
    if _is_withheld(path):
        raise PathRefused(403, f"Refusing {name}: it is in a folder of .torrent files or a seedbox's.")
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise PathRefused(404, f"No such file: {name}.") from None
    except OSError:
        raise PathRefused(403, f"salmon may not read {name}.") from None
    # Before opening it: opening a named pipe waits for a writer.
    if not stat.S_ISREG(mode):
        raise PathRefused(422, f"Refusing {name}: it is not a regular file.")
    if content:
        try:
            check_image(path)
        except NotAnImage as e:
            raise PathRefused(422, f"Refusing {name}: it is not an image salmon will send ({e}).") from None
        except OSError:
            raise PathRefused(403, f"salmon may not read {name}.") from None
    return path


def _changed(entry: os.DirEntry[str]) -> float:
    """When the folder entry last changed or was copied in: the later of its mtime and ctime, not following a link.

    A folder copied with ``cp -a`` or ``rsync -a`` keeps its source's mtime, but its ctime (its creation time on
    Windows) is the copy's. ``DirEntry`` caches the stat, so this costs one syscall per folder. An entry that
    vanished mid listing counts as the oldest.
    """
    try:
        found = entry.stat(follow_symlinks=False)
    except OSError:
        return float("-inf")
    return max(found.st_mtime, found.st_ctime)


def browsable_folder(raw: str) -> str:
    """`raw` resolved, confined and checked as the folder browser does (same errors); a folder to read, not to work on.

    Raises:
        PathRefused: A relative path (400), one outside the roots (403), or not a folder (404).
    """
    path = _resolve(raw)
    if not _within(path, roots()):
        raise PathRefused(403, "Refusing to list a folder outside download_directory and library_dirs.")
    if not os.path.isdir(path):
        raise PathRefused(404, "No such folder.")
    return path


def listing(raw: str | None, images: bool = False, sort: str = "newest") -> dict[str, Any]:
    """What the folder browser shows: the roots, or the folders inside one folder of a root. Changes nothing.

    A path in a root that is not a folder (an album an upload has removed since) lists the nearest folder above it,
    never above the root: ``path`` is the folder listed, and ``missing`` the path asked for, null when that is the
    folder listed. A path outside the roots is refused before anything probes it.

    Folders whose name starts with a dot and symlinks are left out. Each folder listed, and the folder listed
    itself, says ``audio`` true when the picker offers it as an album: when ``album_folder`` accepts it, as a job
    does at Start (see ``_Offers`` for its bounds). A root never is, in the roots view and when listed itself. The
    folder listed says why it is not offered (``refused``) when it holds audio all the same, else null.
    At most ``MAX_LISTED`` folders are listed, out of the first
    ``MAX_SCANNED`` entries (in the order the disk gives them: a folder of more entries is not read past them);
    ``truncated`` says when the folder holds more. The folders are sorted before that cut: ``sort`` is
    ``"newest"`` (the default: the most recently changed or copied in first, ties by name) or ``"name"``, and
    the answer says which. The roots keep the order they are configured in, and the image files go by name.

    With `images`, the folder's image files are listed too (``images``, at most ``MAX_LISTED``, with their sizes):
    the regular files, not symlinks, whose name says JPEG, PNG, GIF or WebP, in no folder of .torrent files or a
    seedbox's. What a file is, the upload checks again.

    Raises:
        PathRefused: A path outside the roots, a root that is not a folder, or a sort that is not one of ``SORTS``.
    """
    if sort not in SORTS:
        raise PathRefused(400, f"Unknown sort: use one of {', '.join(SORTS)}.")
    found = roots()
    shown_roots = [root.shown() for root in found]
    if not raw:
        return {
            "path": None,
            "missing": None,
            "parent": None,
            "library": False,
            "audio": False,
            "refused": None,
            "folders": [{**root.shown(), "audio": False} for root in found],
            "truncated": False,
            "sort": sort,
            "roots": shown_roots,
            **({"images": []} if images else {}),
        }
    asked = _resolve(raw)
    if not _within(asked, found):
        raise PathRefused(403, "Refusing to list a folder outside download_directory and library_dirs.")
    path = asked
    # Up to the root it is in at most: inside a root, every folder above the path is in it too.
    while not os.path.isdir(path) and not any(path == root.path for root in found):
        parent = os.path.dirname(path)
        if parent == path:  # A drive or share root spelled otherwise than its root: never loop on it.
            break
        path = parent
    if not os.path.isdir(path):
        raise PathRefused(404, "No such folder.")
    names: list[tuple[str, str, float]] = []
    pictures: list[tuple[str, str, int]] = []
    truncated = False
    audio = False
    # An image in a folder of .torrent files is never offered.
    offered = images and not _is_withheld(path)
    try:
        with os.scandir(path) as entries:
            for seen, entry in enumerate(entries):
                if seen >= MAX_SCANNED:
                    truncated = True
                    break
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    names.append((entry.name, entry.path, _changed(entry) if sort == "newest" else 0.0))
                elif entry.is_file(follow_symlinks=False):
                    if not audio and _is_audio(entry.name):
                        audio = True
                    elif offered and os.path.splitext(entry.name.lower())[1] in IMAGE_EXTENSIONS:
                        pictures.append((entry.name, entry.path, entry.stat(follow_symlinks=False).st_size))
    except PermissionError:
        raise PathRefused(403, "salmon may not read this folder.") from None
    # Sorted before the cut, so the newest folders are always inside it.
    names.sort(key=lambda item: (-item[2], item[0].lower()) if sort == "newest" else item[0].lower())
    pictures.sort(key=lambda item: item[0].lower())
    if len(names) > MAX_LISTED or len(pictures) > MAX_LISTED:
        truncated = True
        names = names[:MAX_LISTED]
        pictures = pictures[:MAX_LISTED]
    parent = os.path.dirname(path)
    offers = _Offers(found, path)
    selectable, refused = offers.here(path, audio)
    answer: dict[str, Any] = {
        "path": path,
        "missing": asked if asked != path else None,
        "parent": parent if _within(parent, found) and parent != path else None,
        "library": cfg.directory.is_library_path(path),
        "audio": selectable,
        "refused": refused,
        "folders": [{"name": name, "path": full, "audio": offers(full)} for name, full, _ in names],
        "truncated": truncated,
        "sort": sort,
        "roots": shown_roots,
    }
    if images:
        answer["images"] = [{"name": name, "path": full, "size": size} for name, full, size in pictures]
    return answer
