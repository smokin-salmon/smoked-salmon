#!/usr/bin/env python3

# flake8: noqa

import click

import salmon.commands
from salmon.common import commandgroup
from salmon.errors import FilterError, LoginError, UploadError

def main():
    try:
        commandgroup(obj={})
    except (UploadError, FilterError) as e:
        click.secho(f"There was an error: {e}", fg="red", bold=True)
    except LoginError:
        click.secho(f"Failed to log in. Is your session cookie up to date?", fg="red")
    except ImportError as e:
        click.secho(f"You are missing required dependencies: {e}", fg="red")


if __name__ == "__main__":
    main()

