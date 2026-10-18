import os
import shutil
import time

import asyncclick as click

import salmon.commands
import salmon.converter
import salmon.cross_upload
import salmon.images
import salmon.search
import salmon.tagger
import salmon.uploader
import salmon.webui
from salmon import cfg
from salmon.common import commandgroup, tmp_lock
from salmon.errors import FilterError, LoginError, TLSCertificateError, UploadError
from salmon.release_notification import show_release_notification

# Without a lock, clean_tmp_dir keeps what changed more recently than this: a running salmon may own it.
STALE_SECONDS = 24 * 60 * 60


def _remove(item_path: str) -> bool:
    """Remove a file, link or folder; False, said, when it fails."""
    try:
        if os.path.isfile(item_path) or os.path.islink(item_path):
            os.unlink(item_path)
        elif os.path.isdir(item_path):
            shutil.rmtree(item_path)
        return True
    except Exception as e:
        click.secho(f"Failed to remove {item_path}: {e}", fg="yellow")
        return False


def cleanup_tmp_dir():
    """Lock tmp_dir for this salmon's life and, if configured, clean what no running salmon is using."""
    tmp_dir = cfg.directory.tmp_dir
    if not tmp_dir:
        return
    clean = cfg.directory.clean_tmp_dir
    claim = tmp_lock.claim(tmp_dir, exclusive=clean)
    if not clean:
        return
    if claim is tmp_lock.Claim.SHARED:
        click.secho("Another salmon is running: not cleaning tmp_dir.", fg="yellow")
        return
    if claim is tmp_lock.Claim.UNLOCKED:
        click.secho(
            "Cannot lock tmp_dir to see whether another salmon is running: cleaning only what has not changed in"
            " the last 24 hours.",
            fg="yellow",
        )
    cutoff = time.time() - STALE_SECONDS
    failed = 0
    try:
        for item in os.listdir(tmp_dir):
            if item == tmp_lock.LOCK_NAME:
                continue
            item_path = os.path.join(tmp_dir, item)
            if claim is tmp_lock.Claim.UNLOCKED:
                try:
                    if tmp_lock.newest_change(item_path) > cutoff:
                        continue
                except OSError as e:
                    click.secho(f"Failed to remove {item_path}: {e}", fg="yellow")
                    failed += 1
                    continue
            if not _remove(item_path):
                failed += 1
        if failed:
            entries = "entry" if failed == 1 else "entries"
            click.secho(f"Cleaned temporary directory partly: {failed} {entries} left in {tmp_dir}", fg="yellow")
        else:
            click.secho(f"Cleaned temporary directory: {tmp_dir}", fg="green")
    except Exception as e:
        click.secho(f"Failed to clean temporary directory: {e}", fg="yellow")
    finally:
        if claim is tmp_lock.Claim.ALONE and not tmp_lock.share():
            click.secho("Lost the lock on tmp_dir: another salmon may clean it while this one runs.", fg="yellow")


def main():
    try:
        cleanup_tmp_dir()
        show_release_notification()
        click.echo()

        commandgroup(obj={})
    except (UploadError, FilterError) as e:
        click.secho(f"There was an error: {e}", fg="red", bold=True)
    except TLSCertificateError as e:
        # Where an upload ends when its first request to the tracker fails on it (the dupe check).
        click.secho(f"There was an error: {e}", fg="red", bold=True)
        click.secho(
            f"Your session cookie and API key are not the cause. Run salmon checkconf -t {e.tracker} to see where"
            " Python gets its CA certificates.",
            fg="yellow",
        )
    except ImportError as e:
        click.secho(f"You are missing required dependencies: {e}", fg="red")


if __name__ == "__main__":
    main()
