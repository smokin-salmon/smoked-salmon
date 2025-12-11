import os
import shutil

import asyncclick as click

from salmon import cfg
from salmon.common import commandgroup
from salmon.errors import FilterError, LoginError, UploadError
from salmon.release_notification import show_release_notification


def cleanup_tmp_dir():
    """Clean up the temporary directory if configured."""
    if cfg.directory.tmp_dir and cfg.directory.clean_tmp_dir:
        try:
            for item in os.listdir(cfg.directory.tmp_dir):
                item_path = os.path.join(cfg.directory.tmp_dir, item)
                try:
                    if os.path.isfile(item_path) or os.path.islink(item_path):
                        os.unlink(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                except Exception as e:
                    click.secho(f"Failed to remove {item_path}: {e}", fg="yellow")
            click.secho(f"Cleaned temporary directory: {cfg.directory.tmp_dir}", fg="green")
        except Exception as e:
            click.secho(f"Failed to clean temporary directory: {e}", fg="yellow")


def main():
    try:
        cleanup_tmp_dir()
        show_release_notification()
        click.echo()

        commandgroup(obj={})
    except (UploadError, FilterError) as e:
        click.secho(f"There was an error: {e}", fg="red", bold=True)
    except LoginError:
        click.secho(
            "Failed to log in. Is your session cookie up to date? Run the checkconf command to diagnose.", fg="red"
        )
    except ImportError as e:
        click.secho(f"You are missing required dependencies: {e}", fg="red")


if __name__ == "__main__":
    main()
