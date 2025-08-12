import re

from salmon.sources.base import BaseScraper


class BandcampBase(BaseScraper):
    search_url = "https://bandcamp.com/search/"
    regex = re.compile(r"^https?://([^/]+)/(album|track)/([^/]+)/?")
    release_format = "https://{rls_url}/{type}/{rls_id}"

    @classmethod
    def format_url(cls, rls_id, rls_name=None):
        # rls_id is now expected to be a tuple of (domain, type, id)
        return cls.release_format.format(rls_url=rls_id[0], type=rls_id[1], rls_id=rls_id[2])
