#!/usr/bin/env python3

# flake8: noqa

import click

import salmon.commands
from salmon.common import commandgroup
from salmon.errors import FilterError, LoginError, UploadError
from salmon.release_notification import show_release_notification

def main():
    try:
        show_release_notification()
        click.echo()

        commandgroup(obj={})
    except (UploadError, FilterError) as e:
        click.secho(f"There was an error: {e}", fg="red", bold=True)
    except LoginError:
        click.secho(f"Failed to log in. Is your session cookie up to date? Run the checkconf command to diagnose.", fg="red")
    except ImportError as e:
        click.secho(f"You are missing required dependencies: {e}", fg="red")


if __name__ == "__main__":
    main()

