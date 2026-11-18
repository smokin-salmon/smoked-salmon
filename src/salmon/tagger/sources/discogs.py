import re
from collections import defaultdict

from salmon.common import RE_FEAT_JOIN
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


# A disc and what follows it: "2-15", a box set's "CD2-15", or "2-1a", a track's part. A video disc's ("DVD1-1")
# stays as written, so its entries never take a CD's track numbers.
_RE_DISC_TRACK = re.compile(r"(?:CD|SACD)?(\d+)-(\S+)")
# A disc's first position: seen again on that disc, its numbering or a record's sides start again.
_FIRST_POSITIONS = {"1", "A1"}
# "1.1", "2.1": a disc and track when the whole tracklist is numbered so; else the dot numbers a track's parts ("12.1").
_RE_DOTTED = re.compile(r"\d+\.\d+")


def _clean_discogs_position(position: str) -> tuple[int | None, str]:
    """A Discogs position's disc (None when it names none) and track number.

    Examples:
        '1-01'   -> (1, '1')
        'CD2-15' -> (2, '15')
        '2-1a'   -> (2, '2-1A')
        '01'     -> (None, '1')
        'A1'     -> (None, 'A1')
    """
    pos = str(position).strip().upper()
    if match := _RE_DISC_TRACK.fullmatch(pos):
        return int(match[1]), (str(int(match[2])) if match[2].isdecimal() else pos)
    return None, (str(int(pos)) if pos.isdecimal() else pos)


def _starts_a_disc(number: str, on_disc: dict, after_heading: bool) -> bool:
    """Whether a track numbered `number`, its position naming no disc, starts the next one: the disc's first position
    seen again on it, or, after a heading, numbering that goes back ("1" after "12", a side's "A" again). A heading
    alone starts none: headings also group one disc's tracks (a record's side, a composer's works)."""
    if number in on_disc and (after_heading or number in _FIRST_POSITIONS):
        return True
    return after_heading and number.isdecimal() and any(n.isdecimal() and int(n) >= int(number) for n in on_disc)


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
        dotted = all(
            _RE_DOTTED.fullmatch(str(t.get("position", "")).strip()) for t in soup["tracklist"] if t["type_"] == "track"
        )
        after_heading = False
        for track in soup["tracklist"]:
            if track["type_"] == "heading":
                after_heading = True
            elif track["type_"] == "track":
                position = str(track.get("position", ""))
                disc, track_num = _clean_discogs_position(position.replace(".", "-") if dotted else position)
                if disc is not None:
                    cur_disc = disc
                elif _starts_a_disc(track_num, tracks.get(str(cur_disc), {}), after_heading):
                    cur_disc += 1
                # An unnumbered track ("-") after a heading leaves the restart to the next.
                after_heading = after_heading and not any(char.isalnum() for char in track_num)
                disc_str = str(cur_disc)
                disc_num = cur_disc

                artists, variations = parse_credits(soup["artists"], track)
                tracks[disc_str][track_num] = self.generate_track(
                    trackno=track_num,
                    discno=disc_num,
                    artists=artists,
                    title=track["title"],
                    duration=parse_duration(track.get("duration")),
                    # Each under the name the release credits it too (the ANV), which the files may use instead.
                    credited_remixers=[
                        *(name for name, role in artists if role == "remixer"),
                        *(v["anv"] for v in variations if v["role"] == "remixer"),
                    ],
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
