import re
from collections import defaultdict
from datetime import datetime

from salmon.common import re_split
from salmon.errors import ScrapeError
from salmon.sources import JunodownloadBase
from salmon.tagger.sources.base import MetadataMixin


class Scraper(JunodownloadBase, MetadataMixin):
    def parse_release_title(self, soup):
        try:
            return soup.select(".product-title a")[0].string
        except (TypeError, IndexError) as e:
            raise ScrapeError("Failed to parse scraped title.") from e

    def parse_cover_url(self, soup):
        try:
            return (
                soup.select("img.img-release")[0]["src"][::-1]
                .replace("MED"[::-1], "BIG"[::-1], 1)[::-1]
                .replace("/300/", "/full/")
            )
        except (TypeError, IndexError) as e:
            raise ScrapeError("Could not parse cover URL.") from e

    def parse_genres(self, soup):
        try:
            genre_str = re.sub(r"[^A-Za-z]+$", "", soup.select('meta[itemprop="genre"]')[0]["content"])
            return {"Electronic", *(set(genre_str.split("/")))}
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
            date = soup.select('span[itemprop="datePublished"]')[0].string.strip()
            return datetime.strptime(date, "%d %B, %Y").strftime("%Y-%m-%d")
        except (IndexError, AttributeError) as e:
            raise ScrapeError("Could not parse release date.") from e

    def parse_release_label(self, soup):
        try:
            return soup.select(".product-label a")[0].string
        except IndexError as e:
            raise ScrapeError("Could not parse record label.") from e

    def parse_release_catno(self, soup):
        try:
            catblob = soup.find_all("div", attrs={"class": "mb-2"})[10]
            return catblob.find("strong", text="Cat:").next_sibling.strip().replace(" ", "")
        except IndexError as e:
            raise ScrapeError("Could not parse catalog number.") from e

    def parse_comment(self, soup):
        try:
            return soup.select('#product_release_note span[itemprop="reviewBody"]')[0].string
        except IndexError:
            return None

    def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        cur_disc = 1
        for track in soup.find_all(
            "div",
            attrs={"class": "row gutters-sm align-items-center product-tracklist-track"},
        ):
            try:
                num = track.text.strip().split(".", 1)[0]
                tobj = track.find("div", attrs={"class": "col track-title"})
                title = tobj.find("a").text
                tracks[str(cur_disc)][num] = self.generate_track(
                    trackno=(num),
                    discno=cur_disc,
                    artists=parse_artists(soup, track, title),
                    title=parse_title(title, track),
                )
            except (ValueError, IndexError) as e:
                raise ScrapeError("Could not parse tracks.") from e
        return dict(tracks)


def parse_title(title, track):
    """Parse the track title from the HTML."""
    try:
        artist = track.select('meta[itemprop="byArtist"]')[0]["content"]
        title = title.split(artist, 1)[1].lstrip(" -")
    except (TypeError, IndexError):
        pass
    # A bit convoluted so we can have `(feat artist - Club edit)` --> `- Club edit`
    return (
        re.sub(
            r"( -)? \(?(original mix|feat [^((?! - ).)]+|album mix)\)?",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()
        # .rstrip(")") Why was this here?
    )


def parse_artists(soup, track, title):
    """
    Parse the per-track artists from the tracks or the header."""
    raw_rls_arts = [s.string for s in soup.select("#topbar_bread a") if "/artists/" in s["href"] and s.string] or [
        s.string.title() for s in soup.select(".product-artist a")
    ]

    artists = []
    for art in raw_rls_arts:
        for split in re_split(art):
            artists.append(split)

    try:
        artists = split_artists(track.select('meta[itemprop="byArtist"]')[0]["content"], artists)
    except (TypeError, IndexError):
        artists = [(a, "main") for a in artists]

    guests = re.search(r"[Ff]eat\.? ([^\)]+)", title)
    if guests:
        artists += [
            (
                re.sub(r"(?:(?:\s*-)?\s+.+?\s+(?:mix|remix|edit)|(?:\s*-\s*.+))", "", a, flags=re.IGNORECASE).strip(),
                "guest",
            )
            for a in re_split(guests[1])
        ]

    return artists


def split_artists(artist, rls_artists):
    """
    Split an artist string by known delimiter characters and compare them
    to the album artists. If any release artists match the split artists,
    return the release artists that matched.
    """
    art_li = [a.strip() for a in re_split(artist) if a]
    rls_artists = [a.lower() for a in rls_artists]
    return [(r, "main") for r in art_li]
