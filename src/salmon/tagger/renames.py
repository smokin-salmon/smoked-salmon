"""Renaming a batch of files without replacing one that exists: os.rename replaces an existing file silently on POSIX.

A batch is planned before its first rename (``plan_renames``), then renamed one file at a time, each checked
right before it is renamed (``rename_all``). A rename that fails stops the batch; nothing is undone.
"""

import errno
import os
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence

Rename = tuple[str, str]


class RenameStopped(Exception):
    """A rename of a batch failed: the ones before it were made, it and the ones after it were not."""

    def __init__(self, error: OSError, done: Sequence[Rename], failed: Rename, left: Sequence[Rename]) -> None:
        super().__init__(str(error))
        self.error = error
        self.done = list(done)
        self.failed = failed
        self.left = list(left)

    def report(self, root: str) -> str:
        """What was renamed and what was not, the paths shown relative to ``root``."""

        def shown(name: str) -> str:
            return os.path.relpath(name, root)

        lines = [f"Renaming {shown(self.failed[0])} failed: {self.error}"]
        if self.done:
            lines.append("Renamed: " + ", ".join(f"{shown(old)} >>> {shown(new)}" for old, new in self.done))
        lines.append("Not renamed: " + ", ".join(shown(old) for old, _ in [self.failed, *self.left]))
        return "\n".join(lines)


def _folded(path: str) -> str:
    """``path`` as a volume that ignores case and Unicode form compares it."""
    return unicodedata.normalize("NFC", os.path.normpath(path)).casefold()


def _same_entry(a: str, b: str) -> bool:
    """Whether the two paths name one directory entry (a name given in another case, on a volume that ignores it)."""
    # A second hard link is another entry for the file: os.rename onto it does nothing and leaves both names.
    if _folded(a) != _folded(b):
        return False
    try:
        return os.path.samestat(os.lstat(a), os.lstat(b))
    except OSError:
        return False


def _folds_case(path: str) -> bool:
    """Whether the folder holding ``path``, which exists, finds names whatever their case."""
    directory, name = os.path.split(path)
    swapped = name.swapcase()
    return swapped != name and _same_entry(path, os.path.join(directory, swapped))


def name_key(paths: Iterable[str]) -> Callable[[str], str]:
    """The form in which two paths under the folders of ``paths`` (existing files) name the same file.

    Case-insensitive when a volume holding one of them is.
    """
    if any(_folds_case(path) for path in paths):
        return _folded
    return os.path.normpath


def plan_renames(renames: Sequence[Rename]) -> tuple[list[Rename], list[str]]:
    """Order a batch of renames (old path, new path) so that none replaces a file, or name the new paths that clash.

    A new path clashes when it is given to more than one file, when a file that is not renamed already has it,
    or when the files go round in a circle of names (a swap). Without a clash, the renames come back in an order
    where each file moves away before another takes its name, with no clashes; otherwise unchanged, with them.
    """
    key = name_key(old for old, _ in renames)
    moving = {key(old) for old, _ in renames}
    given = Counter(key(new) for _, new in renames)
    clashes: list[str] = []
    for old, new in renames:
        taken = os.path.lexists(new) and not _same_entry(old, new) and key(new) not in moving
        if (given[key(new)] > 1 or taken) and new not in clashes:
            clashes.append(new)
    if clashes:
        return list(renames), clashes

    ordered: list[Rename] = []
    pending = list(renames)
    while pending:
        held = {key(old) for old, _ in pending}
        ready = [(old, new) for old, new in pending if key(new) == key(old) or key(new) not in held]
        if not ready:
            return list(renames), [new for _, new in pending]
        ordered.extend(ready)
        pending = [rename for rename in pending if rename not in ready]
    return ordered, []


# What os.link raises where the volume has no hard links, or no more for the file.
_NO_HARD_LINKS = frozenset(
    code
    for code in (getattr(errno, name, None) for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "EMLINK", "ENOSYS"))
    if code is not None
)


def _rename_no_clobber(old: str, new: str) -> None:
    """``os.rename`` by a hard link, which fails on a name in use where a rename replaces the file there.

    A folder or a symlink, which have no hard link here, the file itself under another case, and a file on a volume
    without hard links are renamed with os.rename.
    """
    if os.path.islink(old) or os.path.isdir(old) or _same_entry(old, new):
        os.rename(old, new)
        return
    try:
        os.link(old, new)
    except OSError as e:
        if e.errno not in _NO_HARD_LINKS:
            raise
        # A volume without hard links (FAT, many network shares): the rename rename_all checked, as before.
        os.rename(old, new)
        return
    try:
        linked = os.path.samestat(os.lstat(old), os.lstat(new))
    except FileNotFoundError:
        return  # The old name went in the meantime: the link is the rename.
    if not linked:
        raise OSError(errno.EPERM, "The file changed during its rename; both names are left", old)
    try:
        os.unlink(old)
    except OSError as e:
        # One file under both names would be uploaded twice.
        try:
            os.unlink(new)
        except OSError:
            raise OSError(e.errno, f"{e.strerror}; the file is left under both names", old) from e
        raise


def rename_all(renames: Sequence[Rename]) -> None:
    """Rename each pair in turn, its new path checked just before, so a path that already exists is never replaced.

    The file itself under another case is not in the way. A file created between the check and the rename is not
    replaced either: a file is renamed by a hard link, which fails on a name in use (``_rename_no_clobber``). A
    folder, a symlink, or a file on a volume without hard links is renamed with os.rename, which can still replace
    what appears at its new name in that moment; nothing else writes into the folder.

    Raises:
        RenameStopped: A rename failed, or its new path was in use; the renames after it were not made.
    """
    for done, (old, new) in enumerate(renames):
        try:
            if os.path.lexists(new) and not _same_entry(old, new):
                raise FileExistsError(errno.EEXIST, "Another file already has the new name", new)
            _rename_no_clobber(old, new)
        except OSError as e:
            raise RenameStopped(e, renames[:done], (old, new), renames[done + 1 :]) from e


def rename_recorded(folder: str, renames: Sequence[Rename]) -> None:
    """``rename_all`` inside folder, keeping the record of the files salmon converted into it in step.

    On a stop, the record follows the renames that were made, so it never names a file that was renamed away.

    Raises:
        RenameStopped: As ``rename_all``.
    """
    # Local: the converter package imports the tagger.
    from salmon.converter.conversions import note_renamed

    try:
        rename_all(renames)
    except RenameStopped as e:
        note_renamed(folder, e.done)
        raise
    note_renamed(folder, renames)
