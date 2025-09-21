import os
import re
import shutil
from copy import copy
from string import Formatter

import click

from salmon import cfg
from salmon.common import strip_template_keys
from salmon.constants import (
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
)
from salmon.errors import UploadError


def rename_folder(path, metadata, auto_rename, check=True):
    """
    Create a revised folder name from the new metadata and present it to the
    user. Have them decide whether or not to accept the folder name.
    Then offer them the ability to edit the folder name in a text editor
    before the renaming occurs.
    For scene releases, the name of the original folder is kept untouched, and
    the folder is copied to the download folder.
    """
    old_base = os.path.basename(path)
    new_base = generate_folder_name(metadata)
    if metadata["scene"]:
        new_base = old_base
        auto_rename = True

    if check and old_base != new_base:
        click.secho("\nRenaming folder...", fg="cyan", bold=True)
        click.echo(f"Old folder name        : {old_base}")
        click.echo(f"New pending folder name: {new_base}")

        user_rename_choice = click.confirm(
            click.style("\nWould you like to replace the original folder name?", fg="magenta"), default=True
        )

        new_base = _edit_folder_interactive(new_base, auto_rename) if auto_rename or user_rename_choice else old_base

    new_path = os.path.join(cfg.directory.download_directory, new_base)
    if os.path.isdir(new_path) and not os.path.samefile(path, new_path):
        if not check or click.confirm(
            click.style(
                f"A folder already exists with the new folder name '{new_path}', would you like to replace it?",
                fg="magenta",
                bold=True,
            ),
            default=True,
        ):
            shutil.rmtree(new_path)
        else:
            raise UploadError("New folder name already exists.")
    new_path_dirname = os.path.dirname(new_path)
    if not os.path.exists(new_path_dirname):
        os.makedirs(new_path_dirname)

    # Check if hardlinks can be used
    same_volume = os.stat(path).st_dev == os.stat(cfg.directory.download_directory).st_dev
    use_hardlinks = same_volume and cfg.directory.hardlinks

    if os.path.exists(path) and os.path.exists(new_path) and os.path.samefile(path, new_path):
        click.secho(f"Skipping copy, same location already for '{new_path}'", fg="yellow")
    else:
        if use_hardlinks:
            try:
                shutil.copytree(path, new_path, copy_function=os.link, dirs_exist_ok=True)
                click.secho(f"Hardlinked folder to '{new_path}'.", fg="yellow")
            except shutil.Error as _:
                click.secho("Hardlinking didn't work, falling back to non-hardlink copy...", fg="red")
                shutil.copytree(path, new_path, dirs_exist_ok=True)
                click.secho(f"Copied folder to '{new_path}'.", fg="yellow")
        else:
            shutil.copytree(path, new_path, dirs_exist_ok=True)
            click.secho(f"Copied folder to '{new_path}'.", fg="yellow")

        if cfg.upload.formatting.remove_source_dir:
            shutil.rmtree(path)

    # Also rename spectrals folder in TMP_DIR if it exists
    if cfg.directory.tmp_dir and os.path.exists(cfg.directory.tmp_dir):
        tmp_old_specs_path = os.path.join(cfg.directory.tmp_dir, f"spectrals_{old_base}")
        tmp_new_specs_path = os.path.join(cfg.directory.tmp_dir, f"spectrals_{new_base}")

        if (
            os.path.exists(tmp_old_specs_path)
            and os.path.exists(tmp_new_specs_path)
            and os.path.samefile(tmp_old_specs_path, tmp_new_specs_path)
        ):
            click.secho(f"Skipping copy, same location already for '{tmp_new_specs_path}'", fg="yellow")
        else:
            if use_hardlinks:
                try:
                    shutil.copytree(tmp_old_specs_path, tmp_new_specs_path, copy_function=os.link, dirs_exist_ok=True)
                    click.secho(f"Hardlinked temporary spectrals folder to '{tmp_new_specs_path}'.", fg="yellow")
                except shutil.Error as _:
                    click.secho("Hardlinking didn't work, falling back to non-hardlink copy...", fg="red")
                    shutil.copytree(tmp_old_specs_path, tmp_new_specs_path, dirs_exist_ok=True)
                    click.secho(f"Copied temporary spectrals folder to '{tmp_new_specs_path}'.", fg="yellow")
            else:
                shutil.copytree(tmp_old_specs_path, tmp_new_specs_path, dirs_exist_ok=True)
                click.secho(f"Copied temporary spectrals folder to '{tmp_new_specs_path}'.", fg="yellow")

            if cfg.upload.formatting.remove_source_dir:
                shutil.rmtree(tmp_old_specs_path)

    return new_path


def generate_folder_name(metadata):
    """
    Fill in the values from the folder template using the metadata, then strip
    away the unnecessary keys.
    """
    metadata = {**metadata, **{"artists": _compile_artist_str(metadata["artists"])}}
    template = cfg.upload.formatting.folder_template
    keys = [fn for _, fn, _, _ in Formatter().parse(template) if fn]
    for k in keys.copy():
        if not metadata.get(k):
            template = strip_template_keys(template, k)
            keys.remove(k)
    sub_metadata = _fix_format(metadata, keys)
    return template.format(**{k: _sub_illegal_characters(sub_metadata[k]) for k in keys})


def _compile_artist_str(artist_data):
    """Create a string to represent the main artists of the release."""
    artists = [a[0] for a in artist_data if a[1] == "main"]
    if len(artists) > cfg.upload.formatting.various_artist_threshold:
        return cfg.upload.formatting.various_artist_word
    c = ", " if len(artists) > 2 or "&" in "".join(artists) else " & "
    return c.join(sorted(artists))


def _sub_illegal_characters(stri):
    if cfg.upload.description.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            stri = str(stri).replace(char, sub)
    return re.sub(BLACKLISTED_CHARS, cfg.upload.formatting.blacklisted_substitution, str(stri))


def _fix_format(metadata, keys):
    """
    Add abbreviated encoding to format key when the format is not 'FLAC'.
    Helpful for 24 bit FLAC and MP3 320/V0 stuff.

    So far only 24 bit FLAC is supported, when I fix the script for MP3 i will add MP3 encodings.
    """
    sub_metadata = copy(metadata)
    if "format" in keys:
        if metadata["format"] == "FLAC" and metadata["encoding"] == "24bit Lossless":
            sub_metadata["format"] = "24bit FLAC"
        elif metadata["format"] == "MP3":
            enc = re.sub(r" \(VBR\)", "", str(metadata["encoding"]))
            sub_metadata["format"] = f"MP3 {enc}"
            if metadata["encoding_vbr"]:
                sub_metadata["format"] += " (VBR)"
        elif metadata["format"] == "AAC":
            enc = re.sub(r" \(VBR\)", "", metadata["encoding"])
            sub_metadata["format"] = f"AAC {enc}"
            if metadata["encoding_vbr"]:
                sub_metadata["format"] += " (VBR)"
    return sub_metadata


def _edit_folder_interactive(foldername, auto_rename):
    """Allow the user to edit the pending folder name in a text editor."""
    if auto_rename:
        return foldername
    if not click.confirm(
        click.style("Is the new folder name acceptable? ([n] to edit)", fg="magenta"),
        default=True,
    ):
        newname = click.edit(foldername, editor=cfg.upload.default_editor)
        while True:
            if newname is None:
                return foldername
            elif re.search(BLACKLISTED_CHARS, newname):
                if not click.confirm(
                    click.style(
                        "Folder name contains invalid characters, retry?",
                        fg="magenta",
                        bold=True,
                    ),
                    default=True,
                ):
                    exit()
            else:
                return newname.strip().replace("\n", "")
            newname = click.edit(foldername, editor=cfg.upload.default_editor)
    return foldername
