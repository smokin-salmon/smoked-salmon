"""Read and change each tracker's session cookie and API key in config.toml, for salmon web's Settings page (#1034).

Only those two keys, in a tracker's own table, written where they are with comments and layout kept (tomlkit). The
whole edited text is read as a start reads it before anything is written, so a save never leaves a config salmon
would refuse. ``cfg`` is not changed: a salmon web job reads the credentials from the file when it starts (ADR 0008).
"""

import os
import stat
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

import asyncclick as click
import msgspec
import tomlkit
from tomlkit.items import String, StringType

from salmon.config import _parse_text, compat
from salmon.config.validations import Cfg

TRACKER_CODES = ("RED", "OPS", "DIC")

# Saves run one at a time: each reads the file afresh, changes it and writes it back.
_saving = threading.Lock()


class Credentials(msgspec.Struct, frozen=True):
    """A tracker's session cookie and API key, as the config gives them."""

    session: str
    api_key: str | None = None


class CredentialsUpdate(msgspec.Struct, forbid_unknown_fields=True):
    """What a save changes: a key left out is kept, and an api_key of null or "" is removed."""

    session: str | msgspec.UnsetType = msgspec.UNSET
    api_key: str | None | msgspec.UnsetType = msgspec.UNSET


class ConfigEditError(click.ClickException):
    """The config could not be read, or the change could not be made. The message names the file."""


def _parsed(text: str, path: Path) -> Cfg:
    # The load gave its warnings once already: reading the file again gives none.
    try:
        with compat.quiet():
            return _parse_text(text, path)
    except (msgspec.MsgspecError, ValueError) as e:
        raise ConfigEditError(f"{path}: {e}") from None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigEditError(f"Could not read {path}: {e}") from None


def read_credentials(path: Path) -> dict[str, Credentials]:
    """Each tracker's credentials in the config at ``path``, by site code: what a salmon web job reads as it starts.

    Only the tracker tables are checked, the old keys read as a start reads them: a folder of the config that is
    offline (a library on a NAS) must fail no job. A save checks the whole file (``update_credentials``).

    Raises:
        ConfigEditError: If the file cannot be read, or its trackers' session or api_key would be refused.
    """
    text = _read(path)
    try:
        with compat.quiet():
            raw = msgspec.toml.decode(text)
            compat.apply(raw, path)
        tables = raw.get("tracker", {})
        if not isinstance(tables, dict):
            raise ValueError("tracker must be a table")
        found: dict[str, Credentials] = {}
        for code in TRACKER_CODES:
            table = tables.get(code.lower())
            if table is not None:
                found[code] = msgspec.convert(table, type=Credentials)
        return found
    except (msgspec.MsgspecError, ValueError) as e:
        raise ConfigEditError(f"{path}: {e}") from None


def writable(path: Path) -> bool:
    """Whether salmon may write the config file itself (not only the folder it is in).

    A file with no write bit set is read-only even to root, who may write it anyway (the Docker image runs as root):
    ``chmod a-w config.toml`` turns saving off.
    """
    real = path.resolve()
    no_write_bit = not real.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    return not no_write_bit and os.access(real, os.W_OK)


def _string(old: object, value: str) -> String:
    """``value`` as a TOML string, in single quotes when the old value was and the new one allows it.

    A single-quoted string cannot hold a quote or a control character; a double-quoted one escapes them.
    """
    literal = isinstance(old, String) and old.type is StringType.SLL and "'" not in value and value.isprintable()
    return tomlkit.string(value, literal=literal)


def _edited(text: str, path: Path, code: str, update: CredentialsUpdate) -> str:
    try:
        doc = tomlkit.parse(text)
    except ValueError as e:
        # Where the file is broken, never what it holds.
        raise ConfigEditError(f"{path}: {e}") from None
    tracker = doc.get("tracker")
    table = tracker.get(code.lower()) if isinstance(tracker, dict) else None
    if not isinstance(table, dict):
        raise ConfigEditError(f"{code} is not in {path}: add [tracker.{code.lower()}] by hand and restart salmon web.")
    if update.session is not msgspec.UNSET:
        table["session"] = _string(table.get("session"), update.session)
    if update.api_key is not msgspec.UNSET:
        if update.api_key:
            table["api_key"] = _string(table.get("api_key"), update.api_key)
        elif "api_key" in table:
            del table["api_key"]
    return tomlkit.dumps(doc)


def _write(path: Path, text: str) -> None:
    """Write ``text`` to the file at ``path``: to a new file beside it with the old one's mode and owner, moved over it.

    A move cannot leave a file cut short if salmon stops part way. When the folder takes no new file, the owner cannot
    be kept or the move fails (a config bind-mounted on its own into a container), the file is written in place.

    Raises:
        OSError: If the new file or the file in place cannot be written (a full disk), the file being left as it was.
    """
    real = path.resolve()
    try:
        fd, temp = tempfile.mkstemp(dir=real.parent, prefix=".config.toml.")
    except PermissionError:
        _write_in_place(real, text)
        return
    try:
        moved = _replace(real, fd, temp, text)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp)
    if not moved:
        _write_in_place(real, text)


def _replace(real: Path, fd: int, temp: str, text: str) -> bool:
    """Write ``text`` to the new file ``temp`` (open as ``fd``) with ``real``'s mode and owner, and move it over it.

    Returns:
        False, nothing changed, when the owner cannot be kept or the move fails: the caller writes in place.
    """
    st = real.stat()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), stat.S_IMODE(st.st_mode))
        if hasattr(os, "fchown"):
            try:
                os.fchown(f.fileno(), st.st_uid, st.st_gid)
            except PermissionError:
                # Moved over theirs, salmon's file would make the config salmon's (root's, in the Docker image).
                return False
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(temp, real)
    except OSError:
        return False
    return True


def _write_in_place(real: Path, text: str) -> None:
    """Write ``text`` over the file ``real``, for a file that cannot be replaced.

    The file first grows to the new text's length, padded with spaces TOML ignores, so a full disk fails before the
    old text is touched. If writing over it fails all the same, the old text is put back.
    """
    new = text.encode("utf-8")
    old = real.read_bytes()
    fd = os.open(real, os.O_RDWR)
    try:
        try:
            _write_at(fd, len(old), b" " * (len(new) - len(old)))
            os.fsync(fd)
        except OSError:
            os.ftruncate(fd, len(old))
            raise
        try:
            _write_at(fd, 0, new)
            os.ftruncate(fd, len(new))
            os.fsync(fd)
        except OSError:
            _write_at(fd, 0, old)
            os.ftruncate(fd, len(old))
            raise
    finally:
        os.close(fd)


def _write_at(fd: int, offset: int, data: bytes) -> None:
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def update_credentials(path: Path, code: str, update: CredentialsUpdate) -> None:
    """Change tracker ``code``'s session cookie and/or API key in the config at ``path``.

    Raises:
        ConfigEditError: If the file cannot be read or written, has no table for the tracker, or would be refused
            once changed. The file is then left as it was.
    """
    with _saving:
        if not writable(path):
            raise ConfigEditError(f"salmon cannot write {path}: edit it by hand.")
        text = _edited(_read(path), path, code, update)
        _parsed(text, path)
        try:
            _write(path, text)
        except OSError as e:
            raise ConfigEditError(f"Could not write {path}: {e}. Edit it by hand.") from None
