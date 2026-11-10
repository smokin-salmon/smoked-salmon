import sys

import asyncclick as click

from salmon.config import Cfg, find_config_path, setup_config
from salmon.localization import text as _zh

try:
    cfg: Cfg = setup_config()
except Exception as e:
    click.secho(_zh(f"Configuration error: {find_config_path()}"), fg="yellow")
    click.secho(_zh(str(e)), fg="red")
    sys.exit(1)

# config = Config()
