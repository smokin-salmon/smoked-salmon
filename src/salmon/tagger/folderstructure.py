import os
import re
import shutil
from pathlib import Path

import asyncclick as click

from salmon import cfg, interaction
from salmon.checks.tag_rules import in_torrent_path
from salmon.common import clean_name, has_visible_chars, invisible_chars, show_invisible
from salmon.constants import (
    ALLOWED_EXTENSIONS,
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
    ESSENTIAL_EXTENSIONS,
)
from salmon.errors import NoncompliantFolderStructure
from salmon.tagger.renames import RenameStopped, name_key, rename_recorded

# Used when no tracker is known yet (e.g. `salmon tag`), and as the fallback for trackers whose
# own limit has not been set.
DEFAULT_MAX_PATH_LENGTH = 180


async def check_folder_structure(
    path: str, scene: bool, *, essential_only: bool = False, max_path_length: int = DEFAULT_MAX_PATH_LENGTH
) -> None:
    """Run through every filesystem check that causes uploads to violate the rules
    or be rejected on the upload form.

    Verifies that no name holds invisible or odd Unicode characters or a character a tracker
    refuses (renamed, or reported for a scene release), that path lengths are within
    max_path_length characters, that there are no zero length folders, and that the file
    extensions are valid. Loops until the user has fixed all issues or the upload is aborted.

    Args:
        path: Absolute path to the release folder being checked.
        scene: Whether the release is a scene release. Scene releases are not
            automatically fixed and require manual intervention.
        essential_only: If True, only essential extensions are allowed;
            files like nfo, sfv, md5, txt, etc. are flagged for removal.
        max_path_length: The longest in-torrent path (folder, sub-folders and file name)
            this check allows. Defaults to the value used before this was per-tracker.

    Raises:
        click.Abort: If the user aborts, or if a scene release has structural issues.
    """
    while True:
        click.secho("\nChecking folder structure...", fg="cyan", bold=True)
        try:
            await _check_illegal_folders(path)
            await _check_invisible_characters(path, scene)
            await _check_forbidden_characters(path, scene)
            _check_path_lengths(path, scene, max_path_length)
            _check_zero_len_folder(path)
            await _check_extensions(path, scene, essential_only=essential_only)
            return
        except NoncompliantFolderStructure:
            if scene:
                click.secho(
                    "The folder structure is not compliant with the upload rules. "
                    "As this is a scene release, you need to manually descene it before upload.",
                    fg="red",
                    bold=True,
                )
                raise click.Abort() from None
            await interaction.confirm(
                click.style(
                    "You need to manually fix the issues present in the upload's folder? "
                    "Send a [Y] once you have done so, or a [N] to abort.",
                    fg="magenta",
                    bold=True,
                ),
                default=False,
                abort=True,
            )


async def _check_illegal_folders(path: str) -> None:
    """Verify that no illegal folders exist within the release directory.

    Currently checks for Synology NAS metadata directories (``@eaDir``).
    Prompts the user to delete, abort, or continue for each offending folder.

    Args:
        path: Absolute path to the release folder being checked.

    Raises:
        click.Abort: If the user chooses to abort.
    """
    for root, dirs, _files in os.walk(path, topdown=False):
        for dirname in dirs:
            if dirname == "@eaDir":
                target_dir = os.path.join(root, dirname)
                while True:
                    resp = (
                        await interaction.prompt(
                            f"Dirname {target_dir} is illegal. [D]elete, [A]bort, or [C]ontinue?",
                            default="D",
                        )
                    ).lower()
                    if resp[0] == "d":
                        shutil.rmtree(target_dir)
                        break
                    elif resp[0] == "a":
                        raise click.Abort
                    elif resp[0] == "c":
                        break


async def _check_invisible_characters(path: str, scene: bool) -> None:
    """Verify that no folder or file name holds an invisible or odd Unicode character.

    Unicode spaces such as the no-break space, zero-width characters and other format or control
    characters make a tracker refuse the torrent. Offending sub-folders and files are renamed to
    their cleaned names after the user agrees, sub-folders last so the paths stay valid while the
    renaming goes on. A scene release is never renamed: it raises.

    The release folder itself is renamed by ``rename_folder``, which cleans the names it builds, and
    is not renamed here, as the caller keeps its path: a release folder that still holds such a
    character (the user kept the old name) makes the upload abort.

    Args:
        path: Absolute path to the release folder being checked.
        scene: Whether the release is a scene release.

    Raises:
        NoncompliantFolderStructure: If a scene release has such names, the user declines the
            rename, a rename would replace a path that exists, or nothing would be left of a name.
        click.Abort: If the release folder's own name holds such a character.
    """
    folder_name = os.path.basename(os.path.normpath(path))
    offending: list[tuple[str, str, list[str]]] = []
    for root, dirs, files in os.walk(path, topdown=False):
        for name in [*files, *dirs]:
            if chars := invisible_chars(name):
                offending.append(
                    (os.path.join(root, name), os.path.join(folder_name, os.path.relpath(root, path), name), chars)
                )
    root_chars = invisible_chars(folder_name)

    if not offending and not root_chars:
        return click.secho("No names hold invisible Unicode characters.", fg="green")

    click.secho("The following names hold invisible or unusual Unicode characters:", fg="red", bold=True)
    listing = [(shown, chars) for _, shown, chars in offending]
    if root_chars:
        listing.append((folder_name, root_chars))
    for shown, chars in listing:
        click.echo(f" >> {show_invisible(os.path.normpath(shown))}")
        for char in chars:
            click.echo(f"      {char}")
    if scene:
        raise NoncompliantFolderStructure
    if root_chars:
        click.secho(
            "The release folder's own name cannot be renamed here. Run again and accept the proposed folder name.",
            fg="red",
            bold=True,
        )
        raise click.Abort

    renames = [(old, os.path.join(os.path.dirname(old), clean_name(os.path.basename(old)))) for old, _, _ in offending]
    taken: set[str] = set()
    for old, new in renames:
        if new == old:
            click.secho(f"Cannot rename {old}: nothing would be left of its name.", fg="red", bold=True)
            raise NoncompliantFolderStructure
        if os.path.lexists(new) or new in taken:
            click.secho(f"Cannot rename {old}: {new} already exists.", fg="red", bold=True)
            raise NoncompliantFolderStructure
        taken.add(new)
    if not await interaction.confirm(
        click.style("Rename them to the names without those characters?", fg="magenta"), default=True
    ):
        raise NoncompliantFolderStructure
    _rename_or_stop(path, renames)


def _forbidden_characters(name: str) -> list[str]:
    """The characters in *name* that a tracker refuses in a torrent's paths, each once."""
    return sorted(set(re.findall(BLACKLISTED_CHARS, name)))


def _substitute_forbidden_characters(name: str) -> str:
    """Replace the forbidden characters in *name* the same way generated folder and file names are."""
    if cfg.upload.formatting.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            name = name.replace(char, sub)
    return re.sub(BLACKLISTED_CHARS, cfg.upload.formatting.blacklisted_substitution, name)


async def _check_forbidden_characters(path: str, scene: bool) -> None:
    """Verify that no file or sub-folder name holds a character a tracker refuses.

    ``BLACKLISTED_CHARS`` is applied to the names salmon builds, but not to the names it only moves:
    a log, cue or image the user named, a sub-folder, or an audio file whose rename was declined.
    The tracker then refuses the torrent at the upload form, after the spectrals have been uploaded.
    Offending names are renamed to the same substitution generated names get, sub-folders last so the
    paths stay valid while the renaming goes on. A scene release is never renamed: it raises.

    The release folder itself is renamed by ``rename_folder``, which cleans the names it builds, and
    is not renamed here, as the caller keeps its path: a release folder that still holds such a
    character (the user kept the old name) makes the upload abort.

    Args:
        path: Absolute path to the release folder being checked.
        scene: Whether the release is a scene release.

    Raises:
        NoncompliantFolderStructure: If a scene release has such names, the user declines the
            rename, a rename would replace a path that exists, or nothing would be left of a name.
        click.Abort: If the release folder's own name holds such a character.
    """
    folder_name = os.path.basename(os.path.normpath(path))
    offending: list[tuple[str, str, list[str]]] = []
    for root, dirs, files in os.walk(path, topdown=False):
        for name in [*files, *dirs]:
            if chars := _forbidden_characters(name):
                offending.append(
                    (os.path.join(root, name), os.path.join(folder_name, os.path.relpath(root, path), name), chars)
                )
    root_chars = _forbidden_characters(folder_name)

    if not offending and not root_chars:
        return click.secho("No names hold characters a tracker refuses.", fg="green")

    click.secho("The following names hold characters a tracker refuses:", fg="red", bold=True)
    listing = [(shown, chars) for _, shown, chars in offending]
    if root_chars:
        listing.append((folder_name, root_chars))
    for shown, chars in listing:
        click.echo(f" >> {os.path.normpath(shown)}")
        for char in chars:
            click.echo(f"      {char}")
    if scene:
        raise NoncompliantFolderStructure
    if root_chars:
        click.secho(
            "The release folder's own name cannot be renamed here. Run again and accept the proposed folder name.",
            fg="red",
            bold=True,
        )
        raise click.Abort

    renames = [
        (old, os.path.join(os.path.dirname(old), _substitute_forbidden_characters(os.path.basename(old))))
        for old, _, _ in offending
    ]
    taken: set[str] = set()
    for old, new in renames:
        if not has_visible_chars(os.path.basename(new)):
            click.secho(f"Cannot rename {old}: nothing visible would be left of its name.", fg="red", bold=True)
            raise NoncompliantFolderStructure
        if os.path.lexists(new) or new in taken:
            click.secho(f"Cannot rename {old}: {new} already exists.", fg="red", bold=True)
            raise NoncompliantFolderStructure
        taken.add(new)
    if not await interaction.confirm(
        click.style("Rename them to the names without those characters?", fg="magenta"), default=True
    ):
        raise NoncompliantFolderStructure
    _rename_or_stop(path, renames)


def _check_path_lengths(path: str, scene: bool, max_path_length: int) -> None:
    """Verify that all file and folder paths are no longer than max_path_length characters.

    Paths are measured the same way the file will sit inside the torrent: the release folder
    name, any sub-folders, and the file name. Files between max_path_length and
    max_path_length + 70 characters are automatically truncated. Longer than that, they cannot
    be safely truncated and raise immediately.

    Args:
        path: Absolute path to the release folder being checked.
        scene: Whether the release is a scene release. Scene releases are never
            auto-truncated; any offending path raises instead.
        max_path_length: The longest in-torrent path this check allows.

    Raises:
        NoncompliantFolderStructure: If any path exceeds max_path_length characters
            and cannot be (or should not be) automatically fixed.
    """
    offending_files, really_offending_files = [], []
    folder_name = os.path.basename(os.path.normpath(path))
    really_offending_cutoff = max_path_length + 70
    for root, _, files in os.walk(path):
        subfolder_len = len(in_torrent_path(folder_name, os.path.relpath(root, path)))
        if subfolder_len > max_path_length:
            click.secho(f"A subfolder has a path length >{max_path_length} characters.", fg="red")
            raise NoncompliantFolderStructure
        for f in files:
            filepath = os.path.join(root, f)
            filepathlen = len(in_torrent_path(folder_name, os.path.relpath(filepath, path)))
            if filepathlen > max_path_length:
                if filepathlen < really_offending_cutoff:
                    offending_files.append(filepath)
                else:
                    really_offending_files.append(filepath)

    if scene and (offending_files or really_offending_files):
        click.secho(f"The following files exceed {max_path_length} characters in length.", fg="red", bold=True)
        for f in offending_files + really_offending_files:
            click.echo(f" >> {f}")
        raise NoncompliantFolderStructure

    if really_offending_files:
        click.secho(
            f"The following files exceed {max_path_length} characters in length, but cannot "
            "be safely truncated (more than 70 characters above the limit):",
            fg="red",
            bold=True,
        )
        for f in really_offending_files:
            click.echo(f" >> {f}")
        raise NoncompliantFolderStructure

    if not offending_files:
        return click.secho(f"No paths exceed {max_path_length} characters in length.", fg="green")

    click.secho(f"The following exceed {max_path_length} characters in length, truncating...", fg="red")
    renames = []
    key = name_key(offending_files)
    planned = set()
    for filepath in sorted(offending_files):
        directory, base = os.path.split(filepath)
        name, ext = os.path.splitext(base)
        relative_len = len(in_torrent_path(folder_name, os.path.relpath(filepath, path)))
        # Shorten the name so the in-torrent path lands exactly on max_path_length, the ".."
        # marker included.
        excess = relative_len - max_path_length + 2
        kept = len(name) - excess
        newpath = os.path.join(directory, name[: max(kept, 0)] + ".." + ext)
        # Two names cut to the same start, or a file already there with it: the second becomes "Name~2.."
        counter = 1
        while os.path.lexists(newpath) or key(newpath) in planned:
            counter += 1
            mark = f"~{counter}"
            if kept - len(mark) < 1:
                click.secho(
                    f"{filepath} cannot be shortened to {max_path_length} characters under a name no other file has.",
                    fg="red",
                    bold=True,
                )
                raise NoncompliantFolderStructure
            newpath = os.path.join(directory, name[: kept - len(mark)] + mark + ".." + ext)
        new_relative_len = len(in_torrent_path(folder_name, os.path.relpath(newpath, path)))
        # At least one character of the name stays: "...flac" alone has no extension, and the track is lost.
        if kept < 1 or new_relative_len > max_path_length:
            click.secho(
                f"{filepath} cannot be shortened to {max_path_length} characters: its "
                "folder path leaves no room for its name.",
                fg="red",
                bold=True,
            )
            raise NoncompliantFolderStructure
        planned.add(key(newpath))
        renames.append((filepath, newpath))
    _rename_or_stop(path, renames)


def _rename_or_stop(path: str, renames: list[tuple[str, str]]) -> None:
    """Make the renames the checks planned, stopping at the first that fails with what was renamed and what was not.

    Raises:
        NoncompliantFolderStructure: A rename failed, or its new name was taken since the check.
    """
    try:
        rename_recorded(path, renames)
    except RenameStopped as e:
        click.secho(e.report(path), fg="red", bold=True)
        raise NoncompliantFolderStructure from e
    for _, new in renames:
        click.echo(f" >> {new}")


def _check_zero_len_folder(path: str) -> None:
    """Verify that no zero-length (empty-name) folder segments exist in any path.

    Uses ``pathlib`` to iterate through every entry under *path* and checks
    whether any directory or file has an empty (or whitespace-only) name,
    which would indicate a malformed folder structure.

    Args:
        path: Absolute path to the release folder being checked.

    Raises:
        NoncompliantFolderStructure: If a zero-length folder segment is found.
    """
    for item in Path(path).rglob("*"):
        if not item.name.strip():
            click.secho("A zero length folder exists in this directory.", fg="red")
            raise NoncompliantFolderStructure
    click.secho("No zero length folders were found.", fg="green")


async def _check_extensions(path: str, scene: bool, *, essential_only: bool = False) -> None:
    """Validate that all file extensions within the release folder are permitted.

    Files with disallowed extensions are either handled interactively (non-scene)
    or collected and reported as a group before raising (scene). Also warns when
    multiple audio codecs are present in the same folder.

    Args:
        path: Absolute path to the release folder being checked.
        scene: Whether the release is a scene release. Scene releases report all
            offending files at once rather than prompting per-file.
        essential_only: If True, only essential extensions are allowed.

    Raises:
        NoncompliantFolderStructure: If scene release contains files with invalid
            extensions.
        click.Abort: If the user aborts during interactive extension handling.
    """
    mp3, aac, flac = [], [], []
    offending_files = []  # Collect offending files for scene releases
    effective_extensions = ESSENTIAL_EXTENSIONS if essential_only else ALLOWED_EXTENSIONS
    for root, _, files in os.walk(path):
        for fln in files:
            _, ext = os.path.splitext(fln.lower())
            if ext == ".mp3":
                mp3.append(fln)
            elif ext == ".flac":
                flac.append(fln)
            elif ext == ".m4a":
                aac.append(fln)
            elif ext not in effective_extensions:
                if scene:
                    offending_files.append(os.path.join(root, fln))
                else:
                    await _handle_bad_extension(os.path.join(root, fln))

    if scene and offending_files:
        click.secho("The following files have invalid extensions:", fg="red", bold=True)
        for filepath in offending_files:
            click.echo(f" >> {filepath}")
        raise NoncompliantFolderStructure

    if len([li for li in [mp3, flac, aac] if li]) > 1:
        await _handle_multiple_audio_exts()
    else:
        click.secho("File extensions have been validated.", fg="green")


async def _handle_bad_extension(filepath: str) -> None:
    """Interactively handle a file with a disallowed extension.

    Prompts the user to delete the file, abort the upload, or continue without
    taking action.

    Args:
        filepath: Absolute path to the offending file.

    Raises:
        click.Abort: If the user chooses to abort.
    """
    while True:
        resp = (
            await interaction.prompt(
                f"{filepath} does not have an approved file extension. [D]elete, [a]bort, or [c]ontinue?",
                default="D",
            )
        ).lower()
        if resp[0].lower() == "d":
            return os.remove(filepath)
        elif resp[0].lower() == "a":
            raise click.Abort
        elif resp[0].lower() == "c":
            return


async def _handle_multiple_audio_exts() -> None:
    """Interactively handle the presence of multiple audio codec types in one folder.

    Prompts the user to abort or continue. Having mixed codecs (e.g. both FLAC
    and MP3 files) in a single release folder is generally against upload rules.

    Raises:
        click.Abort: If the user chooses to abort.
    """
    while True:
        resp = (
            await interaction.prompt(
                "There are multiple audio codecs in this folder. [A]bort or [c]ontinue?",
                default="A",
            )
        ).lower()
        if resp[0] == "a":
            raise click.Abort
        if resp[0] == "c":
            return
