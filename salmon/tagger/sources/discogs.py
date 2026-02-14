import re
from collections import defaultdict

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

SOURCES = {
    "Vinyl": "Vinyl",
    "File": "WEB",
    "CD": "CD",
}


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
                track_num = track["position"].upper()
                tracks[str(cur_disc)][track_num] = self.generate_track(
                    trackno=track_num,
                    discno=cur_disc,
                    artists=parse_artists(soup["artists"], track),
                    title=track["title"],
                )
        return dict(tracks)


def parse_artists(artist_soup, track):
    """
    Generate the artists list from the artist dictionary provided with
    each track.
    """
    if "artists" in track:
        artists = [*((sanitize_artist_name(art["name"]), "main") for art in track["artists"])]
    else:
        artists = [*((sanitize_artist_name(art["name"]), "main") for art in artist_soup if art["name"] != "Various")]
    if "extraartists" in track:
        for art in track["extraartists"]:
            for role in art["role"].split(","):
                role = role.strip()
                if role in ROLES:
                    artists.append((sanitize_artist_name(art["name"]), ROLES[role]))
        for name, role in artists:
            if role != "main" and (name, "main") in artists:
                artists.remove((name, "main"))
    #        for a, i in [
    #            (a, i) for a, i in artists if i != "main" and (a, "main") in artists
    #        ]:
    #            artists.remove((a, "main"))
    return artists


def sanitize_artist_name(name):
    """
    Remove parenthentical number disambiguation bullshit from artist names,
    as well as the asterisk stuff.
    """
    name = re.sub(r" \(\d+\)$", "", name)
    return re.sub(r"\*+$", "", name)


def parse_source(formats):
    """
    Take the list of format strings provided by Discogs and iterate over them
    to find a possible source for the release.
    """
    for format_s, source in SOURCES.items():
        if any(format_s in f for f in formats):
            return source
