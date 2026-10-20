"""Old config keys and old template values, read with a warning for one release (ADR 0005).

``apply`` rewrites the decoded TOML in place before the typed decode: a moved or renamed key is read under its old
name until ``refused_from``, a removed one is dropped, and a value left over from the old template counts as unset.
The rest of salmon reads only the new names. The user's file is never rewritten.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncclick as click
from packaging.version import InvalidVersion, Version

REFUSED_FROM = "0.22.0"
UPGRADE_PAGE = "https://github.com/smokin-salmon/smoked-salmon/wiki/Upgrading-to-0.21"


@dataclass(frozen=True)
class Moved:
    """A key read under its old name, with a warning, until ``refused_from``."""

    old: str
    new: str
    convert: Callable[[Any], Any] | None = None
    refused_from: str = REFUSED_FROM


@dataclass(frozen=True)
class Removed:
    """A key salmon no longer reads. Warned about when ignoring it changes nothing, refused otherwise.

    It has no ``refused_from``: the warning lasts as long as the entry stays in the table.
    """

    old: str
    reason: str
    default: Any = None
    # It never did anything, so ignoring it changes nothing whatever its value.
    did_nothing: bool = False


def _one_tracker_to_list(value: Any) -> Any:
    """``default_tracker = "RED"`` as ``default_trackers = ["RED"]``; anything else is left for the decode to refuse."""
    return [value] if isinstance(value, str) else value


OLD_KEYS: tuple[Moved | Removed, ...] = (
    Moved("tracker.default_tracker", "tracker.default_trackers", convert=_one_tracker_to_list),
    Moved("upload.web_interface.host", "upload.spectrals_viewer.host"),
    Moved("upload.web_interface.port", "upload.spectrals_viewer.port"),
    Moved("upload.web_interface.display_host", "upload.spectrals_viewer.display_host"),
    Moved("upload.debug_tracker_connection", "tracker.debug_connection"),
    Moved("upload.description.review_as_comment_tag", "upload.formatting.review_as_comment_tag"),
    Moved("upload.description.fullwidth_replacements", "upload.formatting.fullwidth_replacements"),
    Moved("upload.description.empty_track_comment_tag", "upload.formatting.empty_track_comment_tag"),
    Moved("upload.compression.lma_comment_in_t_desc", "upload.description.lma_comment_in_t_desc"),
    Moved("image.auto_compress_cover", "image.embed_missing_cover"),
    Removed(
        "upload.web_interface.static_root_url",
        "the spectrals viewer now always serves its files at /static",
        default="/static",
    ),
    Removed(
        "metadata.tidal.token",
        "it only worked with Tidal's retired API. Register a client at https://developer.tidal.com and set "
        "client_id and client_secret under [metadata.tidal] instead",
        did_nothing=True,
    ),
)

_TRACKER_CODES = ("red", "ops", "dic")
_SESSION_PLACEHOLDER = "get-from-site-cookie"

# Values the old config.default.toml shipped (and the older Qobuz ones), by setting: none of them is a real value.
PLACEHOLDERS: dict[str, str] = {
    **{f"tracker.{code}.session": _SESSION_PLACEHOLDER for code in _TRACKER_CODES},
    **{f"image.{key}": "api_key" for key in ("ptscreens_key", "oeimg_key", "imgbb_key", "ra_key")},
    "metadata.discogs_token": "discogs-token",
    "metadata.beatport.username": "username",
    "metadata.beatport.password": "password",
    "metadata.qobuz.app_id": "app-id",
    "metadata.qobuz.user_auth_token": "user_auth_token",
}

# Every warning about the config, unknown keys included, in the order printed: for salmon web's Dashboard.
config_warnings: list[str] = []
# (config file, message) pairs already printed, so each is printed once per config path.
_warned: set[tuple[str, str]] = set()

_MISSING = object()


def warn(config_path: Path, message: str) -> None:
    """Print a config warning, once per config path, and keep it in ``config_warnings``."""
    if (str(config_path), message) in _warned:
        return
    _warned.add((str(config_path), message))
    config_warnings.append(message)
    click.secho(message, fg="yellow", err=True)


def placeholder(path: str) -> str | None:
    """The old template's placeholder for the setting at the dotted ``path``, if it had one."""
    return PLACEHOLDERS.get(path)


def _salmon_version() -> Version | None:
    # Imported here: salmon.release_notification imports salmon, which is still loading its config at this point.
    from salmon import release_notification

    current = release_notification.get_version()
    if current is None:
        return None
    try:
        return Version(current)
    except InvalidVersion:
        return None


def _refused(refused_from: str, version: Version | None) -> bool:
    """Whether this salmon no longer honours an old key: an unknown version counts as before ``refused_from``."""
    return version is not None and version >= Version(refused_from)


def _works_in(refused_from: str) -> str:
    """The release line before ``refused_from``: "0.21" for "0.22.0"."""
    version = Version(refused_from)
    return f"{version.major}.{version.minor - 1}" if version.minor else str(version.major - 1)


def _get(raw: dict[str, Any], path: str) -> Any:
    node: Any = raw
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _remove(raw: dict[str, Any], path: str) -> None:
    """Remove the key at ``path``, then each table above it that this leaves empty."""
    *parents, leaf = path.split(".")
    chain = [raw]
    for key in parents:
        chain.append(chain[-1][key])
    del chain[-1][leaf]
    for key, table, parent in zip(reversed(parents), reversed(chain[1:]), reversed(chain[:-1]), strict=True):
        if table:
            break
        del parent[key]


def _set(raw: dict[str, Any], path: str, value: Any) -> bool:
    """Set the key at ``path``, making the tables above it. False when one of them is set to something else."""
    *parents, leaf = path.split(".")
    node = raw
    for key in parents:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            return False
    node[leaf] = value
    return True


def _move(raw: dict[str, Any], entry: Moved, value: Any, version: Version | None, config_path: Path) -> None:
    section = entry.new.rpartition(".")[0]
    message = (
        f"{entry.old} is now {entry.new} (under [{section}]): it still works in {_works_in(entry.refused_from)}, "
        f"and stops salmon from {entry.refused_from}. See {UPGRADE_PAGE}"
    )
    if _get(raw, entry.new) is not _MISSING:
        raise ValueError(
            f"{entry.old} and {entry.new} are both set: {entry.old} is the old name of {entry.new}, "
            f"so remove {entry.old}. See {UPGRADE_PAGE}"
        )
    if _refused(entry.refused_from, version):
        raise ValueError(message)
    converted = entry.convert(value) if entry.convert is not None else value
    # A table above the new key set to something else is left for the typed decode to refuse.
    if _set(raw, entry.new, converted):
        _remove(raw, entry.old)
        warn(config_path, message)


def _drop_removed(raw: dict[str, Any], entry: Removed, value: Any, config_path: Path) -> None:
    if not entry.did_nothing and value != entry.default:
        # Ignoring it would change what salmon does: refused now, not after a release of warnings.
        raise ValueError(f"{entry.old} is no longer used: {entry.reason}. Remove it. See {UPGRADE_PAGE}")
    _remove(raw, entry.old)
    warn(config_path, f"{entry.old} is no longer used, remove it: {entry.reason}. See {UPGRADE_PAGE}")


def _drop_placeholder_trackers(raw: dict[str, Any], config_path: Path) -> None:
    """Leave out each tracker whose session is the old template's placeholder, and each default naming one.

    RED and OPS take an API key on its own, so one that has a key stays, with no cookie. DIC takes none.
    """
    tracker = raw.get("tracker")
    if not isinstance(tracker, dict):
        return
    dropped: list[str] = []
    for code in _TRACKER_CODES:
        table = tracker.get(code)
        if not isinstance(table, dict) or table.get("session") != _SESSION_PLACEHOLDER:
            continue
        api_key = table.get("api_key")
        if code != "dic" and isinstance(api_key, str) and api_key.strip():
            table["session"] = ""
            warn(
                config_path,
                f"tracker.{code}.session is still the old template's placeholder '{_SESSION_PLACEHOLDER}': "
                f"salmon treats it as unset, so {code.upper()} uses only its API key.",
            )
            continue
        del tracker[code]
        dropped.append(code)
        warn(
            config_path,
            f"tracker.{code}.session is still the old template's placeholder '{_SESSION_PLACEHOLDER}'. "
            f"{code.upper()} is left out: set its cookie or remove [tracker.{code}].",
        )
    defaults = tracker.get("default_trackers")
    if not isinstance(defaults, list):
        return
    left_out = [code for code in defaults if isinstance(code, str) and code.strip().lower() in dropped]
    if not left_out:
        return
    tracker["default_trackers"] = kept = [code for code in defaults if code not in left_out]
    then = "the run uploads to the others it names" if kept else "salmon asks which tracker to upload to"
    for code in dict.fromkeys(code.strip().upper() for code in left_out):
        warn(config_path, f"tracker.default_trackers names {code}, which is left out: it is ignored, so {then}.")


def _drop_placeholders(raw: dict[str, Any], config_path: Path) -> None:
    _drop_placeholder_trackers(raw, config_path)
    for path, value in PLACEHOLDERS.items():
        if path.startswith("tracker.") or _get(raw, path) != value:
            continue
        *parents, leaf = path.split(".")
        del _get(raw, ".".join(parents))[leaf]
        warn(
            config_path,
            f"{path} is still the old template's placeholder '{value}': salmon treats it as unset. "
            "Set it or remove the line.",
        )


def apply(raw: dict[str, Any], config_path: Path) -> None:
    """Rewrite the decoded config in place: old keys under their new names, old placeholders unset.

    Raises:
        ValueError: A moved key is set with its new name, or this salmon no longer honours its old name, or a
            removed key holds a value that ignoring would change.
    """
    version = _salmon_version()
    for entry in OLD_KEYS:
        value = _get(raw, entry.old)
        if value is _MISSING:
            continue
        if isinstance(entry, Moved):
            _move(raw, entry, value, version, config_path)
        else:
            _drop_removed(raw, entry, value, config_path)
    _drop_placeholders(raw, config_path)
