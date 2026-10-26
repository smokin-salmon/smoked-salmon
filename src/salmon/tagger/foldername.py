import os
import re
import shutil
import sys
from copy import copy
from string import Formatter

import asyncclick as click

from salmon import cfg, dryrun, interaction
from salmon.common import clean_name, has_visible_chars, strip_template_keys
from salmon.common.files import shares_files
from salmon.constants import (
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
)
from salmon.errors import UploadError
from salmon.tagger.audio_info import gather_audio_info


async def rename_folder(path, metadata, auto_rename, check=True, parent=None, original=None):
    """
    Create a revised folder name from the new metadata and present it to the
    user. Have them decide whether or not to accept the folder name.
    Then offer them the ability to edit the folder name in a text editor
    before the renaming occurs.
    For scene releases, the name of the original folder is kept untouched, and
    the folder is copied to the download folder.
    `parent` replaces the download folder as the directory the renamed folder goes into.
    `original` is the folder the user gave, passed only when `path` is a copy of it: the copy is left for its run
    directory to remove. The source folder is never removed here: see remove_source_folder.
    """
    # Imported here: the uploader package imports this module.
    from salmon.uploader.spectrals import get_spectrals_path, made_by_salmon

    old_base = os.path.basename(path)
    template_fields = {fn for _, fn, _, _ in Formatter().parse(cfg.upload.formatting.folder_template) if fn}
    if "resolution" in template_fields:
        metadata = {**metadata, "resolution": _resolution(path)}
    new_base = generate_folder_name(metadata)
    if metadata["scene"]:
        new_base = old_base
        auto_rename = True

    if check and old_base != new_base:
        click.secho("\nRenaming folder...", fg="cyan", bold=True)
        click.echo(f"Old folder name        : {old_base}")
        click.echo(f"New pending folder name: {new_base}")

        user_rename_choice = await interaction.assume_defaults() or await interaction.confirm(
            click.style("\nWould you like to replace the original folder name?", fg="magenta"), default=True
        )

        new_base = (
            await _edit_folder_interactive(new_base, auto_rename) if auto_rename or user_rename_choice else old_base
        )

    if not _is_folder_name(new_base):
        raise UploadError(f"Invalid folder name: {new_base!r}")

    new_path = os.path.join(parent or cfg.directory.download_directory, new_base)
    same_location = os.path.isdir(new_path) and os.path.samefile(path, new_path)
    # Checked whether or not new_path exists yet: a symlinked folder on the way can lead into a library.
    if not same_location and cfg.directory.protects(new_path):
        raise UploadError(f"Not renaming into {new_path}: it is in library_dirs, or holds one.")
    if os.path.isdir(new_path) and not same_location:
        # The album this run's copy was made from, already under the new name: replacing it would delete it.
        if original is not None and os.path.samefile(new_path, original):
            raise UploadError(
                f"Not replacing {new_path}: it is the album this run works on a copy of. "
                "Rename it, or give this album another name. "
                "If it is already uploaded, salmon cross-upload sends it to another tracker."
            )
        if not check or await interaction.confirm(
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
    # A hardlink shares the inode, so a later tag write on the new folder would reach a library album.
    in_library = cfg.directory.protects(path)
    use_hardlinks = same_volume and cfg.directory.hardlinks and not in_library

    # The spectrals salmon made before this rename are looked up by the new name afterwards: they move with the album,
    # wherever they are and whatever remove_source_dir says. A Spectrals folder salmon did not make is the album's own,
    # copied like any other folder (get_spectrals_path never names it).
    specs_path = get_spectrals_path(path)
    carry_specs = made_by_salmon(specs_path)
    in_source = carry_specs and _is_direct_child(specs_path, path)
    ignore = _ignoring_top_level(path, os.path.basename(specs_path)) if in_source else None

    same_folder = os.path.exists(path) and os.path.exists(new_path) and os.path.samefile(path, new_path)
    if same_folder:
        click.secho(f"Skipping copy, same location already for '{new_path}'", fg="yellow")
    else:
        if use_hardlinks:
            try:
                shutil.copytree(path, new_path, copy_function=os.link, dirs_exist_ok=True, ignore=ignore)
                click.secho(f"Hardlinked folder to '{new_path}'.", fg="yellow")
            except shutil.Error as _:
                click.secho("Hardlinking didn't work, falling back to non-hardlink copy...", fg="red")
                # A partially hardlinked tree makes the plain copy raise SameFileError (#356)
                shutil.rmtree(new_path, ignore_errors=True)
                shutil.copytree(path, new_path, dirs_exist_ok=True, ignore=ignore)
                click.secho(f"Copied folder to '{new_path}'.", fg="yellow")
        else:
            shutil.copytree(path, new_path, dirs_exist_ok=True, ignore=ignore)
            click.secho(f"Copied folder to '{new_path}'.", fg="yellow")

    if carry_specs:
        # Moved, not copied: the source must not keep a spectrals folder the upload never deletes. Also into the same
        # folder: a case-only rename still renames salmon's folder outside it.
        _move_specs_folder(specs_path, get_spectrals_path(new_path))

    # Local: the converter package imports this module.
    from salmon.converter.conversions import carry_conversion

    carry_conversion(path, new_path)
    return new_path


def _is_direct_child(child, parent):
    """Whether `child` is an entry directly inside `parent`."""
    return os.path.dirname(os.path.abspath(child)) == os.path.abspath(parent)


def _ignoring_top_level(top, name):
    """A copytree `ignore` that skips the entry `name` of `top` only, not one of that name further down."""
    top = os.path.abspath(top)

    def ignore(directory, names):
        return {name} & set(names) if os.path.abspath(directory) == top else set()

    return ignore


def _move_specs_folder(src, dst):
    """Move salmon's spectrals folder to `dst`, replacing salmon's own stale one there. Works across volumes.

    Raises:
        UploadError: Something salmon did not make is at `dst`.
    """
    # Imported here: the uploader package imports this module.
    from salmon.uploader.spectrals import carry_specs_claim, make_way_for_spectrals

    if os.path.exists(dst) and os.path.samefile(src, dst):
        return  # The album was renamed into itself, and its spectrals folder keeps its name.
    old_real = os.path.realpath(src)
    make_way_for_spectrals(dst)
    shutil.move(src, dst)
    carry_specs_claim(old_real, dst)


def _resolution(path):
    """
    "<bits>-<kHz>"-style bit depth and sample rate for the folder's tracks, e.g. "24-96" or
    "24-44.1". Blank for lossy files, for plain 16-bit/44.1kHz, for a missing or zero bit depth,
    and when the tracks do not all share one bit depth and sample rate: a folder name must not
    claim a single resolution for a hybrid release.
    """
    return resolution_token(gather_audio_info(path))


def resolution_token(audio_info):
    """
    Same as `_resolution`, from an already-gathered `gather_audio_info` mapping. The converters
    use this to work out the exact token (if any) a source folder's name would carry, so they can
    drop or replace it without guessing at a pattern that could also match unrelated text.
    """
    bits = {info["precision"] for info in audio_info.values()}
    rates = {info["sample rate"] for info in audio_info.values()}
    if len(bits) != 1 or len(rates) != 1:
        return ""
    (bit_depth,), (sample_rate,) = bits, rates
    if not bit_depth or not sample_rate or (bit_depth == 16 and sample_rate == 44100):
        return ""
    return f"{bit_depth}-{sample_rate / 1000:g}"


def _template_has_resolution() -> bool:
    """Whether the folder template places {resolution} (with or without a format spec) in the name."""
    template = cfg.upload.formatting.folder_template
    return any(field == "resolution" for _, field, _, _ in Formatter().parse(template))


def _bracketed(foldername, position) -> bool:
    """Whether position is inside a [...], (...) or {...} group of the name."""
    depth = 0
    for char in foldername[:position]:
        if char in "[({":
            depth += 1
        elif char in "])}":
            depth = max(depth - 1, 0)
    return depth > 0


def _token_span(foldername, token):
    """Where the name's last resolution token is, or None.

    It must stand alone ("1924-48" in a title holds "24-48" but not as a token) and sit in a bracket group, unless
    the folder template places {resolution} itself: a bare "24-96" in a title is then the title's.
    """
    if not token:
        return None
    matches = list(re.finditer(r"(?<![\w.-])" + re.escape(token) + r"(?![\w.-])", foldername))
    if not _template_has_resolution():
        matches = [m for m in matches if _bracketed(foldername, m.start())]
    return matches[-1].span() if matches else None


def holds_resolution_token(foldername, token) -> bool:
    """Whether the folder name carries token as its own word."""
    return _token_span(foldername, token) is not None


def swap_resolution_token(foldername, token, new):
    """Replace the name's standalone resolution token with new; a name without one is left as it is."""
    span = _token_span(foldername, token)
    return foldername if span is None else foldername[: span[0]] + new + foldername[span[1] :]


def drop_resolution_token(foldername, token):
    """Remove the name's standalone resolution token, one space beside it, and a bracket pair it leaves empty."""
    span = _token_span(foldername, token)
    if span is None:
        return foldername
    start, end = span
    if start > 0 and foldername[start - 1] == " ":
        start -= 1
    elif end < len(foldername) and foldername[end] == " ":
        end += 1
    name = foldername[:start] + foldername[end:]
    if 0 < start < len(name) and name[start - 1] in "[({" and name[start] in "])}":
        name = name[: start - 1].rstrip(" ") + name[start + 1 :]
    return name.strip()


async def remove_source_folder(
    source: str, final: str | None, original: str | None = None, uploaded: bool = True, aborted: bool = False
) -> None:
    """With remove_source_dir on, remove the folder the run started from, once the run is done with it.

    Called when a run ends, never at the folder rename: a run stopped after the rename (an abort at a later
    question, a failed upload) leaves the source folder where it was, and running salmon on it again works.
    The user is asked first, defaulting to yes; a run that assumes defaults removes it without asking. After an
    abort that followed an upload the question defaults to no, and a run that assumes defaults keeps the folder.

    Args:
        source: The folder the run started from.
        final: The folder the run ended with, under its new name. None when the run stopped before the rename.
        original: The folder the user gave, when `source` is a copy of it: it is kept, and the output says why.
        uploaded: Whether the run uploaded anything. A run that uploaded nothing keeps the source folder.
        aborted: Whether the run was aborted after an upload, at a later tracker's questions.
    """
    if not cfg.upload.formatting.remove_source_dir:
        return
    if original is not None:
        click.secho(f"remove_source_dir is on, but {original} is kept: {_kept_reason(original)}.", fg="yellow")
        return
    if final is None or not os.path.isdir(source) or (os.path.isdir(final) and os.path.samefile(source, final)):
        return
    if cfg.directory.protects(source):
        click.secho(f"Not removing {source}: it is in library_dirs, or holds one.", fg="yellow")
        return
    if not uploaded:
        click.secho(f"remove_source_dir is on, but {source} is kept: nothing was uploaded.", fg="yellow")
        return
    assume_defaults = await interaction.assume_defaults()
    if aborted and assume_defaults:
        click.secho(f"remove_source_dir is on, but {source} is kept: the run was aborted.", fg="yellow")
        return
    question = f"Remove the source folder {source}? The release is now in {final}."
    if aborted:
        question = f"The run was aborted after an upload. {question}"
    if not assume_defaults and not await interaction.confirm(
        click.style(f"\n{question}", fg="magenta"), default=not aborted
    ):
        click.secho(f"Kept the source folder {source}.", fg="yellow")
        return
    shutil.rmtree(source)
    click.secho(f"Removed the source folder {source} (remove_source_dir).", fg="yellow")
    # Local: the converter package imports this module. With the folder gone, its conversion record goes too.
    from salmon.converter.conversions import carry_conversion

    carry_conversion(source, final)


def _kept_reason(original: str) -> str:
    """Why a run that worked on a copy of `original` leaves it where it is."""
    if cfg.directory.is_library_path(original):
        return "it is in library_dirs, and salmon never removes a library album"
    if shares_files(original):
        return "it shares files with another folder through a hardlink or symlink, so salmon worked on a copy"
    if dryrun.active():
        return "a dry run works on a copy and changes nothing"
    return "--skip-flac-upload works on a copy"


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
            template = _strip_blank_resolution(template) if k == "resolution" else strip_template_keys(template, k)
            keys.remove(k)
    sub_metadata = _fix_format(metadata, keys)
    return clean_name(template.format(**{k: _sub_illegal_characters(sub_metadata[k]) for k in keys}))


def _strip_blank_resolution(template):
    """
    Drop a blank {resolution} token from the template.

    strip_template_keys assumes a token owns the whole bracket around it, which is right for a
    token alone in its brackets but wrong for "[{source} FLAC {resolution}]": it would eat the
    closing bracket and leave "FLAC" hanging open. Here only the placeholder (and one adjacent
    separator space) goes; the bracket comes with it only if nothing else was left inside.
    A placeholder can carry a format spec, as in "{resolution:>5}"; that goes too, or it would
    survive into template.format() with no "resolution" key left to fill it.
    """
    template = re.sub(r"\s*\{resolution(?::[^}]*)?\}", "", template)
    template = re.sub(r"[\[{(]\s*[\]})]", "", template)
    template = re.sub(r"\s+", " ", template).strip()
    return re.sub(r" *- *$", "", template)


def _compile_artist_str(artist_data):
    """Create a string to represent the main artists of the release."""
    # In the order given, as the album artist tag (retagger._generate_album_artist) has it.
    artists = list(dict.fromkeys(a[0] for a in artist_data if a[1] == "main"))
    if len(artists) > cfg.upload.formatting.various_artist_threshold:
        return cfg.upload.formatting.various_artist_word
    c = ", " if len(artists) > 2 or "&" in "".join(artists) else " & "
    return c.join(artists)


def _sub_illegal_characters(stri):
    if cfg.upload.formatting.fullwidth_replacements:
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


def _is_folder_name(name: str) -> bool:
    """Whether a name is one folder name: not empty, not . or .., no path separator, something visible in it."""
    if name in {"", ".", ".."} or os.sep in name or (os.altsep and os.altsep in name):
        return False
    return has_visible_chars(name)


async def _edit_folder_interactive(foldername, auto_rename):
    """Allow the user to edit the pending folder name in a text editor."""
    if auto_rename:
        return foldername
    if not await interaction.confirm(
        click.style("Is the new folder name acceptable? ([n] to edit)", fg="magenta"),
        default=True,
    ):
        newname = await interaction.edit(foldername, editor=cfg.upload.default_editor)
        while True:
            if newname is None:
                return foldername
            elif re.search(BLACKLISTED_CHARS, newname) or not _is_folder_name(clean_name(newname.replace("\n", ""))):
                if not await interaction.confirm(
                    click.style(
                        "Folder name is empty or contains invalid characters, retry?",
                        fg="magenta",
                        bold=True,
                    ),
                    default=True,
                ):
                    sys.exit(1)
            else:
                return clean_name(newname.replace("\n", ""))
            newname = await interaction.edit(foldername, editor=cfg.upload.default_editor)
    return foldername
