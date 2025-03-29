import re

import musicbrainzngs

from .base import BaseScraper

musicbrainzngs.set_useragent("salmon", "1.0", "noreply@salm.on")


class MusicBrainzBase(BaseScraper):

    url = site_url = "https://musicbrainz.org"
    release_format = "/release/{rls_id}"
    regex = re.compile(r"^https?://(?:www\.)?musicbrainz.org/release/([a-z0-9\-]+)$")

    async def create_soup(self, url):
        rls_id = re.search(r"/release/([a-f0-9\-]+)$", url)[1]
        return musicbrainzngs.get_release_by_id(
            rls_id,
            [
                "artists",
                "labels",
                "recordings",
                "release-groups",
                "media",
                "artist-credits",
                "artist-rels",
                "recording-level-rels",
            ],
        )["release"]
