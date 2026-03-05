import asyncclick as click

from salmon.config import Cfg, find_config_path, setup_config

try:
    cfg: Cfg = setup_config()
except Exception as e:
    click.secho(f"Configuration error: {find_config_path()}", fg="yellow")
    click.secho(e, fg="red")
    exit(-1)

# config = Config()
