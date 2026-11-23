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
    """Each tracker's credentials in the config at ``path``, by site code, once the whole file is read as a start does.

    Raises:
        ConfigEditError: If the file cannot be read, or salmon would refuse it.
    """
    config = _parsed(_read(path), path)
    found: dict[str, Credentials] = {}
    for code in TRACKER_CODES:
        tracker = getattr(config.tracker, code.lower())
        if tracker is not None:
            found[code] = Credentials(tracker.session, tracker.api_key)
    return found


def writable(path: Path) -> bool:
    """Whether salmon may write the config file itself (not only the folder it is in)."""
    return os.access(path.resolve(), os.W_OK)


def _string(old: object, value: str) -> String:
    """``value`` as a TOML string, in single quotes when the old value was and the new one allows it."""
    literal = isinstance(old, String) and old.type is StringType.SLL and "'" not in value and "\n" not in value
    return tomlkit.string(value, literal=literal)


def _edited(text: str, path: Path, code: str, update: CredentialsUpdate) -> str:
    doc = tomlkit.parse(text)
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
    """Write ``text`` to the file at ``path``: to a new file beside it, then moved over it, with the old file's mode.

    A move cannot leave a file cut short if salmon stops part way. When the folder takes no new file or the move
    fails (a config bind-mounted on its own into a container), the file is written in place instead.
    """
    real = path.resolve()
    mode = stat.S_IMODE(real.stat().st_mode)
    temp: str | None = None
    try:
        fd, temp = tempfile.mkstemp(dir=real.parent, prefix=".config.toml.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, real)
        return
    except OSError:
        if temp is not None:
            with suppress(OSError):
                os.unlink(temp)
    real.write_text(text, encoding="utf-8")


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
