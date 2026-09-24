import os
import re
import shutil
from itertools import chain
from string import Formatter
from typing import Any

import asyncclick as click
import msgspec

from salmon import cfg
from salmon.constants import (
    ARROWS,
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
)
from salmon.tagger.tagfile import TagFile


class Change(msgspec.Struct, frozen=True):
    """Data structure for tag changes."""

    tag: str
    old: Any
    new: Any


def tag_files(path, tags, metadata, auto_rename):
    """
    Wrapper function that calls the functions that create and print the
    proposed changes, and then prompts for confirmation to retag the file.
    """
    click.secho("\nRetagging files...", fg="cyan", bold=True)
    if not check_whether_to_tag(tags, metadata):
        return
    album_changes = collect_album_data(metadata)
    track_changes = create_track_changes(tags, metadata)
    print_changes(album_changes, track_changes, next(iter(tags.values())))
    if auto_rename or click.confirm(
        click.style("\nWould you like to auto-tag the files with the updated metadata?", fg="magenta"),
        default=True,
    ):
        retag_files(path, album_changes, track_changes)


def check_whether_to_tag(tags, metadata):
    """
    Make sure the number of tracks in the metadata equals the number of tracks
    in the folder.
    """
    if len(tags) != sum([len(disc) for disc in metadata["tracks"].values()]):
        click.secho(
            "Number of tracks differed from number of tracks in metadata, skipping retagging procedure...",
            fg="red",
        )
        return False
    return True


def collect_album_data(metadata):
    """Create a dictionary of the proposed album tags (consistent across every track)."""
    if cfg.upload.formatting.add_edition_title_to_album_tag and metadata["edition_title"]:
        title = f"{metadata['title']} ({metadata['edition_title']})"
    else:
        title = metadata["title"]
    return {
        k: v
        for k, v in {
            "album": title,
            "genre": "; ".join(sorted(metadata["genres"])),
            "date": metadata["group_year"],
            "label": metadata["label"],
            "catno": metadata["catno"],
            "albumartist": _generate_album_artist(metadata["artists"]),
            "upc": metadata["upc"],
            "comment": metadata["comment"] if cfg.upload.description.review_as_comment_tag else None,
        }.items()
        if v
    }


def _generate_album_artist(artists):
    main_artists = [a for a, i in artists if i == "main"]
    if len(main_artists) >= cfg.upload.formatting.various_artist_threshold:
        return cfg.upload.formatting.various_artist_word
    c = ", " if len(main_artists) > 2 or "&" in "".join(main_artists) else " & "
    return c.join(sorted(main_artists))


def create_track_changes(tags, metadata):
    """
    Compare the track data in the metadata to the track data in the tags
    and record all differences.
    """
    changes = {}
    tracks = metadata_to_track_list(metadata["tracks"])

    def disc_track_key(tagset):
        return (_get_tag_number(tagset, "discnumber"), _get_tag_number(tagset, "tracknumber"))

    disc_track_keys = [disc_track_key(tagset) for tagset in tags.values()]
    if len(set(disc_track_keys)) == len(disc_track_keys):
        # Every file has its own distinct disc/track pair, so its embedded tags can be
        # trusted to identify it. Files without a discnumber tag (e.g. an untagged CD1/CD2
        # folder layout) all default to disc 1 and collide here, so we fall back to the
        # existing file order instead of mismatching them.
        ordered_tags = sorted(tags.items(), key=lambda item: disc_track_key(item[1]))
    else:
        ordered_tags = list(tags.items())

    for (filename, tagset), trackmeta in zip(ordered_tags, tracks, strict=False):
        changes[filename] = []

        try:
            old_artist_str = ", ".join(tagset.artist)
        except TypeError:
            old_artist_str = "None"

        new_artist_str = create_artist_str(trackmeta["artists"])
        if old_artist_str != new_artist_str:
            changes[filename].append(Change("artist", old_artist_str, new_artist_str))

        old_composer = getattr(tagset, "composer", None) or "None"
        new_composer = create_composer_str(trackmeta["artists"])
        if new_composer and old_composer != new_composer:
            changes[filename].append(Change("composer", old_composer, new_composer))

        old_conductor = getattr(tagset, "conductor", None) or "None"
        new_conductor = create_conductor_str(trackmeta["artists"])
        if new_conductor and old_conductor != new_conductor:
            changes[filename].append(Change("conductor", old_conductor, new_conductor))

        if cfg.upload.formatting.guests_in_track_title:
            trackmeta["title"] = append_guests_to_track_titles(trackmeta)

        if cfg.upload.description.empty_track_comment_tag and getattr(tagset, "comment", False):
            changes[filename].append(Change("comment", tagset.comment, ""))

        for tagfield, metafield in [
            ("title", "title"),
            ("isrc", "isrc"),
            ("tracknumber", "track#"),
            ("discnumber", "disc#"),
            ("tracktotal", "tracktotal"),
            ("disctotal", "disctotal"),
        ]:
            change = _compare_tag(tagfield, metafield, tagset, trackmeta)
            if change:
                changes[filename].append(change)
    return changes


def append_guests_to_track_titles(track):
    guest_artists = [a for a, i in track["artists"] if i == "guest"]
    if (
        "feat" not in track["title"]
        and guest_artists
        and len(guest_artists) <= cfg.upload.formatting.various_artist_threshold
    ):
        c = ", " if len(guest_artists) > 2 or "&" in "".join(guest_artists) else " & "
        # If we find a remix parenthetical, remove it and re-add it after the guest artists.
        remix = re.search(r"( \([^\)]+Remix(?:er)?\))", track["title"], flags=re.IGNORECASE)
        if remix:
            track["title"] = track["title"].replace(remix[1], "")
        track["title"] += f" (feat. {c.join(sorted(guest_artists))})"
        if remix:
            track["title"] += remix[1]
    return track["title"]


def metadata_to_track_list(metadata):
    """Turn the double nested dictionary of tracks into a flat list of tracks."""
    return list(chain.from_iterable([d.values() for d in metadata.values()]))


def _compare_tag(tagfield, metafield, tagset, trackmeta):
    """
    Compare a tag to the equivalent metadata field. If the metadata field
    does not equal the existing tag, return a ``Change``.
    """
    if trackmeta[metafield]:
        if not getattr(tagset, tagfield, False):
            return Change(tagfield, None, trackmeta[metafield])
        if str(getattr(tagset, tagfield, "")) != str(trackmeta[metafield]):
            return Change(tagfield, getattr(tagset, tagfield, "None"), trackmeta[metafield])
    return None


def create_artist_str(artists):
    """Create the artist string from the metadata.

    For classical-friendly tagging, conductor roles are included in the ARTIST
    tag after the main performer list, while composer roles are excluded and
    written to their own COMPOSER tag.
    """
    main_artists = _ordered_unique(a for a, i in artists if i == "main")
    conductors = _ordered_unique(a for a, i in artists if i == "conductor")
    lead_artists = _ordered_unique([*main_artists, *conductors])

    if conductors:
        artist_str = ", ".join(lead_artists)
    else:
        c = ", " if len(lead_artists) > 2 and "&" not in "".join(lead_artists) else " & "
        artist_str = c.join(lead_artists)

    if not cfg.upload.formatting.guests_in_track_title:
        guest_artists = _ordered_unique(a for a, i in artists if i == "guest")
        if len(guest_artists) >= cfg.upload.formatting.various_artist_threshold:
            artist_str += f" (feat. {cfg.upload.formatting.various_artist_word})"
        elif guest_artists:
            c = ", " if len(guest_artists) > 2 and "&" not in "".join(guest_artists) else " & "
            artist_str += f" (feat. {c.join(guest_artists)})"

    return artist_str


def create_composer_str(artists):
    """Create the composer string from the metadata."""
    composers = _ordered_unique(a for a, i in artists if i == "composer")
    return ", ".join(composers)


def create_conductor_str(artists):
    """Create the conductor string from the metadata."""
    conductors = _ordered_unique(a for a, i in artists if i == "conductor")
    return ", ".join(conductors)


def _ordered_unique(values):
    """Preserve the first-seen order while removing duplicates."""
    return list(dict.fromkeys(values))


def print_changes(album_changes, track_changes, a_track):
    """Print all the proposed track changes, then all the album data."""
    if any(t for t in track_changes.values()):
        click.secho("\nProposed tag changes:", fg="yellow", bold=True)
    for filename, changes in track_changes.items():
        if changes:
            click.secho(f"> {filename}", fg="yellow")
            for change in changes:
                click.echo(f"  {change.tag.ljust(20)} ••• {change.old} {ARROWS} {change.new}")

    click.secho("\nAlbum tags (applied to all):", fg="yellow", bold=True)
    for field, value in album_changes.items():
        previous = getattr(a_track, field, "None")
        if isinstance(previous, list):
            previous = "; ".join(previous)
        is_different = str(previous) != str(value)
        if not is_different:
            click.secho(f"> {field.ljust(13)} ••• {previous}")
        else:
            click.echo(
                f"> {click.style(str(field.ljust(13)), bold=True)} ••• {str(previous)} "
                f"{ARROWS} {click.style(str(value), bold=True)}"
            )


def retag_files(path, album_changes, track_changes):
    """Apply the proposed metadata changes to the files."""
    for filename, changes in track_changes.items():
        mut = TagFile(os.path.join(path, filename))
        for change in changes:
            setattr(mut, change.tag, str(change.new))
        for tag, value in album_changes.items():
            setattr(mut, tag, str(value))
        mut.save()
    click.secho("Retagged files.", fg="green")


def rename_files(path, tags, metadata, auto_rename, spectral_ids, source=None):
    """
    Call functions that generate the proposed changes, then print and prompt
    for confirmation. Apply the changes if user agrees.
    """
    to_rename = []
    folders_to_create = set()
    multi_disc = len(metadata["tracks"]) > 1
    md_word = {"CD": "CD", "Vinyl": "LP"}.get(source or "", "Part")
    # "Part" is default if not CD or Vinyl
    # Keep a multi-disc release in the release folder, its tracks numbered <disc>.<track>
    single_folder = multi_disc and not cfg.upload.formatting.split_multi_disc_into_folders
    # Disc numbers of the tracks in each folder that is emptied into the release folder
    folder_discs = {}

    track_list = list(chain.from_iterable([d.values() for d in metadata["tracks"].values()]))
    multiple_artists = any(
        {a for a, i in t["artists"] if i == "main"} != {a for a, i in track_list[0]["artists"] if i == "main"}
        for t in track_list[1:]
    )

    # In one folder, every disc and track number is padded to the width of the largest, so the files sort by
    # disc, then track
    disc_digits = len(str(max((_get_tag_number(t, "discnumber") for t in tags.values()), default=1)))
    track_digits = max(2, len(str(max((_get_tag_number(t, "tracknumber") for t in tags.values()), default=1))))

    for filename, tracktags in tags.items():
        ext = os.path.splitext(filename)[1].lower()
        new_name = generate_file_name(tracktags, ext, multiple_artists)
        disc_number = 1  # Default value
        if single_folder:
            disc_number = _get_tag_number(tracktags, "discnumber")
            track_number = _get_tag_number(tracktags, "tracknumber")
            new_name = generate_file_name(
                tracktags,
                ext,
                multiple_artists,
                trackno_or=f"{disc_number:0{disc_digits}d}.{track_number:0{track_digits}d}",
            )
            folder = os.path.dirname(os.path.join(path, filename))
            if folder != path:
                folder_discs.setdefault(folder, set()).add(disc_number)
        elif multi_disc:
            if isinstance(tracktags, dict):
                disc_number = int(tracktags["discnumber"][0].split("/")[0]) if "discnumber" in tracktags else 1
            else:
                disc_number = int(tracktags.discnumber.split("/")[0]) or 1
            new_name = os.path.join(f"{md_word}{disc_number:02d}", new_name)
        if filename != new_name:
            to_rename.append((filename, new_name))
            if multi_disc and not single_folder:
                folders_to_create.add(os.path.join(path, f"{md_word}{disc_number:02d}"))

    if to_rename:
        print_filenames(to_rename)
        if single_folder and (clashes := _rename_clashes(path, to_rename)):
            click.secho("\nNot renaming: these files would overwrite another file.", fg="red")
            for name in clashes:
                click.secho(f"   {name}", fg="red")
            return
        if auto_rename or click.confirm(
            click.style("\nWould you like to rename the files?", fg="magenta"),
            default=True,
        ):
            for folder in folders_to_create:
                if not os.path.isdir(folder):
                    os.mkdir(folder)
            directory_move_pairs = set()
            for filename, new_name in to_rename:
                old_dir = os.path.dirname(os.path.join(path, filename))
                new_dir = os.path.dirname(os.path.join(path, new_name))

                if old_dir != path:
                    directory_move_pairs.add((os.path.splitext(filename)[1], old_dir, new_dir))
                new_path, new_path_ext = os.path.splitext(os.path.join(path, new_name))
                # new_path = new_path[: 200 - len(new_path_ext) + len(os.path.dirname(path))] + new_path_ext
                new_path = new_path + new_path_ext
                os.rename(os.path.join(path, filename), new_path)

                # Update spectral_ids with new filenames, if spectrals were generated
                if spectral_ids:
                    for old_name, new_name in to_rename:
                        for key, value in spectral_ids.items():
                            if value == old_name:
                                spectral_ids[key] = new_name

            # A folder holding one disc's tracks has its other files named for that disc (log.2.log)
            disc_of_folder = {folder: discs.pop() for folder, discs in folder_discs.items() if len(discs) == 1}
            move_non_audio_files(directory_move_pairs, disc_of_folder)
            delete_empty_folders(path)
    else:
        click.secho("\nNo file renaming is recommended.", fg="green")


def print_filenames(to_rename):
    """Print all the proposed filename changes."""
    click.secho("\nProposed filename changes:", fg="yellow", bold=True)
    for filename, new_name in to_rename:
        click.echo(f"   {filename} {ARROWS} {new_name}")


def generate_file_name(tags, ext, multiple_artists, trackno_or=None):
    """Generate the template keys and format the template with the tags."""
    template = cfg.upload.formatting.file_template
    keys = [fn for _, fn, _, _ in Formatter().parse(template) if fn]
    if (
        "artist" in keys
        and cfg.upload.formatting.no_artist_in_filename_if_only_one_album_artist
        and not multiple_artists
    ):
        keys.remove("artist")
        template = cfg.upload.formatting.one_album_artist_file_template
    if isinstance(tags, dict):
        template_keys: dict[str, str | int] = {}
        for k in keys:
            tag_val = tags.get(k)
            if tag_val is not None and isinstance(tag_val, list) and tag_val:
                template_keys[k] = _parse_integer(tag_val[0])
            else:
                template_keys[k] = _parse_integer("")
    else:
        template_keys = {}
        for k in keys:
            raw_val = getattr(tags, k, "")
            if k == "artist" and isinstance(raw_val, list) and raw_val:
                raw_val = raw_val[0]
            val = _parse_integer(raw_val if isinstance(raw_val, (str, int)) else str(raw_val))
            template_keys[k] = val

    if "artist" in keys:
        if isinstance(tags, dict):
            artist_count = str(tags["artist"]).count(",") + str(tags["artist"]).count("&")
        else:
            artist_count = str(tags.artist).count(",") + str(tags.artist).count("&")
        if artist_count > cfg.upload.formatting.various_artist_threshold:
            template_keys["artist"] = cfg.upload.formatting.various_artist_word
    if "tracknumber" in keys and trackno_or is not None:
        template_keys["tracknumber"] = trackno_or
    new_base = template.format(**template_keys) + ext
    if cfg.upload.description.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            new_base = new_base.replace(char, sub)
    return re.sub(BLACKLISTED_CHARS, cfg.upload.formatting.blacklisted_substitution, new_base)


def _parse_integer(value):
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return f"{int(value):02d}"
    return value


def _get_tag_number(tracktags, field):
    """Read a disc/track number off a tag object or dict, defaulting to 1."""
    value = tracktags.get(field) if isinstance(tracktags, dict) else getattr(tracktags, field, None)

    if isinstance(value, list) and value:
        value = value[0]
    if value is None:
        return 1
    if isinstance(value, str):
        value = value.split("/")[0]
        return int(value) if value.isdigit() else 1
    if isinstance(value, int):
        return value
    return 1


def _rename_clashes(path, to_rename):
    """Return the new names that more than one file would get, or that another file already has."""
    counts = {}
    for _, new_name in to_rename:
        counts[new_name] = counts.get(new_name, 0) + 1
    clashes = []
    for filename, new_name in to_rename:
        old_path, new_path = os.path.join(path, filename), os.path.join(path, new_name)
        # On a case-insensitive filesystem the new name can be the file itself
        taken = os.path.lexists(new_path) and not (os.path.exists(new_path) and os.path.samefile(old_path, new_path))
        if (counts[new_name] > 1 or taken) and new_name not in clashes:
            clashes.append(new_name)
    return clashes


def move_non_audio_files(directory_move_pairs, disc_of_folder=None):
    """
    Move the files other than the tracks (logs, cues, covers, scan folders) out of each folder the tracks
    were moved out of, into the tracks' new folder. A file never replaces one already there: it stays where
    it is, with a warning.

    When the tracks of a multi-disc release are moved into the release folder, ``disc_of_folder`` gives the
    disc number of each folder that held one disc: its files are named for that disc (``rip.log`` from disc 2
    becomes ``rip.2.log``, ``Scans`` becomes ``Scans.2``), so several discs' files can sit side by side.
    """
    for ext, old_dir, new_dir in sorted(directory_move_pairs):
        if old_dir == new_dir:
            continue
        disc_number = (disc_of_folder or {}).get(old_dir)
        for file in sorted(os.listdir(old_dir)):
            old_path = os.path.join(old_dir, file)
            if file.endswith(ext) and not os.path.isdir(old_path):
                continue
            new_file = file
            if disc_number is not None:
                base, file_ext = (file, "") if os.path.isdir(old_path) else os.path.splitext(file)
                new_file = f"{base}.{disc_number}{file_ext}"
            new_path = os.path.join(new_dir, new_file)
            if os.path.lexists(new_path):
                click.secho(f"Left {old_path} where it is: {new_path} already exists.", fg="yellow")
                continue
            shutil.move(old_path, new_path)


def delete_empty_folders(path):
    for root, dirs, files in os.walk(path):
        if not dirs and not files:
            os.rmdir(root)
