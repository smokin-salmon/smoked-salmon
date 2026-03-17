import re
from collections import defaultdict

import asyncclick as click

from salmon import cfg
from salmon.constants import RELEASE_TYPES
from salmon.errors import InvalidMetadataError
from salmon.tagger.metadata import _print_metadata
from salmon.tagger.sources.base import generate_artists

_CLASSICAL_GENRES = {
    "classical",
    "baroque",
    "chambermusic",
    "choral",
    "modernclassical",
    "orchestral",
    "opera",
}
_DEFERRED_PRE_AI_METADATA_ERRORS = {
    "Invalid release type.",
    "You must specify at least one genre.",
    "Label must be over 2 and under 80 characters.",
    "Records DK is not a label. It's a platform for releasing albums. Please change the label (e.g Self Released)",
    "Catno must be over 2 and under 80 characters.",
}


async def review_metadata(metadata, validator, enforce_required_fields: bool = True):
    """
    Validate that the metadata is per the user's wishes and then offer the user
    the ability to edit it.
    """
    if enforce_required_fields:
        await _check_for_empty_release_type(metadata)
        await _check_for_empty_genre_list(metadata)

    break_ = False
    edit_functions = {
        "a": _edit_artists,
        "l": _alias_artists,
        "t": _edit_title,
        "g": _edit_genres,
        "r": _edit_release_type,
        "y": _edit_years,
        "e": _edit_edition_info,
        "c": _edit_comment,
        "k": _edit_tracks,
        "u": _edit_urls,
    }
    while True:
        _print_metadata(metadata)
        r = await click.prompt(
            click.style(
                "\nAre there any metadata fields you would like to edit? [a]rtists, "
                "artist a[l]iases, [t]itle, [g]enres, [r]elease type, [y]ears, "
                "[e]dition info, [c]omment, trac[k]s, [u]rls, [n]othing",
                fg="magenta",
            )
        )
        r_let = r[0].lower()
        try:
            await edit_functions[r_let](metadata)
        except KeyError:
            if r_let == "n":
                break_ = True
            else:
                click.secho(f"{r_let} is not a valid editing option.", fg="red")
                continue
        try:
            validator(metadata)
        except InvalidMetadataError as e:
            if not enforce_required_fields and str(e) in _DEFERRED_PRE_AI_METADATA_ERRORS:
                if break_:
                    break
                continue
            click.confirm(
                click.style(str(e) + " Revisit metadata step?", fg="magenta"),
                default=True,
                abort=True,
            )
            continue
        if break_:
            break

    _warn_classical_genre(metadata)
    return metadata


async def _check_for_empty_release_type(metadata):
    if not metadata["rls_type"]:
        await _edit_release_type(metadata)


async def _check_for_empty_genre_list(metadata):
    if not metadata["genres"]:
        # Use secho instead of prompt since we just want to display a message
        click.secho(
            "\nNo genres were found for this release, but one must be added. Opening the genre editor.",
            fg="magenta",
        )
        await _edit_genres(metadata)


def _warn_classical_genre(metadata: dict) -> None:
    """Display a warning when a classical genre is detected.

    Classical music has stricter tagging standards on Gazelle trackers that
    salmon cannot currently enforce automatically. This function alerts the
    user so they can verify compliance manually before uploading.

    Args:
        metadata: The release metadata dictionary.
    """
    detected = {g for g in metadata["genres"] if g.lower() in _CLASSICAL_GENRES}
    if not detected:
        return

    click.secho(
        "\n==================== CLASSICAL MUSIC WARNING ====================",
        fg="yellow",
        bold=True,
    )
    click.secho(
        f"""\
Detected classical genre(s): {", ".join(sorted(detected))}

Salmon is NOT currently suited for uploading classical music.
Classical releases have strict tagging requirements (composer tags,
per-work artist/composer fields, specific title formatting, etc.)
that salmon cannot handle automatically.

It is STRONGLY RECOMMENDED to upload classical music manually
through the tracker's upload form to ensure full compliance.

If you choose to continue, please verify the following manually:

[Composer]  Must be in the COMPOSER tag (not Artist), full name
            required (e.g. "Johann Sebastian Bach", not "Bach").
            Set per-work on multi-composer albums.

[Artist]    Must list performers only, in order:
            Soloist(s), Orchestra/Ensemble, Conductor.
            Set per-work on multi-work albums.

[Title]     Format: <Work Name> - <Movement No.>. <Tempo/Name>
            e.g. "Symphony No. 5 in C minor, Op. 67 - I. Allegro con brio"
            Do NOT include the composer name in the title.

[Date]      Must be the release date, NOT the composition date.

[Album]     Should match the label/spine title where possible.

[Upload]    Use the Composer field for composer(s), Conductor for
            conductor(s), Main Artist for performer(s) on the form.

For full rules, see the tracker's Classical Tagging Guide.""",
        fg="yellow",
    )
    click.secho(
        "=================================================================\n",
        fg="yellow",
        bold=True,
    )


async def _edit_artists(metadata):
    artist_text = "\n".join(f"{a} ({i})" for a, i in metadata["artists"])
    while True:
        artist_text = click.edit(artist_text, editor=cfg.upload.default_editor)
        if not artist_text:
            return
        try:
            artists_li = [t.strip() for t in artist_text.split("\n") if t.strip()]
            tuples_artists_list = []
            for artist_line in artists_li:
                name, role = artist_line.rsplit(" ", 1)
                role_match = re.search(r"\((.+)\)", role)
                if not role_match:
                    raise ValueError(f"Invalid role format: {role}")
                role = role_match[1].lower()
                tuples_artists_list.append((name, role))
            metadata["artists"] = tuples_artists_list

            # Now update the track-level artists
            for _disc_number, disc_data in metadata["tracks"].items():
                for _track_number, track_info in disc_data.items():
                    track_artists = track_info["artists"]
                    updated_track_artists = []
                    for artist_name, artist_role in track_artists:
                        # Update role for each artist from the album-level metadata
                        updated_role = next(
                            (role for name, role in tuples_artists_list if name == artist_name), artist_role
                        )
                        updated_track_artists.append((artist_name, updated_role))
                    track_info["artists"] = updated_track_artists

            return
        except (ValueError, KeyError, TypeError) as e:
            click.confirm(
                click.style(f"The tracks file is invalid ({type(e)}: {e}), retry?", fg="red"),
                default=True,
                abort=True,
            )


async def _alias_artists(metadata):
    existing_artists = {a for a, _ in metadata["artists"]}
    while True:
        artist_aliases = defaultdict(list)
        artists_to_delete = []
        artist_list_str = (
            "\n".join({a for a, _ in metadata["artists"]})
            + "\n\nEnter the artist alias list below. Refer to README for syntax.\n\n"
        )
        artist_list = click.edit(artist_list_str, editor=cfg.upload.default_editor)
        try:
            if artist_list is None:
                return
            artist_text = artist_list.split("Refer to README for syntax.")[1].strip()
            for line in artist_text.split("\n"):
                if line:
                    existing, new = [a.strip() for a in line.split("-->", 1)]
                    if existing not in existing_artists:
                        raise ValueError  # Too lazy to create new exception.
                    if new:
                        artist_aliases[existing.lower()].append(new)
                    else:
                        artists_to_delete.append(existing.lower())
            break
        except (IndexError, ValueError):
            click.confirm(
                click.style("Invalid artist list. Retry?", fg="red"),
                default=True,
                abort=True,
            )
        except AttributeError:
            return

    for i, (artist, importa) in enumerate(metadata["artists"]):
        if artist.lower() in artist_aliases:
            metadata["artists"].pop(i)
            for artist_name in artist_aliases[artist.lower()]:
                if artist_name:
                    metadata["artists"].append((artist_name, importa))
    for i, (artist, _) in enumerate(metadata["artists"]):
        if artist.lower() in artists_to_delete:
            metadata["artists"].pop(i)

    for dnum, disc in metadata["tracks"].items():
        for tnum, track in disc.items():
            for i, (artist, importa) in enumerate(track["artists"]):
                if artist.lower() in artist_aliases:
                    metadata["tracks"][dnum][tnum]["artists"].pop(i)
                    for artist_name in artist_aliases[artist.lower()]:
                        if artist_name:
                            metadata["tracks"][dnum][tnum]["artists"].append((artist_name, importa))
    for dnum, disc in metadata["tracks"].items():
        for tnum, track in disc.items():
            for i, (artist, _) in enumerate(track["artists"]):
                if artist.lower() in artists_to_delete:
                    metadata["tracks"][dnum][tnum]["artists"].pop(i)


async def _edit_release_type(metadata):
    _print_release_types()
    types = {r.lower(): r for r in RELEASE_TYPES}
    while True:
        rtype = (
            (
                await click.prompt(
                    click.style("\nWhich release type corresponds to this release? (case insensitive)", fg="magenta"),
                    type=click.STRING,
                )
            )
            .strip()
            .lower()
        )
        if rtype in types:
            metadata["rls_type"] = types[rtype]
            return
        click.secho(f"{rtype} is not a valid release type.", fg="red")


def _print_release_types():
    types = RELEASE_TYPES.keys()
    longest = max(len(r) for i, r in enumerate(types) if i % 2 == 0)
    click.secho("\nRelease Types:", fg="yellow", bold=True)
    for i, rtype in enumerate(types):
        click.echo(f"  {rtype.ljust(longest)}", nl=False)
        if i % 2 == 1:
            click.echo()
    click.echo()


async def _edit_title(metadata):
    title = click.edit(metadata["title"], editor=cfg.upload.default_editor)
    if title:
        metadata["title"] = title.strip()


async def _edit_years(metadata):
    while True:
        text = f"Year      : {metadata['year']}\nGroup Year: {metadata['group_year']}"
        text = click.edit(text, editor=cfg.upload.default_editor)
        try:
            if not text:
                return
            year_line, group_year_line = (line.strip() for line in text.strip().split("\n", 1))
            year_match = re.match(r"Year *: *(\d{4})", year_line)
            group_year_match = re.match(r"Group Year *: *(\d{4})", group_year_line)
            if not year_match or not group_year_match:
                raise ValueError("Invalid year format")
            metadata["year"] = year_match[1]
            metadata["group_year"] = group_year_match[1]
            return
        except (TypeError, KeyError, ValueError):
            click.confirm(
                click.style(
                    "Invalid values or formatting in the years file. Retry?",
                    fg="magenta",
                ),
                default=True,
                abort=True,
            )


async def _edit_genres(metadata):
    genres = click.edit("\n".join(metadata["genres"]), editor=cfg.upload.default_editor)
    if genres:
        metadata["genres"] = [g for g in genres.split("\n") if g.strip()]


async def _edit_urls(metadata):
    urls = click.edit("\n".join(metadata["urls"]), editor=cfg.upload.default_editor)
    if urls:
        metadata["urls"] = [g for g in urls.split("\n") if g.strip()]


async def _edit_edition_info(metadata):
    while True:
        text = (
            f"Label         : {metadata['label'] or ''}\n"
            f"Catalog Number: {metadata['catno'] or ''}\n"
            f"Edition Title : {metadata['edition_title'] or ''}\n"
            f"UPC           : {metadata['upc'] or ''}"
        )
        text = click.edit(text, editor=cfg.upload.default_editor)
        try:
            if not text:
                return
            label_line, cat_line, title_line, upc_line = (line.strip() for line in text.strip().split("\n", 3))
            label_match = re.match(r"Label *: *(.*)", label_line)
            catno_match = re.match(r"Catalog Number *: *(.*)", cat_line)
            edition_match = re.match(r"Edition Title *: *(.*)", title_line)
            upc_match = re.match(r"UPC *: *(.*)", upc_line)
            if not label_match or not catno_match or not edition_match or not upc_match:
                raise ValueError("Invalid format")
            metadata["label"] = label_match[1] or None
            metadata["catno"] = catno_match[1] or None
            metadata["edition_title"] = edition_match[1] or None
            metadata["upc"] = upc_match[1] or None
            return
        except (TypeError, KeyError, ValueError):
            click.confirm(
                click.style(
                    "Invalid values or formatting in the editions file. Retry?",
                    fg="magenta",
                ),
                default=True,
                abort=True,
            )


async def _edit_comment(metadata):
    review = click.edit(metadata["comment"], editor=cfg.upload.default_editor)
    metadata["comment"] = review.strip() if review else None


async def _edit_tracks(metadata):
    text_tracks_li = []
    for dnum, disc in metadata["tracks"].items():
        for tnum, track in disc.items():
            text_tracks_li.append(
                f"Disc {dnum} Track {tnum}\n"
                f"Title: {track['title']}\n"
                f"Artists:\n" + "\n".join(f"> {a} ({i})" for a, i in track["artists"])
            )

    text_tracks = "\n\n-----\n\n".join(text_tracks_li)
    while True:
        text_tracks = click.edit(text_tracks, editor=cfg.upload.default_editor)
        if not text_tracks:
            return
        try:
            tracks_li = [tr for tr in re.split("\n-+\n", text_tracks) if tr.strip()]
            for track_tx in tracks_li:
                ident, title, _, *artists_li = [t.strip() for t in track_tx.split("\n") if t.strip()]
                r_ident = re.search(r"Disc ([^ ]+) Track ([^ ]+)", ident)
                if not r_ident:
                    raise ValueError("Invalid track identifier format")
                discnum, tracknum = r_ident[1], r_ident[2]
                title_match = re.search(r"Title *: *(.+)", title)
                if not title_match:
                    raise ValueError("Invalid title format")
                metadata["tracks"][discnum][tracknum]["title"] = title_match[1]

                tuples_artists_list = []
                for artist_line in artists_li:
                    artist_line_name, artist_line_role = artist_line.rsplit(" ", 1)
                    role_match = re.search(r"\((.+)\)", artist_line_role)
                    name_match = re.search(r"> *(.+)", artist_line_name)
                    if not role_match or not name_match:
                        raise ValueError("Invalid artist format")
                    tuples_artists_list.append((name_match[1], role_match[1].lower()))
                metadata["tracks"][discnum][tracknum]["artists"] = tuples_artists_list
            metadata["artists"], metadata["tracks"] = generate_artists(metadata["tracks"])
            return
        except (TypeError, ValueError, KeyError) as e:
            click.confirm(
                click.style(f"The tracks file is invalid ({type(e)}: {e}), retry?", fg="red"),
                default=True,
                abort=True,
            )
