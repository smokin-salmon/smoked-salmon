import shutil
from pathlib import Path

import asyncclick as click
import msgspec
import requests
from platformdirs import user_config_dir

from .validations import Cfg

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
    return msgspec.toml.decode(config_path.read_bytes(), type=Cfg)


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
        attempted_default_cfg = cfg_path.parent / "config.default.toml"

        click.secho(f"Could not find configuration path at {cfg_path}.", fg="red")
        if attempted_default_cfg.exists():
            click.secho(
                "Hint: Create a config by copying config.default.toml to config.toml. Hope you enjoy your salmon :)",
                fg="yellow",
            )
        else:
            user_choice = click.confirm(
                f"Do you want smoked-salmon to create a default config file at {attempted_default_cfg}?"
            )
            if user_choice:
                default_cfg = get_default_config_path()
                _try_creating_config(default_cfg, attempted_default_cfg)
        exit(-1)

    cfg = _parse_config(path)
    return cfg
