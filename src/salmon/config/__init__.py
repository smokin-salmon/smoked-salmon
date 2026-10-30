import os
import sys
from pathlib import Path
from typing import Any, NoReturn, get_args

import anyio
import asyncclick as click
import msgspec
import msgspec.inspect as mi
import requests
from platformdirs import user_config_dir

from . import compat
from .validations import Cfg, ImgUploaderLiteral

APPNAME = "smoked-salmon"

# Containers mount a single directory for config; platformdirs would otherwise bury
# config.toml under an app-name subdirectory of that mount.
CONFIG_DIR_ENV = "SALMON_CONFIG_DIR"

_PKG_DIR = Path(__file__).parent.parent

_warned_legacy_config_fallback = False


def _platform_cfg_path() -> Path:
    return Path(user_config_dir(APPNAME)) / "config.toml"


def get_user_cfg_path() -> Path:
    """config.toml location: $SALMON_CONFIG_DIR when set, else the platform config dir.

    If SALMON_CONFIG_DIR is set but no config.toml exists there yet, and the platform
    config dir does have one, fall back to that older path instead. This keeps a
    container upgraded from the old image (which mounted the platform config dir
    directly) working without a config change.
    """
    global _warned_legacy_config_fallback

    override = os.environ.get(CONFIG_DIR_ENV)
    if not override:
        return _platform_cfg_path()

    override_path = Path(override).expanduser() / "config.toml"
    legacy_path = _platform_cfg_path()
    if not override_path.exists() and legacy_path.exists():
        if not _warned_legacy_config_fallback:
            click.secho(
                f"{CONFIG_DIR_ENV} is set to {override!r} but {override_path} does not exist; "
                f"using the existing config at {legacy_path} instead.",
                fg="yellow",
                err=True,
            )
            _warned_legacy_config_fallback = True
        return legacy_path
    return override_path


def get_default_config_path() -> Path:
    default_config_path = _PKG_DIR / "data" / "config.default.toml"

    if not default_config_path.exists():
        click.secho(f"Default config file not found at {default_config_path}", fg="yellow")
        click.secho("Downloading from GitHub...", fg="blue")

        default_config_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            github_url = "https://raw.githubusercontent.com/smokin-salmon/smoked-salmon/master/src/salmon/data/config.default.toml"
            response = requests.get(github_url, timeout=30)
            response.raise_for_status()

            default_config_path.write_text(response.text, encoding="utf-8")

            click.secho(f"Successfully downloaded default config to {default_config_path}", fg="green")
        except requests.exceptions.RequestException as e:
            click.secho(f"Failed to download default config: {e}", fg="red")
            raise FileNotFoundError(f"Could not find or download default config file: {e}") from e
        except Exception as e:
            click.secho(f"Failed to save default config: {e}", fg="red")
            raise FileNotFoundError(f"Could not save default config file: {e}") from e

    return default_config_path


def _unwrap(tp: mi.Type) -> mi.Type:
    while isinstance(tp, mi.Metadata):
        tp = tp.type
    return tp


def _table_members(tp: mi.Type) -> list[mi.StructType]:
    """The Struct types a value of type ``tp`` can be when it is a TOML table."""
    tp = _unwrap(tp)
    if isinstance(tp, mi.StructType):
        return [tp]
    if isinstance(tp, mi.UnionType):
        return [m for member in tp.types if isinstance(m := _unwrap(member), mi.StructType)]
    return []


def _list_item_structs(tp: mi.Type) -> list[mi.StructType]:
    """The Struct types a value of type ``tp`` holds when it is an array of tables."""
    tp = _unwrap(tp)
    if isinstance(tp, mi.ListType):
        return _table_members(tp.item_type)
    if isinstance(tp, mi.UnionType):
        return [s for member in tp.types for s in _list_item_structs(member)]
    return []


def _walk_unknown(value: object, structs: list[mi.StructType], path: str, found: list[str]) -> None:
    """Append the path of every key under ``value`` that none of ``structs`` declares."""
    if not isinstance(value, dict) or not structs:
        return
    # Union members are alternatives; a key is known when the first Struct declares it.
    fields = {f.encode_name: f for f in structs[0].fields}
    for key, sub in value.items():
        sub_path = f"{path}.{key}" if path else str(key)
        field = fields.get(key)
        if field is None:
            found.append(sub_path)
            continue
        _walk_unknown(sub, _table_members(field.type), sub_path, found)
        if isinstance(sub, list):
            items = _list_item_structs(field.type)
            for i, item in enumerate(sub):
                _walk_unknown(item, items, f"{sub_path}[{i}]", found)


def unknown_keys(raw: dict[str, Any], struct_type: type[msgspec.Struct] = Cfg) -> list[str]:
    """Full dotted paths of the keys in a decoded TOML table that the schema does not declare.

    Only nested tables, optional tables and arrays of tables are descended into (``seedbox[0].name``);
    anything else, such as a table-valued setting of another shape, is left alone.
    """
    found: list[str] = []
    _walk_unknown(raw, _table_members(mi.type_info(struct_type)), "", found)
    return found


def _schema_keys(structs: list[mi.StructType], path: str, header: str, out: list[tuple[str, str]]) -> None:
    for field in structs[0].fields if structs else []:
        field_path = f"{path}.{field.encode_name}" if path else field.encode_name
        tables = _table_members(field.type)
        items = _list_item_structs(field.type)
        if tables:
            _schema_keys(tables, field_path, f"[{field_path}]", out)
        elif items:
            _schema_keys(items, field_path, f"[[{field_path}]]", out)
        else:
            out.append((field_path, header))


def _suggestions(key_path: str) -> list[str]:
    """Known keys with the same last component as ``key_path`` (case-insensitive), each with its header."""
    last = key_path.rsplit(".", 1)[-1].lower()
    known: list[tuple[str, str]] = []
    _schema_keys(_table_members(mi.type_info(Cfg)), "", "", known)
    out = []
    for path, header in known:
        if path.rsplit(".", 1)[-1].lower() != last:
            continue
        out.append(f"{path} (under {header})" if header else f"{path} (at the top of the file, before any [section])")
    return out


def _warn_unknown_keys(raw: dict[str, Any], config_path: Path) -> None:
    """Warn about each key in the config that the schema does not know. The config still loads."""
    for key_path in unknown_keys(raw):
        message = f"Unknown config key {key_path} in {config_path}: ignored."
        if suggestions := _suggestions(key_path):
            message += f" Did you mean {', '.join(suggestions)}?"
        compat.warn(config_path, message)


def _parse_config(config_path: Path) -> Cfg:
    raw = msgspec.toml.decode(config_path.read_bytes())
    # Old keys first: an old name is read under its new one, never reported as unknown.
    compat.apply(raw, config_path)
    _warn_unknown_keys(raw, config_path)
    try:
        return msgspec.convert(raw, type=Cfg)
    except msgspec.ValidationError as e:
        raise _ptpimg_removed_error(e) from e


def _ptpimg_removed_error(e: msgspec.ValidationError) -> Exception:
    """Turn msgspec's generic Literal error into a plain message when it is caused by ptpimg.

    ptpimg.me has shut down, so it was dropped from the valid image hosts. A config that
    still names it (image_uploader, cover_uploader or specs_uploader, in [image] or a
    per-tracker [image.<tracker>]) would otherwise fail with msgspec's opaque
    "Invalid enum value" message.
    """
    if "'ptpimg'" not in str(e):
        return e
    hosts = ", ".join(get_args(ImgUploaderLiteral))
    return ValueError(
        "ptpimg has shut down and is no longer a supported image host. Choose another one "
        f"in your config ({hosts}); catbox needs no API key."
    )


def find_config_path() -> Path:
    config_dir_path = get_user_cfg_path()
    root_config_path = _PKG_DIR.parent.parent / "config.toml"

    # An explicitly set SALMON_CONFIG_DIR beats the repo-root config.toml: it says
    # exactly where to look, so it should win over that implicit dev convenience.
    if os.environ.get(CONFIG_DIR_ENV) and config_dir_path.exists():
        return config_dir_path

    # You can put a config.toml in the root directory for development purposes
    if root_config_path.exists():
        return root_config_path
    elif config_dir_path.exists():
        return config_dir_path
    else:
        raise FileNotFoundError("Could not find config path")


# The file setup_config() loaded: salmon web's Dashboard names it next to the warnings that file gave.
loaded_config_path: Path | None = None


def setup_config() -> Cfg:
    global loaded_config_path
    try:
        path = find_config_path()
    except FileNotFoundError:
        _first_run(get_user_cfg_path())
    loaded_config_path = path
    return _parse_config(path)


def _first_run(cfg_path: Path) -> NoReturn:
    """No config: ask for one in a terminal, else copy the short file to fill in. Exits either way."""
    from . import first_run

    click.secho(f"No config found at {cfg_path}.", fg="yellow")
    try:
        template = get_default_config_path()
        if not first_run.is_terminal():
            first_run.copy_and_explain(template, cfg_path)
            sys.exit(1)
    except OSError as e:
        click.secho(f"Failed to create a config: {e}", fg="red")
        sys.exit(1)
    sys.exit(anyio.run(first_run.run, template, cfg_path))
