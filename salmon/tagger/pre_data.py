import contextlib
import os
import re
from collections import defaultdict
from copy import deepcopy

import click

from salmon.common import RE_FEAT, re_split
from salmon.common.figles import _tracknumber_sort_key
from salmon.constants import FORMATS, TAG_ENCODINGS

EMPTY_METADATA = {
    "artists": [],
    "title": None,
    "group_year": None,
    "year": None,
    "date": None,
    "edition_title": None,
    "label": None,
    "catno": None,
    "rls_type": None,
    "genres": [],
    "format": None,
    "encoding": None,
    "encoding_vbr": None,
    "scene": None,
    "source": None,
    "cover": None,
    "upc": None,
    "comment": None,
    "urls": [],
    "tracks": {},
}


def construct_rls_data(
    tags,
    audio_info,
    source,
    encoding,
    scene=False,
    existing=None,
    overwrite=False,
    prompt_encoding=False,
):
    """Create the default release metadata from the tags."""
    if not existing:
        metadata = deepcopy(EMPTY_METADATA)
        tag_track = next(iter(tags.values()))
        metadata["title"] = tag_track.album or "None"
        if not overwrite:
            metadata["artists"] = construct_artists_li(tags)
            with contextlib.suppress(ValueError, IndexError, TypeError):
                metadata["year"] = re.search(r"(\d{4})", str(tag_track.date))[1]
            metadata["group_year"] = metadata["year"]
            metadata["upc"] = tag_track.upc
            metadata["label"] = tag_track.label
            metadata["catno"] = tag_track.catno
            metadata["genres"] = split_genres(tag_track.genre)
        metadata["tracks"] = create_track_list(tags, overwrite)
    else:
        metadata = {"artists": existing["artists"]}
        del existing["artists"]
        metadata = {**metadata, **existing}
    metadata["source"] = source
    metadata["scene"] = scene
    metadata["format"] = parse_format(next(iter(tags.keys())))

    audio_track = next(iter(audio_info.values()))
    metadata["encoding"], metadata["encoding_vbr"] = parse_encoding(
        metadata["format"], audio_track, encoding, prompt_encoding
    )
    return metadata


def construct_artists_li(tags):
    """Create a list of artists from the artist string."""
    artists = []
    for track in tags.values():
        if track.artist:
            artists += parse_artists(track.artist)
    return list(set(artists))


def split_genres(genres_list):
    """Create a list of genres from splitting the string."""
    genres = set()
    if genres_list:
        for g in genres_list:
            for genre in re_split(g):
                genres.add(genre.strip())
    return list(genres)


def parse_format(filename):
    return FORMATS[os.path.splitext(filename)[1].lower()]


def parse_encoding(format_, track, supplied_encoding, prompt_encoding):
    """Get the encoding from the FLAC files, otherwise require the user to specify it."""
    if format_ == "FLAC":
        if track["precision"] == 16:
            return "Lossless", False
        elif track["precision"] == 24:
            return "24bit Lossless", False
    if supplied_encoding and list(supplied_encoding) != [None, None]:
        return supplied_encoding
    if prompt_encoding:
        return _prompt_encoding()
    click.secho(
        "An encoding must be specified if the files are not lossless.", fg="red"
    )
    raise click.Abort


def create_track_list(tags, overwrite):
    """Generate the track data from each track tag."""
    tracks = defaultdict(dict)
    for trackindex, (_, track) in enumerate(sorted(tags.items(), key=lambda k: _tracknumber_sort_key(k[0])), 1):
        discnumber = track.discnumber or "1"
        tracknumber = (
            str(track.tracknumber).split("/")[0]
                if (track.tracknumber
                    and str(track.tracknumber).split("/")[0].isdigit()
                    and int(str(track.tracknumber).split("/")[0]) > 0)
                else str(trackindex)
        )
        tracks[discnumber][tracknumber] = {
            "track#": tracknumber,
            "disc#": discnumber,
            "tracktotal": track.tracktotal,
            "disctotal": track.disctotal,
            "artists": parse_artists(track.artist),
            "title": track.title,
            "replay_gain": track.replay_gain,
            "peak": track.peak,
            "isrc": track.isrc,
            "explicit": None,
            "format": None,
            "streamable": None,
        }
        if overwrite:
            tracks[discnumber][tracknumber]["artists"] = []
            tracks[discnumber][tracknumber]["replay_gain"] = None
            tracks[discnumber][tracknumber]["peak"] = None
            tracks[discnumber][tracknumber]["isrc"] = None
    return dict(tracks)


def parse_artists(artist_list):
    """Split the artists by common split characters, and aso accomodate features."""
    artists = []
    if not artist_list:
        artist_list = "none"
    if isinstance(artist_list, str):
        artist_list = [artist_list]
    for artist in artist_list:
        feat = RE_FEAT.search(artist)
        if feat:
            for a in re_split(feat[1]):
                artists.append((a, "guest"))
            artist = artist.replace(feat[0], "")
        remix = re.search(r" \(?remix(?:\.|ed|ed by)? ([^\)]+)\)?", artist)
        if remix:
            for a in re_split(remix[1]):
                artists.append((a, "remixer"))
            artist = artist.replace(remix[0], "")
        for a in re_split(artist):
            artists.append((a, "main"))
    return artists


def _prompt_encoding():
    click.echo(f'\nValid encodings: {", ".join(TAG_ENCODINGS.keys())}')
    while True:
        enc = click.prompt(
            click.style(
                "What is the encoding of this release? [a]bort",
                fg="magenta"
            ),
            default="",
        )
        try:
            return TAG_ENCODINGS[enc.upper()]
        except KeyError:
            if enc.lower().startswith("a"):
                raise click.Abort from None
            click.secho(f"{enc} is not a valid encoding.", fg="red")
