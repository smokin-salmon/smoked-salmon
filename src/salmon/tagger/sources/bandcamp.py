import contextlib
import re
from collections import defaultdict
from datetime import datetime

from salmon.common import RE_FEAT, fetch_genre, re_split
from salmon.errors import GenreNotInWhitelist, ScrapeError
from salmon.sources import BandcampBase
from salmon.tagger.sources.base import MetadataMixin

CATNO_PREFIX_RE = re.compile(r"^(?P<catno>(?=.*\d)[A-Za-z0-9][A-Za-z0-9 ._-]{1,31})\s*/\s*(?P<title>.+)$")
BRACKETED_CATNO_PREFIX_RE = re.compile(r"^[\[(](?P<catno>(?=.*\d)[A-Za-z0-9][A-Za-z0-9 ._-]{1,31})[\])]\s*(?P<title>.+)$")
TITLE_ARTIST_PREFIX_RE = re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<title>.+)$")
TRACK_SIDE_PREFIX_RE = re.compile(r"^(?P<prefix>[A-Z]{1,3}\d{1,2}[A-Z]?)\s+(?P<artist>.+)$", re.IGNORECASE)


class Scraper(BandcampBase, MetadataMixin):
    def parse_release_title(self, soup):
        try:
            return resolve_release_context(soup)["title"]
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
            context = resolve_release_context(soup)
            label = context["account_title"]
            if label and normalize_release_key(context["artist"]) != normalize_release_key(label):
                return label
        except IndexError as e:
            raise ScrapeError("Could not parse record label.") from e

    def parse_release_catno(self, soup):
        context = resolve_release_context(soup)
        artist = context["artist"]
        catno = context["catno"]
        clean_title = context["title"]
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
        context = resolve_release_context(soup)
        artist = context["artist"]
        release_title = context["title"]
        tracklist_scrape = soup.select("#track_table tr.track_row_view")
        if tracklist_scrape:
            for track in tracklist_scrape:
                try:
                    num = track.select(".track-number-col .track_number")[0].text.rstrip(".")
                    title = track.select('.title-col span[class="track-title"]')[0].string
                    track_artists, strip_artist_prefix = parse_artists(artist, title, release_title=release_title)
                    tracks["1"][num] = self.generate_track(
                        trackno=int(num),
                        discno=1,
                        artists=track_artists,
                        title=parse_title(title, strip_artist_prefix=strip_artist_prefix),
                    )
                except (ValueError, IndexError, TypeError) as e:
                    raise ScrapeError("Could not parse tracks.") from e
        else:
            tracks["1"]["1"] = self.generate_track(
                trackno=1,
                discno=1,
                artists=[(a, "main") for a in re_split(artist)],
                title=release_title,
            )
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


def parse_account_title(soup):
    title = soup.select_one("#band-name-location .title")
    if title:
        return title.get_text(strip=True)
    return None


def extract_catno_and_title(raw_title, artist=None):
    title = raw_title.strip()
    match = CATNO_PREFIX_RE.match(title) or BRACKETED_CATNO_PREFIX_RE.match(title)
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


def resolve_release_context(soup):
    raw_title = parse_raw_release_title(soup)
    page_artist = parse_page_artist(soup)
    account_title = parse_account_title(soup)
    catno, clean_title = extract_catno_and_title(raw_title, page_artist)
    release_artist = page_artist
    release_title = clean_title

    if (
        page_artist
        and account_title
        and normalize_release_key(page_artist) == normalize_release_key(account_title)
    ):
        split_title = split_artist_prefixed_title(clean_title)
        if split_title and normalize_release_key(split_title[0]) != normalize_release_key(page_artist):
            release_artist, release_title = split_title

    return {
        "artist": release_artist,
        "title": release_title,
        "catno": catno,
        "account_title": account_title,
    }


def split_artist_prefixed_title(title):
    match = TITLE_ARTIST_PREFIX_RE.match(title or "")
    if not match:
        return None
    return match["artist"].strip(), match["title"].strip()


def strip_track_side_prefix(track_artists):
    match = TRACK_SIDE_PREFIX_RE.match(track_artists.strip())
    if not match:
        return track_artists.strip()
    return match["artist"].strip()


def parse_artists(artist, title, release_title=None):
    """
    Parse guest artists from the title and add them to the list
    of artists as guests.
    """
    feat_artists = RE_FEAT.search(title)
    artists = []
    strip_artist_prefix = False
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
        normalized_track_artist = normalize_release_key(track_artists)
        normalized_release_title = normalize_release_key(release_title) if release_title else ""
        normalized_release_artist = normalize_release_key(artist)
        if normalized_track_artist not in {normalized_release_title, normalized_release_artist}:
            if "various" in artist.lower():
                track_artists = strip_track_side_prefix(track_artists)
            artists += [(a, "main") for a in re_split(track_artists)]
            strip_artist_prefix = True
    except (IndexError, TypeError):
        pass
    if "various" not in artist.lower():
        for a in re_split(artist):
            if (a, "main") not in artists:
                artists.append((a, "main"))

    return artists, strip_artist_prefix


def parse_title(title, strip_artist_prefix=False):
    """Strip featuring artists from title; they belong with artists."""
    if strip_artist_prefix and " - " in title:
        title = title.split(" - ", 1)[1]
    return RE_FEAT.sub("", title).rstrip()
