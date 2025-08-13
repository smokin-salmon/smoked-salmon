import re

from salmon.sources.base import BaseScraper


class JunodownloadBase(BaseScraper):
    url = site_url = "https://www.junodownload.com"
    search_url = "https://www.junodownload.com/search/"
    regex = re.compile(r"^https?://(?:(?:www|secure)\.)?junodownload\.com/products/[^/]+/([^/]*)/?")

    release_format = "/products/{rls_name}/{rls_id}"
