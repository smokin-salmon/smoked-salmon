import shutil
import sys
from pathlib import Path
from typing import get_args

import asyncclick as click
import msgspec
import requests
from platformdirs import user_config_dir

from .validations import Cfg, ImgUploaderLiteral

APPNAME = "smoked-salmon"

_PKG_DIR = Path(__file__).parent.parent


def get_user_cfg_path() -> Path:
    return Path(user_config_dir(APPNAME)) / "config.toml"


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


def _parse_config(config_path: Path) -> Cfg:
    try:
        return msgspec.toml.decode(config_path.read_bytes(), type=Cfg)
    except msgspec.ValidationError as e:
        raise _ptpimg_removed_error(e) from e


def _ptpimg_removed_error(e: msgspec.ValidationError) -> Exception:
    """Turn msgspec's generic Literal error into a plain message when it is caused by ptpimg.

    ptpimg.me has shut down, so it was dropped from the valid image hosts. A config that
    still names it (image_uploader, cover_uploader, specs_uploader, or a per-tracker
    [image.<tracker>] cover_uploader) would otherwise fail with msgspec's opaque
    "Invalid enum value" message.
    """
    if "'ptpimg'" not in str(e):
        return e
    hosts = ", ".join(get_args(ImgUploaderLiteral))
    return ValueError(
        "ptpimg has shut down and is no longer a supported image host. Choose another one "
        f"in your config ({hosts}); catbox needs no API key."
    )


def _try_creating_config(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)


def find_config_path() -> Path:
    config_dir_path = get_user_cfg_path()
    root_config_path = _PKG_DIR.parent.parent / "config.toml"

    # You can put a config.toml in the root directory for development purposes
    if root_config_path.exists():
        return root_config_path
    elif config_dir_path.exists():
        return config_dir_path
    else:
        raise FileNotFoundError("Could not find config path")


def setup_config() -> Cfg:
    try:
        path = find_config_path()
    except Exception:
        cfg_path = get_user_cfg_path()

        click.secho(f"Could not find configuration path at {cfg_path}.", fg="red")
        user_choice = click.confirm(f"Do you want smoked-salmon to create a default config file at {cfg_path}?")
        if user_choice:
            try:
                default_cfg = get_default_config_path()
                _try_creating_config(default_cfg, cfg_path)
            except (FileNotFoundError, OSError, PermissionError) as e:
                click.secho(f"Failed to create default config: {e}", fg="red")
                sys.exit(1)
            click.secho(
                f"Default config created at {cfg_path}. Please edit it with your settings and restart.",
                fg="green",
            )
            sys.exit(0)
        click.secho(
            f"No config was created. To get started, manually create a config file at {cfg_path} "
            "or re-run smoked-salmon to generate one from the default template.",
            fg="yellow",
        )
        sys.exit(1)

    cfg = _parse_config(path)
    return cfg
