import contextlib
import re
from collections import defaultdict
from datetime import datetime

from salmon.common import RE_FEAT, fetch_genre, re_split
from salmon.errors import GenreNotInWhitelist, ScrapeError
from salmon.sources import BandcampBase
from salmon.tagger.sources.base import MetadataMixin

CATNO_PREFIX_RE = re.compile(r"^(?P<catno>(?=.*\d)[A-Za-z0-9][A-Za-z0-9 ._-]{1,31})\s*/\s*(?P<title>.+)$")


class Scraper(BandcampBase, MetadataMixin):
    def parse_release_title(self, soup):
        try:
            artist = parse_page_artist(soup)
            _, title = extract_catno_and_title(parse_raw_release_title(soup), artist)
            return title
        except (TypeError, IndexError) as e:
            raise ScrapeError("Failed to parse scraped title.") from e

    def parse_cover_url(self, soup):
        try:
            return soup.select("#tralbumArt img")[0]["src"]
        except (TypeError, IndexError) as e:
            raise ScrapeError("Could not parse cover URL.") from e

    def parse_genres(self, soup):
        genres = set()
        try:
            for a in soup.select(".tralbumData.tralbum-tags a"):
                with contextlib.suppress(GenreNotInWhitelist):
                    genres |= fetch_genre(a.string)
            return genres
        except TypeError as e:
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

    def parse_release_date(self, soup):
        try:
            match = re.search(
                r"release(?:d|s) ([^\d]+ \d+, \d{4})",
                soup.select(".tralbumData.tralbum-credits")[0].text,
            )
            if not match:
                raise ScrapeError("Could not parse release date.")
            date = match[1]
            return datetime.strptime(date, "%B %d, %Y").strftime("%Y-%m-%d")
        except (TypeError, IndexError, ValueError) as e:
            raise ScrapeError("Could not parse release date.") from e

    def parse_release_label(self, soup):
        try:
            artist = parse_page_artist(soup)
            label = soup.select("#band-name-location .title")[0].string
            if artist != label:
                return label
        except IndexError as e:
            raise ScrapeError("Could not parse record label.") from e

    def parse_release_catno(self, soup):
        artist = parse_page_artist(soup)
        raw_title = parse_raw_release_title(soup)
        catno, clean_title = extract_catno_and_title(raw_title, artist)
        if catno:
            return catno

        current_key = normalize_release_key(clean_title)
        artist_key = normalize_release_key(artist)
        for album in soup.select("li.recommended-album.footer-ar[data-albumtitle][data-artist]"):
            footer_title = album.get("data-albumtitle", "").strip()
            footer_artist = album.get("data-artist", "").strip()
            footer_catno, footer_clean_title = extract_catno_and_title(footer_title, footer_artist)
            if (
                footer_catno
                and normalize_release_key(footer_artist) == artist_key
                and normalize_release_key(footer_clean_title) == current_key
            ):
                return footer_catno
        return None

    async def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        artist = parse_page_artist(soup)
        various = artist
        tracklist_scrape = soup.select("#track_table tr.track_row_view")
        if tracklist_scrape:
            for track in tracklist_scrape:
                try:
                    num = track.select(".track-number-col .track_number")[0].text.rstrip(".")
                    title = track.select('.title-col span[class="track-title"]')[0].string
                    tracks["1"][num] = self.generate_track(
                        trackno=int(num),
                        discno=1,
                        artists=parse_artists(artist, title),
                        title=parse_title(title, various=various),
                    )
                except (ValueError, IndexError, TypeError) as e:
                    raise ScrapeError("Could not parse tracks.") from e
        else:
            try:
                title = self.parse_release_title(soup)
                tracks["1"]["1"] = self.generate_track(
                    trackno=1,
                    discno=1,
                    artists=parse_artists(artist, title),
                    title=parse_title(title, various=various),
                )
            except (ValueError, TypeError) as e:
                raise ScrapeError("Could not parse single track.") from e
        return dict(tracks)


def parse_raw_release_title(soup):
    try:
        return soup.select("#name-section .trackTitle")[0].string.strip()
    except (TypeError, IndexError) as e:
        raise ScrapeError("Failed to parse scraped title.") from e


def parse_page_artist(soup):
    for div in soup.select("#name-section"):
        span = div.find("span")
        if span:
            return span.text.strip()
    return ""


def extract_catno_and_title(raw_title, artist=None):
    title = raw_title.strip()
    match = CATNO_PREFIX_RE.match(title)
    if not match:
        return None, title

    clean_title = match["title"].strip()
    if artist:
        artist_match = re.match(rf"^{re.escape(artist)}\s*-\s*(?P<title>.+)$", clean_title, flags=re.IGNORECASE)
        if artist_match:
            clean_title = artist_match["title"].strip()
    return match["catno"].strip(), clean_title


def normalize_release_key(text):
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def parse_artists(artist, title):
    """
    Parse guest artists from the title and add them to the list
    of artists as guests.
    """
    feat_artists = RE_FEAT.search(title)
    artists = []
    if feat_artists:
        feat_part = feat_artists[1].split(" - ", 1)[0]  # Match only until ' - '
        artists = [(a, "guest") for a in re_split(feat_part)]
    try:
        if " - " not in title:
            raise IndexError
        track_artists = title.split(" - ", 1)[0]
        if feat_artists:
            # Remove featuring artists from track artists
            track_artists = track_artists.replace(feat_artists[0].split(" - ", 1)[0], "").strip()
        artists += [(a, "main") for a in re_split(track_artists)]
    except (IndexError, TypeError):
        pass
    if "various" not in artist.lower():
        for a in re_split(artist):
            if (a, "main") not in artists:
                artists.append((a, "main"))

    return artists


def parse_title(title, various):
    """Strip featuring artists from title; they belong with artists."""
    if various and " - " in title:
        title = title.split(" - ", 1)[1]
    return RE_FEAT.sub("", title).rstrip()
