import re
from typing import Any

from salmon.sources.base import BaseScraper


class BandcampBase(BaseScraper):
    search_url = "https://bandcamp.com/search/"
    regex = re.compile(r"^https?://([^/]+)/(album|track)/([^/]+)/?")
    release_format = "https://{rls_url}/{type}/{rls_id}"

    @classmethod
    def format_url(cls, rls_id: Any, rls_name: str | None = None, url: str | None = None) -> str:
        if url:
            return url
        # rls_id is expected to be a tuple of (domain, type, id)
        return cls.release_format.format(rls_url=rls_id[0], type=rls_id[1], rls_id=rls_id[2])
