import os
import shutil

import asyncclick as click
import msgspec
import requests
from platformdirs import user_config_dir

from .validations import Cfg

APPNAME = "smoked-salmon"

root_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def get_user_cfg_path():
    return os.path.join(user_config_dir(APPNAME), "config.toml")


def get_default_config_path():
    default_config_path = os.path.join(root_path, "data", "config.default.toml")

    if not os.path.exists(default_config_path):
        click.secho(f"Default config file not found at {default_config_path}", fg="yellow")
        click.secho("Downloading from GitHub...", fg="blue")

        os.makedirs(os.path.dirname(default_config_path), exist_ok=True)

        try:
            github_url = "https://raw.githubusercontent.com/smokin-salmon/smoked-salmon/master/data/config.default.toml"
            response = requests.get(github_url, timeout=30)
            response.raise_for_status()

            with open(default_config_path, "w", encoding="utf-8") as f:
                f.write(response.text)

            click.secho(f"Successfully downloaded default config to {default_config_path}", fg="green")
        except requests.exceptions.RequestException as e:
            click.secho(f"Failed to download default config: {e}", fg="red")
            raise FileNotFoundError(f"Could not find or download default config file: {e}") from e
        except Exception as e:
            click.secho(f"Failed to save default config: {e}", fg="red")
            raise FileNotFoundError(f"Could not save default config file: {e}") from e

    return default_config_path


def _parse_config(config_path):
    with open(config_path, "rb") as f:
        cfg_string = f.read()
        return msgspec.toml.decode(cfg_string, type=Cfg)


def _try_creating_config(src, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(src, dest)


def find_config_path():
    config_dir_path = get_user_cfg_path()
    root_config_path = os.path.join(root_path, "config.toml")

    # You can put a config.toml in the root directory for development purposes
    if os.path.exists(root_config_path):
        config_path = root_config_path
    elif os.path.exists(config_dir_path):
        config_path = config_dir_path
    else:
        raise FileNotFoundError("Could not find config path")

    return config_path


def setup_config():
    try:
        path = find_config_path()
    except Exception:
        cfg_path = get_user_cfg_path()
        attempted_default_cfg = os.path.join(os.path.dirname(cfg_path), "config.default.toml")

        click.secho(f"Could not find configuration path at {cfg_path}.", fg="red")
        if os.path.exists(attempted_default_cfg):
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
