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
# The positions a disc's numbering or a record's sides start from: either seen again on a disc starts the next.
_FIRST_POSITIONS = {"1", "A1"}
# "1.1", "2.1": a disc and track when every numbered position is so, over more than one disc, on a release of more
# than one CD, naming no disc beyond the discs it states and no disc and track twice; else the dot numbers a track's
# parts ("12.1") or a work's movements ("1.1" to "2.2" on one CD, or again on each CD).
_RE_DOTTED = re.compile(r"(\d+)\.(\d+)")
# Formats that are CDs: a record's or a tape's sides are one disc, and a file release's qty counts its files.
_CD_FORMATS = {"CD", "CDr", "SACD"}
# Formats that name no medium: a file release's qty counts its files.
_NO_MEDIA = {"File", "All Media", "Box Set"}
# Video discs, which a set's dots may number on from its CDs ("3.1" on the DVD after two CDs).
_VIDEO_FORMATS = {"DVD", "DVDr", "Blu-ray", "Blu-ray-R"}


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


def _stated(soup: dict, names: set[str]) -> int:
    """How many media named in `names` a release's formats state, over all of them: a box set lists its CDs beside
    the box."""
    quantities = (str(f.get("qty") or "") for f in soup.get("formats") or [] if f.get("name") in names)
    return sum(int(qty) if qty.isdecimal() else 1 for qty in quantities)


def _dotted(soup: dict) -> bool:
    """Whether the release's dots ("1.1", "2.1") are disc and track (_RE_DOTTED says when)."""
    positions = (str(t.get("position", "")).strip() for t in soup["tracklist"] if t["type_"] == "track")
    matches = [_RE_DOTTED.fullmatch(position) for position in positions if _numbered(position)]
    pairs = [(int(match[1]), int(match[2])) for match in matches if match]
    leads = {lead for lead, _ in pairs}
    return (
        all(matches)
        and len(set(pairs)) == len(pairs)
        and len(leads) > 1
        and _stated(soup, _CD_FORMATS) > 1
        and max(leads) <= _stated(soup, _CD_FORMATS | _VIDEO_FORMATS)
    )


def _disc_and_number(track: dict, dotted: bool) -> tuple[int | None, str]:
    """A track's disc (None when its position names none) and track number."""
    position = str(track.get("position", ""))
    return _clean_discogs_position(position.replace(".", "-") if dotted else position)


def _why_a_disc_starts(number: str, listed: dict[str, int], headings: int, after_heading: bool) -> str | None:
    """Why a track numbered `number`, its position naming no disc, starts the next disc, or None when it doesn't.

    `listed` holds the disc's positions so far, each with how many headings came before it was first listed. A heading
    alone starts no disc: headings also group one disc's tracks (a record's side, a composer's works).
    """
    if number in listed and number in _FIRST_POSITIONS:
        return f"{number!r} again"
    # An unnumbered position ("-") only before the heading's first numbered track: later, it is one more "-".
    if number in listed and listed[number] < headings and (after_heading or _numbered(number)):
        return f"{number!r} again after a heading"
    # Only as a heading's first numbered track: later under it, going back is more likely a typo ("2:15" for "2-15").
    # A part goes back by its number, then its letter: "1a" after "2" does, "3a" after "2" and "1b" after "1a" don't.
    place = _place(number)
    if (
        after_heading
        and place is not None
        and any((other := _place(seen)) is not None and other >= place for seen in listed)
    ):
        return f"numbering back to {number!r} after a heading"
    return None


def _disc_starts(tracklist: list[dict], dotted: bool) -> dict[int, str]:
    """Where each disc that no position names starts, by its index in the tracklist, and why.

    A disc starts at the latest heading when the heading came partway through the disc before, so the tracks under it
    go with it ("A", "B", heading, "AA", "B"): except when numbering starts over, later under the heading, at the
    disc's first numbered position, which starts the disc there and leaves a "Bonus" heading's tracks on the disc
    before. A track whose position names its disc keeps it, so a heading over one starts no disc.
    """
    starts: dict[int, str] = {}
    disc, headings, heading_at, after_heading, named_under = 1, 0, 0, False, False
    listed: defaultdict[int, dict[str, int]] = defaultdict(dict)
    for index, entry in enumerate(tracklist):
        if entry["type_"] == "heading":
            headings, heading_at, after_heading, named_under = headings + 1, index, True, False
            continue
        if entry["type_"] != "track":
            continue
        named, number = _disc_and_number(entry, dotted)
        if named is not None:
            disc, named_under = named, True
        elif why := _why_a_disc_starts(number, listed[disc], headings, after_heading):
            on_disc = listed[disc]
            under = {position: seen for position, seen in on_disc.items() if seen == headings}
            first = next((position for position in on_disc if _numbered(position)), None)
            # Not a heading at or before the latest start: a disc named out of order ("2-2" before "1-2") can leave one.
            at_heading = (
                not named_under
                and heading_at > max(starts, default=-1)
                and len(under) < len(on_disc)
                and (after_heading or number != first)
            )
            starts[heading_at if at_heading else index] = why
            disc += 1
            if at_heading:
                listed[disc].update(under)
                for position in under:
                    del on_disc[position]
        listed[disc].setdefault(number, headings)
        # An unnumbered track ("-") after a heading leaves the heading's checks to the next.
        after_heading = after_heading and not _numbered(number)
    return starts


def _disc_starts_on(soup: dict, dotted: bool) -> dict[int, str]:
    """Where each disc starts on a release, and why: as its positions say (_disc_starts), and, when no position names
    a disc and they start fewer discs than its formats state, at its first other headings between tracks, up to that
    count: "1" to "3", "CD 2", "4" to "6" on two CDs. When its formats name no medium (a file release), at each of
    them, as on master. Headings in a row count once, at the first ("CD 2", "Act 2"), and not at all when the positions
    start a disc at one of them already. A heading that starts no disc of its own ("Bonus") is a smaller harm than a
    missing disc: the merge keeps the files' numbers when a disc's don't run from 1, and renumbers a later disc's files
    after the one before when they are on one."""
    tracklist = soup["tracklist"]
    starts = _disc_starts(tracklist, dotted)
    tracks = [index for index, entry in enumerate(tracklist) if entry["type_"] == "track"]
    if not tracks or any(_disc_and_number(tracklist[index], dotted)[0] is not None for index in tracks):
        return starts
    between = [
        index
        for index in range(tracks[0] + 1, tracks[-1])
        if tracklist[index]["type_"] == "heading" and tracklist[index - 1]["type_"] != "heading"
    ]
    formats = soup.get("formats") or []
    if formats and all(f.get("name") in _NO_MEDIA for f in formats):
        discs, why = len(between) + 1, "a heading, with no media stated"
    else:
        discs = _stated(soup, _CD_FORMATS | _VIDEO_FORMATS)
        why = f"a heading, with {discs} discs stated"
    # Not a run of headings the positions already start a disc in ("CD 2", "Act 1", "1"): that would skip a disc.
    free = [index for index in between if starts.keys().isdisjoint(range(index, min(t for t in tracks if t > index)))]
    more = free[: max(discs - 1 - len(starts), 0)]
    return {**starts, **dict.fromkeys(more, why)}


def _numbered(position: str) -> bool:
    """Whether a position numbers its track at all: "-" and "" don't."""
    return any(char.isalnum() for char in position)


def _place(number: str) -> tuple[int | str, ...] | None:
    """A track number's place in a numbering of parts, each run of digits by its value: (1, "B") for "1B", (2,) for
    "2", (1, ".", 10) for "1.10", after "1.9", or None when it starts with no number ("A1", "-")."""
    if not number[:1].isdecimal():
        return None
    return tuple(int(run) if run.isdecimal() else run for run in re.findall(r"\d+|\D+", number))


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
        dotted = _dotted(soup)
        # Each start's reason is for the tests, and for saying why a release has its discs when that is wanted.
        starts = _disc_starts_on(soup, dotted)
        disc_num = 1
        for index, track in enumerate(soup["tracklist"]):
            if index in starts:
                disc_num += 1
            if track["type_"] == "track":
                named, track_num = _disc_and_number(track, dotted)
                disc_num = disc_num if named is None else named
                disc_str = str(disc_num)

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
