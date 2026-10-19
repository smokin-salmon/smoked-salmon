import re
from collections import defaultdict

from salmon.common.strings import strip_numbered_suffix
from salmon.sources import DiscogsBase
from salmon.tagger.sources.base import MetadataMixin

VALID_EDITION_TITLES = {
    "Remastered",
    "Reissue",
    "Repress",
    "Club Edition",
    "Deluxe Edition",
    "Enhanced",
    "Limited Edition",
    "Mixed",
    "Partially Mixed",
    "Promo",
    "Special Edition",
    "Mono",
    "Quadraphonic",
    "Ambisonic",
    "Unofficial Release",
}

ROLES = {
    "Composed By": "composer",
    "Producer": "producer",
    "Featuring": "guest",
    "Vocals": "guest",
    "Featuring [Vocals]": "guest",
    "Remix": "remixer",
    "Arranged By": "arranger",
}

RE_FEAT_JOIN = re.compile(r"(?:feat|featuring|ft)\.?", flags=re.IGNORECASE)

RELEASE_TYPES = {
    "Album": "Album",
    "Mini-Album": "Album",
    "EP": "EP",
    "Sampler": "EP",
    "Single": "Single",
    "Maxi-Single": "Single",
    "Compilation": "Compilation",
    "Mixtape": "Mixtape",
}

SOURCES = {
    "Vinyl": "Vinyl",
    "File": "WEB",
    "CD": "CD",
}


def _clean_discogs_position(position: str, default_disc: int) -> tuple[str, str]:
    """Parse raw Discogs position string into clean disc and track numbers.

    Examples:
        '1-01' -> ('1', '1')
        '2-15' -> ('2', '15')
        '01'   -> ('1', '1')
    """
    pos = str(position).strip().upper()

    # Handles "1-01", "2-15" multi-disc format
    match = re.match(r"^(\d+)-(\d+)$", pos)
    if match:
        return str(int(match[1])), str(int(match[2]))

    # Handles "01", "02", "1"
    if pos.isdigit():
        return str(default_disc), str(int(pos))

    return str(default_disc), pos


class Scraper(DiscogsBase, MetadataMixin):
    def parse_release_title(self, soup):
        return soup["title"]

    def parse_cover_url(self, soup):
        try:
            return soup["images"][0]["resource_url"]
        except (KeyError, IndexError):
            return None

    def parse_genres(self, soup):
        return set(soup["genres"])

    def parse_release_year(self, soup):
        return int(soup["year"])

    def parse_release_date(self, soup):
        if "released" in soup and re.match(r"\d{4}-\d{2}-\d{2}", soup["released"]):
            return soup["released"]

    def parse_edition_title(self, soup):
        if soup["formats"] and "descriptions" in soup["formats"][0]:
            return (
                " / ".join([w for w in soup["formats"][0]["descriptions"] if any(v in w for v in VALID_EDITION_TITLES)])
                or None
            )

    def parse_release_label(self, soup):
        if soup["labels"]:
            return sanitize_artist_name(soup["labels"][0]["name"])
        return "Not On Label"

    def parse_release_catno(self, soup):
        if soup["labels"] and soup["labels"][0]["catno"] != "none":
            return soup["labels"][0]["catno"]

    def parse_release_type(self, soup):
        if "formats" in soup and soup["formats"] and "descriptions" in soup["formats"][0]:
            try:
                return next(iter(RELEASE_TYPES[f] for f in soup["formats"][0]["descriptions"] if f in RELEASE_TYPES))
            except StopIteration:
                return

    async def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        cur_disc = 1
        for track in soup["tracklist"]:
            if track["type_"] == "heading" and tracks:
                cur_disc += 1
            elif track["type_"] == "track":
                raw_pos = track.get("position", "")
                disc_str, track_num = _clean_discogs_position(raw_pos, cur_disc)
                disc_num = int(disc_str)

                artists, variations = parse_credits(soup["artists"], track)
                tracks[disc_str][track_num] = self.generate_track(
                    trackno=track_num,
                    discno=disc_num,
                    artists=artists,
                    title=track["title"],
                    duration=parse_duration(track.get("duration")),
                )
                # Only the merge reads this, to match tag artists spelled the way the release credits them.
                tracks[disc_str][track_num]["artist_variations"] = variations
        return dict(tracks)


def parse_duration(duration):
    """Convert Discogs' "m:ss" / "h:mm:ss" track duration to seconds."""
    if not duration:
        return None
    parts = duration.strip().split(":")
    # isdecimal(), not isdigit(): isdigit() accepts some Unicode digits (superscript "2") that int() rejects.
    if not all(p.isdecimal() for p in parts) or not 2 <= len(parts) <= 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def parse_artists(artist_soup, track):
    """
    Generate the artists list from the artist dictionary provided with
    each track.
    """
    return parse_credits(artist_soup, track)[0]


def parse_credits(artist_soup, track):
    """
    Generate the artists list of a track and, beside it, the name each artist is
    credited under on this release when that differs from the main Discogs name
    (the ANV): `[{"name": ..., "role": ..., "anv": ...}]`, plain data that survives JSON.
    """
    credits = track["artists"] if "artists" in track else [art for art in artist_soup if art["name"] != "Various"]
    artists, variations = _credited_artists(credits)
    if "extraartists" in track:
        for art in track["extraartists"]:
            for role in art["role"].split(","):
                role = role.strip()
                credit = (sanitize_artist_name(art["name"]), ROLES.get(role))
                if role in ROLES and credit not in artists:
                    artists.append(credit)
                    _note_variation(variations, credit, art)
        for name, role in artists:
            # An artist who also arranged the track stays its main artist.
            if role != "main" and (name, "main") in artists and (name, "arranger") not in artists:
                artists.remove((name, "main"))
    return artists, variations


def _credited_artists(credits):
    """Name the artists of a credit list, making those after a feat. join guests."""
    artists = []
    variations = []
    role = "main"
    for art in credits:
        credit = (sanitize_artist_name(art["name"]), role)
        artists.append(credit)
        _note_variation(variations, credit, art)
        if RE_FEAT_JOIN.fullmatch((art.get("join") or "").strip()):
            role = "guest"
    return artists, variations


def _note_variation(variations, credit, art):
    """Add the name `art` is credited under on the release to `variations` when it is not the main name."""
    anv = sanitize_artist_name((art.get("anv") or "").strip())
    if anv and anv != credit[0]:
        variations.append({"name": credit[0], "role": credit[1], "anv": anv})


def sanitize_artist_name(name):
    """
    Remove parenthentical number disambiguation bullshit from artist names,
    as well as the asterisk stuff.
    """
    return strip_numbered_suffix(re.sub(r"\*+$", "", name))


def parse_source(formats):
    """
    Take the list of format strings provided by Discogs and iterate over them
    to find a possible source for the release.
    """
    for format_s, source in SOURCES.items():
        if any(format_s in f for f in formats):
            return source
