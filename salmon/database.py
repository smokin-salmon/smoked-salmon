import shutil
import sqlite3
from os import listdir, makedirs, path

import click
from platformdirs import user_data_dir

from salmon.common import commandgroup
from salmon.config import APPNAME

DB_DIR = user_data_dir(appname=APPNAME)
DB_PATH = path.join(DB_DIR, "smoked.db")
OLD_DB_PATH = path.abspath(path.join(path.dirname(path.dirname(__file__)), "smoked.db"))
MIG_DIR = path.abspath(path.join(path.dirname(path.dirname(__file__)), "data", "migrations"))


@commandgroup.command()
@click.option(
    "--list", "-l", is_flag=True, help="List migrations instead of migrating."
)
def migrate(list):
    """Migrate database to newest version"""
    if list:
        list_migrations()
        return

    current_version = get_current_version()
    ran_once = False
    makedirs(DB_DIR, exist_ok=True)
    if path.exists(OLD_DB_PATH):
        click.secho(f"Moving existing smoked.db to {DB_PATH}...", fg="yellow")
        shutil.move(OLD_DB_PATH, DB_PATH)
    else:
        click.secho(f"Connecting to database at {DB_PATH}...", fg="yellow")
    with sqlite3.connect(DB_PATH) as conn:
        for migration in sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql")):
            try:
                mig_version = int(migration[:4])
            except TypeError:
                click.secho(
                    f"\n{migration} is improperly named. It must start with "
                    "a four digit integer.",
                    fg="red",
                )
                raise click.Abort from None

            if mig_version > current_version:
                ran_once = True
                click.secho(f"Running {migration}...")
                cursor = conn.cursor()
                with open(path.join(MIG_DIR, migration)) as mig_file:
                    cursor.executescript(mig_file.read())
                    cursor.execute(
                        "INSERT INTO version (id) VALUES (?)", (mig_version,)
                    )
                conn.commit()
                cursor.close()

    if not ran_once:
        click.secho("You are already caught up with all migrations.", fg="green")


def list_migrations():
    """List migration history and current status"""
    current_version = get_current_version()
    for migration in sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql")):
        try:
            mig_version = int(migration[:4])
        except TypeError:
            click.secho(
                f"\n{migration} is improperly named. It must start with a "
                "four digit integer.",
                fg="red",
            )
            raise click.Abort from None

        if mig_version == current_version:
            click.secho(f"{migration} (CURRENT)", fg="cyan", bold=True)
        else:
            click.echo(migration)

    if not current_version:
        click.secho(
            "\nYou have not yet ran a migration. Catch your database up with "
            "./run.py migrate",
            fg="magenta",
            bold=True,
        )


def get_current_version():
    current_path = DB_PATH
    if not path.isfile(current_path):
        if path.isfile(OLD_DB_PATH):
            current_path = OLD_DB_PATH
        else:
            return 0
    with sqlite3.connect(current_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT MAX(id) from version")
        except sqlite3.OperationalError:
            return 0
        return cursor.fetchone()[0]


def check_if_migration_is_needed():
    current_version = get_current_version()
    most_recent_mig = sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql"))[-1:][0]
    if path.exists(OLD_DB_PATH):
        click.secho(
            f"The database needs to be moved to the new directory ({DB_PATH}). Please run `salmon migrate`.\n",
            fg="red",
            bold=True,
        )
    try:
        mig_version = int(most_recent_mig[:4])
    except TypeError:
        click.secho(
            f"\n{most_recent_mig} is improperly named. It must start with a "
            "four digit integer.",
            fg="red",
        )
        raise click.Abort from None
    if mig_version > current_version:
        click.secho(
            "The database needs updating. Please run `salmon migrate`.\n",
            fg="red",
            bold=True,
        )


check_if_migration_is_needed()
