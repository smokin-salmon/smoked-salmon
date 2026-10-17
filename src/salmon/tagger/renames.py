"""Renaming a batch of files without ever replacing one: os.rename replaces an existing file silently on POSIX.

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


def _same_entry(a: str, b: str) -> bool:
    """Whether the two paths name one directory entry (a name given in another case, on a volume that ignores it)."""
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
        return lambda path: unicodedata.normalize("NFC", os.path.normpath(path)).casefold()
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


def rename_all(renames: Sequence[Rename]) -> None:
    """Rename each pair in turn, never onto a path in use (other than by the file itself, under another case).

    Raises:
        RenameStopped: A rename failed, or its new path was in use; the renames after it were not made.
    """
    for done, (old, new) in enumerate(renames):
        try:
            if os.path.lexists(new) and not _same_entry(old, new):
                raise FileExistsError(errno.EEXIST, "Another file already has the new name", new)
            os.rename(old, new)
        except OSError as e:
            raise RenameStopped(e, renames[:done], (old, new), renames[done + 1 :]) from e
