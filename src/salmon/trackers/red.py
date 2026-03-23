import asyncclick as click
from bs4 import BeautifulSoup

from salmon import cfg
from salmon.common import UploadFiles
from salmon.errors import LoginError
from salmon.trackers.base import BaseGazelleApi


def _get_input_value(soup: BeautifulSoup, name: str) -> str | None:
    """Get the value attribute of an input element by name.

    Args:
        soup: Parsed BeautifulSoup document.
        name: The input name attribute.

    Returns:
        The value string, or None if input not found or value is empty.
    """
    tag = soup.select_one(f'input[name="{name}"]')
    if tag is None:
        return None
    val = tag.get("value", "")
    return str(val) or None


def _get_select_value(soup: BeautifulSoup, name: str) -> str | None:
    """Get the selected option value of a select element by name.

    Args:
        soup: Parsed BeautifulSoup document.
        name: The select name attribute.

    Returns:
        The selected option value, or None if not found.
    """
    selected = soup.select_one(f'select[name="{name}"] option[selected]')
    if selected is None:
        return None
    val = selected.get("value", "")
    return str(val) or None


def _get_textarea_value(soup: BeautifulSoup, name: str) -> str | None:
    """Get the text content of a textarea element by name.

    Args:
        soup: Parsed BeautifulSoup document.
        name: The textarea name attribute.

    Returns:
        The textarea text, or None if not found or empty.
    """
    tag = soup.select_one(f'textarea[name="{name}"]')
    if tag is None:
        return None
    text = tag.get_text()
    return text or None


def _parse_upload_form(data: dict, soup: BeautifulSoup) -> None:
    """Extract pre-filled fields from the upload.php form HTML.

    Args:
        data: Upload form data dict (mutated in place).
        soup: Parsed BeautifulSoup of the upload page.
    """
    # --- Artists and importance ---
    # Each artist row has an <input name="artists[]"> immediately
    # followed by a sibling <select name="importance[]">.  We iterate
    # row-wise so the two lists stay in lockstep.
    artist_names: list[str] = []
    artist_importances: list[int] = []
    for input_tag in soup.select('input[name="artists[]"]'):
        name = str(input_tag.get("value", ""))
        if not name:
            continue
        importance = 1  # default: Main
        sibling_select = input_tag.find_next_sibling("select", attrs={"name": "importance[]"})
        if sibling_select is not None:
            selected = sibling_select.select_one("option[selected]")
            if selected is not None:
                val = str(selected.get("value", ""))
                if val:
                    importance = int(val)
        artist_names.append(name)
        artist_importances.append(importance)

    if artist_names:
        data["artists[]"] = artist_names
        data["importance[]"] = artist_importances

    # --- Simple text inputs ---
    for field in ("title", "year", "tags", "image"):
        val = _get_input_value(soup, field)
        if val is not None:
            data[field] = val

    # --- Select (dropdown) ---
    val = _get_select_value(soup, "releasetype")
    if val is not None:
        data["releasetype"] = val

    # --- Textarea: album description ---
    val = _get_textarea_value(soup, "album_desc")
    if val is not None:
        data["album_desc"] = val


class RedApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = "RED"
        self.base_url = "https://redacted.sh"
        self.tracker_url = "https://flacsfor.me"
        self.site_string = "RED"
        if cfg.tracker.red:
            red_cfg = cfg.tracker.red

            self.cookie = red_cfg.session
            self.keeplogged = red_cfg.keeplogged
            if red_cfg.api_key:
                self.api_key = red_cfg.api_key

            if red_cfg.dottorrents_dir:
                self.dot_torrents_dir = red_cfg.dottorrents_dir
            else:
                self.dot_torrents_dir = cfg.directory.dottorrents_dir

        super().__init__()

    async def upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent, using site page upload when log files are present.

        Temporary patch: API key upload has a bug where log scores are not
        announced in IRC. Prefer site page upload when cookie auth works, but
        fall back to API upload if the cookie-backed path is unavailable and an
        API key is configured.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        if files.log_files:
            if not self.api_key:
                return await self.site_page_upload(data, files)

            try:
                return await self.site_page_upload(data, files)
            except LoginError:
                click.secho(
                    "RED cookie auth failed for upload.php; falling back to API upload. "
                    "Log score IRC announcements may be incomplete on RED for this upload.",
                    fg="yellow",
                )
                return await self.api_key_upload(data, files)

        return await super().upload(data, files)

    async def site_page_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via upload.php with group data enrichment.

        When uploading to an existing group (groupid present), fetches the
        group info via API and populates fields that the site normally
        pre-fills on the upload form (artists, title, year, tags, etc.).
        This ensures the POST matches what the site expects.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        group_id = data.get("groupid")
        if group_id:
            await self._enrich_data_from_group(data, int(group_id))
        return await super().site_page_upload(data, files)

    async def _enrich_data_from_group(self, data: dict, group_id: int) -> None:
        """Fetch upload.php?groupid= page and scrape pre-filled fields.

        Instead of relying on the API, this directly GETs the upload page
        with the groupid parameter and parses the pre-filled form fields
        using BeautifulSoup — the same values the site would auto-populate.

        Args:
            data: Upload form data dict (mutated in place).
            group_id: The torrent group ID.
        """
        url = f"{self.base_url}/upload.php?groupid={group_id}"
        resp = await self._request("GET", url, timeout_secs=10)

        soup = BeautifulSoup(resp.text, "lxml")
        _parse_upload_form(data, soup)
