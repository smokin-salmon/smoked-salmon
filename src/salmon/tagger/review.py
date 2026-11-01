import re
from collections import defaultdict
from collections.abc import Sequence

import asyncclick as click

from salmon import cfg, interaction
from salmon.constants import RELEASE_TYPES
from salmon.errors import InvalidMetadataError, PickOtherResults
from salmon.tagger.metadata import _print_metadata
from salmon.tagger.sources.base import generate_artists, standardize_genres

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


# The answers to "Are there any metadata fields you would like to edit?", for salmon web.
_FIELDS_TO_EDIT = interaction.Options(
    (
        interaction.Option("a", "Artists"),
        interaction.Option("l", "Artist aliases"),
        interaction.Option("t", "Title"),
        interaction.Option("g", "Genres"),
        interaction.Option("r", "Release type"),
        interaction.Option("y", "Years"),
        interaction.Option("e", "Edition info"),
        interaction.Option("c", "Comment"),
        interaction.Option("k", "Tracks"),
        interaction.Option("u", "URLs"),
        interaction.Option("n", "Nothing"),
    )
)
# The answer to it that goes back to the metadata results, when the review offers that.
_BACK_TO_RESULTS = interaction.Option("b", "Back to the metadata results", action=True)


async def review_metadata(
    metadata,
    validator,
    enforce_required_fields: bool = True,
    rls_type_hint: str | None = None,
    picked: dict | None = None,
):
    """
    Validate that the metadata is per the user's wishes and then offer the user
    the ability to edit it.

    rls_type_hint (suggest_release_type) is the default answer when the release type has to be asked.
    picked, the metadata as the metadata results gave it, adds [b]ack to the metadata results, which raises
    PickOtherResults, asking first when the metadata has changed since.
    """
    offer_other_results = picked is not None
    fields_to_edit = _FIELDS_TO_EDIT
    if offer_other_results:
        fields_to_edit = interaction.Options((*_FIELDS_TO_EDIT.items, _BACK_TO_RESULTS))
    if enforce_required_fields:
        await _check_for_empty_release_type(metadata, rls_type_hint)
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
        r = await interaction.prompt(
            click.style(
                "\nAre there any metadata fields you would like to edit? [a]rtists, "
                "artist a[L]iases (rename or split an artist on every track), [t]itle, [g]enres, "
                "[r]elease type, [y]ears, [e]dition info, [c]omment, trac[k]s, [u]rls, [n]othing"
                + (", [b]ack to the metadata results" if offer_other_results else ""),
                fg="magenta",
            ),
            options=fields_to_edit,
        )
        r_let = (r or "n")[0].lower()
        try:
            await edit_functions[r_let](metadata)
        except KeyError:
            if r_let == "n":
                break_ = True
            elif r_let == "b" and offer_other_results:
                if metadata == picked or await interaction.confirm(
                    click.style("Going back drops the changes made since the pick. Go back?", fg="magenta"),
                    default=False,
                ):
                    raise PickOtherResults from None
                continue
            else:
                hint = " The artist aliases key is the letter L." if r_let == "1" else ""
                click.secho(f"{r_let} is not a valid editing option.{hint}", fg="red")
                continue
        try:
            validator(metadata)
        except InvalidMetadataError as e:
            if not enforce_required_fields and str(e) in _DEFERRED_PRE_AI_METADATA_ERRORS:
                if break_:
                    break
                continue
            await interaction.confirm(
                click.style(str(e) + " Revisit metadata step?", fg="magenta"),
                default=True,
                abort=True,
            )
            continue
        if break_:
            break

    _warn_classical_genre(metadata)
    return metadata


async def _check_for_empty_release_type(metadata, hint: str | None = None):
    if not metadata["rls_type"]:
        await _edit_release_type(metadata, default=hint)


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
    click.secho(
        "One artist per line as: Name (role). This changes the release's artists and the roles on tracks; "
        "to rename or split an artist on every track, use artist a[L]iases instead.",
        fg="cyan",
    )
    artist_text = "\n".join(f"{a} ({i})" for a, i in metadata["artists"])
    while True:
        artist_text = await interaction.edit(artist_text, editor=cfg.upload.default_editor)
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
            await interaction.confirm(
                click.style(f"The tracks file is invalid ({type(e)}: {e}), retry?", fg="red"),
                default=True,
                abort=True,
            )


async def _alias_artists(metadata):
    existing_artists = {a for a, _ in metadata["artists"]}
    marker = "Enter aliases below this line:"
    while True:
        artist_aliases = defaultdict(list)
        artists_to_delete = []
        artist_list_str = (
            "\n".join({a for a, _ in metadata["artists"]})
            + "\n\nOne alias per line as: existing name --> new name. Two lines with the same existing name split it"
            + " into two artists; nothing after --> removes the artist.\n"
            + f"{marker}\n\n"
        )
        artist_list = await interaction.edit(artist_list_str, editor=cfg.upload.default_editor)
        try:
            if artist_list is None:
                return
            artist_text = artist_list.split(marker)[1].strip()
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
            await interaction.confirm(
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


# Where the track count stops meaning a single, then an EP. Past 30 minutes, any release is an album.
_SINGLE_MAX_TRACKS = 3
_EP_MAX_TRACKS = 6
_ALBUM_MIN_SECONDS = 30 * 60
# A single's track past this length makes it an EP.
_SINGLE_TRACK_MAX_SECONDS = 10 * 60
_TITLE_SAYS_EP = re.compile(r"\bE\.?P\b\.?", re.IGNORECASE)
_TITLE_SAYS_SINGLE = re.compile(r"\bsingle\b", re.IGNORECASE)


def suggest_release_type(title: str | None, durations: Sequence[float]) -> str | None:
    """The release type the files' track count and length imply, as the release type prompt's default.

    1 to 3 tracks are a single, 4 to 6 an EP, more an album; past 30 minutes in all, an album, and a
    single with a track past 10 minutes, an EP. A title that names another type ("... EP", "(Single)")
    contradicts it: then there is no default.

    Args:
        title: The release title from the tags.
        durations: Each audio file's length in seconds; unknown lengths are 0.
    """
    if not durations:
        return None
    total = sum(durations)
    if total >= _ALBUM_MIN_SECONDS or len(durations) > _EP_MAX_TRACKS:
        rls_type = "Album"
    elif len(durations) > _SINGLE_MAX_TRACKS or max(durations) > _SINGLE_TRACK_MAX_SECONDS:
        rls_type = "EP"
    else:
        rls_type = "Single"
    title = title or ""
    if _TITLE_SAYS_EP.search(title) and rls_type != "EP":
        return None
    if _TITLE_SAYS_SINGLE.search(title) and rls_type != "Single":
        return None
    return rls_type


async def _edit_release_type(metadata, default: str | None = None):
    _print_release_types()
    types = {r.lower(): r for r in RELEASE_TYPES}
    while True:
        rtype = (
            (
                await interaction.prompt(
                    click.style("\nWhich release type corresponds to this release? (case insensitive)", fg="magenta"),
                    type=click.STRING,
                    default=default,
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
    title = await interaction.edit(metadata["title"], editor=cfg.upload.default_editor)
    if title:
        metadata["title"] = title.strip()


async def _edit_years(metadata):
    while True:
        text = f"Year      : {metadata['year']}\nGroup Year: {metadata['group_year']}"
        text = await interaction.edit(text, editor=cfg.upload.default_editor)
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
            await interaction.confirm(
                click.style(
                    "Invalid values or formatting in the years file. Retry?",
                    fg="magenta",
                ),
                default=True,
                abort=True,
            )


async def _edit_genres(metadata):
    genres = await interaction.edit("\n".join(metadata["genres"]), editor=cfg.upload.default_editor)
    if genres:
        standardized = standardize_genres([g.strip() for g in genres.split("\n") if g.strip()])
        if not standardized:
            click.secho("Those genres normalize to nothing; keeping the previous ones.", fg="red")
            return
        metadata["genres"] = standardized


async def _edit_urls(metadata):
    urls = await interaction.edit("\n".join(metadata["urls"]), editor=cfg.upload.default_editor)
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
        text = await interaction.edit(text, editor=cfg.upload.default_editor)
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
            await interaction.confirm(
                click.style(
                    "Invalid values or formatting in the editions file. Retry?",
                    fg="magenta",
                ),
                default=True,
                abort=True,
            )


async def _edit_comment(metadata):
    review = await interaction.edit(metadata["comment"], editor=cfg.upload.default_editor)
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
        text_tracks = await interaction.edit(text_tracks, editor=cfg.upload.default_editor)
        if not text_tracks:
            return
        try:
            edits = _parse_edited_tracks(text_tracks, metadata["tracks"])
        except ValueError as e:
            await interaction.confirm(
                click.style(f"The tracks file is invalid ({e}), retry?", fg="red"),
                default=True,
                abort=True,
            )
            continue
        # Nothing is applied before all of the text is valid, so an error leaves the metadata as it was.
        for discnum, tracknum, title, artists in edits:
            metadata["tracks"][discnum][tracknum]["title"] = title
            metadata["tracks"][discnum][tracknum]["artists"] = artists
        metadata["artists"], metadata["tracks"] = generate_artists(metadata["tracks"])
        return


def _parse_edited_tracks(text, tracks):
    """The (disc, track, title, artists) of each block of the edited tracks text.

    Raises ValueError, whose message says what is wrong and on which line, before anything is applied."""
    blocks: list[list[tuple[int, str]]] = [[]]
    for number, line in enumerate(text.split("\n"), 1):
        if re.fullmatch(r"-+", line.strip()):
            blocks.append([])
        elif line.strip():
            blocks[-1].append((number, line.strip()))

    edits = []
    seen: set[tuple[str, str]] = set()
    for block in filter(None, blocks):
        if len(block) < 3:
            raise ValueError(f"line {block[0][0]}: a track needs a Disc/Track line, a Title line and an Artists line")
        (id_no, ident), (title_no, title), (artists_no, artists_header) = block[:3]
        r_ident = re.fullmatch(r"Disc ([^ ]+) Track ([^ ]+)", ident)
        if not r_ident:
            raise ValueError(f'line {id_no}: "{ident}" is not a "Disc X Track Y" line')
        discnum, tracknum = r_ident[1], r_ident[2]
        if tracknum not in tracks.get(discnum, {}):
            raise ValueError(
                f'line {id_no}: "Disc {discnum} Track {tracknum}" is not a track of this release: the Disc/Track '
                "lines can't be changed, only the titles and artists under them."
            )
        if (discnum, tracknum) in seen:
            raise ValueError(f'line {id_no}: "Disc {discnum} Track {tracknum}" is named twice')
        seen.add((discnum, tracknum))
        title_match = re.fullmatch(r"Title *: *(.+)", title)
        if not title_match:
            raise ValueError(f'line {title_no}: "{title}" is not a "Title: ..." line')
        if not re.fullmatch(r"Artists *:", artists_header):
            raise ValueError(f'line {artists_no}: "{artists_header}" is not the "Artists:" line')
        artists = []
        for artist_no, artist_line in block[3:]:
            name_part, _, role_part = artist_line.rpartition(" ")
            role_match = re.fullmatch(r"\((.+)\)", role_part)
            name_match = re.fullmatch(r"> *(.+)", name_part)
            if not role_match or not name_match:
                raise ValueError(f'line {artist_no}: "{artist_line}" is not an artist line like "> Name (role)"')
            artists.append((name_match[1], role_match[1].lower()))
        edits.append((discnum, tracknum, title_match[1], artists))
    return edits
