import re
from typing import Any

import musicbrainzngs

from .base import BaseScraper

musicbrainzngs.set_useragent("salmon", "1.0", "noreply@salm.on")


class MusicBrainzBase(BaseScraper):
    url = site_url = "https://musicbrainz.org"
    release_format = "/release/{rls_id}"
    regex = re.compile(r"^https?://(?:www\.)?musicbrainz.org/release/([a-f0-9\-]+)$")

    async def create_soup(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> dict[str, Any]:
        """Fetch release data from MusicBrainz API.

        Args:
            url: The MusicBrainz release URL.
            params: Unused, kept for API compatibility.
            headers: Unused, kept for API compatibility.
            follow_redirects: Unused, kept for API compatibility.

        Returns:
            Release data dict from MusicBrainz.

        Raises:
            ValueError: If URL is invalid.
        """
        match = re.search(r"/release/([a-f0-9\-]+)$", url)
        if not match:
            raise ValueError("Invalid MusicBrainz URL.")
        rls_id = match[1]
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
