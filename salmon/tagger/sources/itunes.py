import json
import re
from collections import defaultdict

from salmon.common import RE_FEAT, parse_copyright
from salmon.errors import ScrapeError
from salmon.sources import iTunesBase
from salmon.tagger.sources.base import MetadataMixin

ALIAS_GENRE = {
    "Hip-Hop/Rap": {"Hip Hop", "Rap"},
    "R&B/Soul": {"Rhythm & Blues", "Soul"},
    "Music": {},  # Aliasing Music to an empty set because we don't want a genre 'music'
}


class Scraper(iTunesBase, MetadataMixin):
    def parse_release_title(self, soup):
        try:
            title = soup.find("meta", {"name": "apple:title"})["content"].strip()
            return RE_FEAT.sub("", title)
        except (TypeError, IndexError) as e:
            raise ScrapeError("Failed to parse scraped title.") from e

    def parse_cover_url(self, soup):
        try:
            # Just choosing the last artwork url here.
            return soup.find("meta", {"property": "og:image"})["content"].strip()
        except (TypeError, IndexError) as e:
            raise ScrapeError("Could not parse cover URL.") from e

    def parse_genres(self, soup):
        try:
            info = json.loads(soup.find("script", {"id": "schema:music-album"}).text)
            genres = {g for gs in info["genre"] for g in ALIAS_GENRE.get(gs, [gs])}
            # either replace with alias (which can be more than one tag) or return untouched.
            return genres
        except (TypeError, IndexError) as e:
            raise ScrapeError("Could not parse genres.") from e

    def parse_release_year(self, soup):
        try:
            date = self.parse_release_date(soup)
            match = re.search(r"(\d{4})", date) if date else None
            if not match:
                raise ScrapeError("Could not parse release year.")
            return int(match[1])
        except TypeError as e:
            raise ScrapeError("Could not parse release year.") from e

    def parse_release_type(self, soup):
        try:
            title = soup.find("meta", {"name": "apple:title"})["content"].strip()
            if re.match(r".*\sEP$", title, re.IGNORECASE):
                return "EP"
            if re.match(r".*\sSingle$", title, re.IGNORECASE):
                return "Single"
            return "Album"
        except TypeError as e:
            raise ScrapeError("Could not parse release type.") from e

    def parse_release_date(self, soup):
        # This can't be enough. Can it?
        try:
            date_string = soup.find(attrs={"property": "music:release_date"})["content"].split("T")[0]
            return date_string
        except BaseException:
            return None

    def parse_release_label(self, soup):
        try:
            json.loads(soup.find("script", {"id": "serialized-server-data"}).text)
            copyright = soup.find("p", {"data-testid": "tracklist-footer-description"}).text
            return parse_copyright(copyright)
        except IndexError as e:
            raise ScrapeError("Could not parse record label.") from e

    def parse_comment(self, soup):
        try:
            return soup.select(".product-hero-desc .product-hero-desc__section > p")[0]["aria-label"].strip()
        except IndexError:
            return None

    def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        cur_disc = 1

        # Find and parse JSON data from <script> tag
        script_tag = soup.find("script", {"type": "application/ld+json"})
        if not script_tag:
            raise ScrapeError("JSON-LD script not found. Scraping needs to be updated")

        try:
            data = json.loads(script_tag.string)
        except json.JSONDecodeError as e:
            raise ScrapeError("Failed to decode JSON data.") from e

        if "tracks" not in data:
            raise ScrapeError("Tracks data not found in JSON.")

        for index, track in enumerate(data["tracks"], start=1):
            try:
                num = index
                raw_title = track.get("name", "").strip()
                title = RE_FEAT.sub("", raw_title)

                # TODO: handle explicit + discnumber + artists (if available, will have to modify num too)
                # explicit = track.get("isExplicit", False)

                # Increment disc number if necessary
                # if int(num) == 1 and num in tracks[str(cur_disc)]:
                #    cur_disc += 1

                tracks[str(cur_disc)][num] = self.generate_track(
                    trackno=num,
                    discno=cur_disc,
                    artists=[],
                    # artists=parse_artists(soup, track, raw_title),
                    title=title,
                    # explicit=explicit,
                )
            except (ValueError, KeyError) as e:
                raise ScrapeError("Could not parse tracks.") from e

        return dict(tracks)


def parse_artists(soup, track, title):
    """
    Parse all the artists from various locations and compile a split
    list of all of them.  This is not foolproof, but better than the
    alternative. Artists such as Vintage & Morelli will fuck this up,
    but that is why we have manual confirmation for metadata.
    """
    header_artists = parse_artists_header(soup)
    track_artists = parse_artists_track(track)
    title_artists = parse_artists_title(title)
    return reconcile_artists(header_artists, track_artists, title_artists)


def parse_artists_header(soup):
    """Parse the artists listed in the header as artists of the release."""
    artists = []
    try:
        release_artists = soup.select(".product-creator")[0].a.string.strip()
    except (TypeError, IndexError):
        return artists

    if re.match(r"[^,]+, [^&]+ (& [^&]+)+", release_artists):
        first_artist, rest = release_artists.split(",", 1)
        artists.append(first_artist)
        for a in rest.split("&"):
            a = a.strip()
            if a not in artists:
                artists.append(a)
    elif "&" in release_artists:
        for a in release_artists.split("&"):
            a = a.strip()
            if a not in artists:
                artists.append(a)
    else:
        artists.append(release_artists.strip())
    return artists


def parse_artists_track(track):
    """Parse the artists listed per-track, below the track title."""
    track_block = track.select(".by-line.typography-caption")
    if len(track_block) == 1:
        biline = track_block[0].text.strip().replace("\n", ", ")
        if biline[0:3] == "By ":
            return _parse_artists_commas(biline[2:])
        else:
            return _parse_artists_commas(biline)
    return []


def parse_artists_title(title):
    """Parse the guest artists from the track title."""
    feat = RE_FEAT.search(title)
    if feat:
        return _parse_artists_commas(feat[1])
    return set()


def _parse_artists_commas(artiststr):
    """
    Parse the artist names when they begin with commas and end with one
    ampersand. Split and strip them.
    """
    artists = []
    res = re.match(r"([^,]+)((?:, [^,&]+)+) & (.+)$", artiststr)
    if res:
        artists = [res[1].strip()] + [r.strip() for r in res[2].split(",") if r.strip()]
        for a in artists:
            if a not in artists:
                artists.append(a)
        artists.append(res[3].strip())
    elif "&" in artiststr:
        for a in artiststr.split("&"):
            a = a.strip()
            if a not in artists:
                artists.append(a)
    else:
        artists.append(artiststr.strip())
    return artists


def reconcile_artists(headers, tracks, titles):
    """De-duplicate the scraped artists and return a completed list."""
    artists = []
    for artist in tracks if tracks else headers:
        if (artist, "main") not in artists:
            artists.append((artist, "main"))
    for artist in titles:
        if (artist, "guest") not in artists:
            artists.append((artist, "guest"))
    return artists
