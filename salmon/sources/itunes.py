import re

from .base import BaseScraper


class iTunesBase(BaseScraper):
    is_json_api = False
    url = site_url = "https://itunes.apple.com"
    search_url = "https://itunes.apple.com/search"
    regex = re.compile(r"^https?://(itunes|music)\.apple\.com/(?:(\w{2,4})/)?album/(?:[^/]*/)?([^\?]+)")
    release_format = "/us/album/{rls_id}"
