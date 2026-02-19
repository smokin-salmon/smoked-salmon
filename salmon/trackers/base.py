import asyncio
import html
import json
import re
from json.decoder import JSONDecodeError
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import aiohttp
import asyncclick as click
import msgspec
from aiohttp import FormData
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from salmon import cfg
from salmon.constants import RELEASE_TYPES
from salmon.errors import (
    LoginError,
    RequestError,
    RequestFailedError,
)

ARTIST_TYPES = [
    "main",
    "guest",
    "remixer",
    "composer",
    "conductor",
    "djcompiler",
    "producer",
]

INVERTED_RELEASE_TYPES = {
    **dict(zip(RELEASE_TYPES.values(), RELEASE_TYPES.keys(), strict=False)),
    1024: "Guest Appearance",
    1023: "Remixed By",
    1022: "Composition",
    1021: "Produced By",
}


def _compose_form_data(files: FormData, data: dict[str, Any]) -> FormData:
    """Compose FormData by adding data fields with proper value conversion.

    Args:
        files: The FormData object containing file uploads.
        data: Dictionary of field names and values to add.

    Returns:
        The FormData object with all fields added.
    """
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                # Convert bool to string for FormData compatibility
                if isinstance(item, bool):
                    item = str(item).lower()
                files.add_field(key, item)
        else:
            # Convert bool to string for FormData compatibility
            if isinstance(value, bool):
                value = str(value).lower()
            files.add_field(key, value)
    return files


class SearchReleaseData(msgspec.Struct, frozen=True):
    """Data structure for search release results."""

    lossless: bool
    lossless_web: bool
    year: int | None
    artist: str
    album: str
    release_type: str | int
    url: str


class RetryableError(Exception):
    """Exception for retryable network errors."""

    pass


class BaseGazelleApi:
    """Base API client for Gazelle-based trackers."""

    # Subclasses must set these attributes before calling __init__
    cookie: str
    base_url: str
    tracker_url: str
    site_code: str
    site_string: str
    api_key: str  # Optional, only for API key upload

    # Rate limiter: 10 requests per 10 seconds (shared across all instances)
    _rate_limiter = AsyncLimiter(10, 10)

    def __init__(self) -> None:
        """Initialize the API client. Subclasses should call this after setting cookie/base_url."""
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": cfg.upload.user_agent,
        }
        if not hasattr(self, "dot_torrents_dir"):
            self.dot_torrents_dir = cfg.directory.dottorrents_dir

        self.release_types = RELEASE_TYPES
        self.authkey: str | None = None
        self.passkey: str | None = None
        self._authenticated = False

    def _get_cookies(self) -> dict[str, str]:
        """Get cookies dict for requests."""
        return {"session": self.cookie}

    @property
    def announce(self) -> str:
        """Get the announce URL."""
        return f"{self.tracker_url}/{self.passkey}/announce"

    def request_url(self, id: int) -> str:
        """Get URL for a request page.

        Args:
            id: The request ID.

        Returns:
            The request URL.
        """
        return f"{self.base_url}/requests.php?action=view&id={id}"

    async def authenticate(self) -> None:
        """Authenticate with the tracker API and get authkey/passkey."""
        try:
            acctinfo = await self.request("index")
        except RequestError as err:
            raise LoginError from err
        self.authkey = acctinfo["authkey"]
        self.passkey = acctinfo["passkey"]
        self._authenticated = True

    async def ensure_authenticated(self) -> None:
        """Ensure we are authenticated before making requests."""
        if not self._authenticated:
            await self.authenticate()

    @retry(
        retry=retry_if_exception_type(RetryableError),
        stop=stop_after_attempt(5),
        wait=wait_fixed(1),
        reraise=True,
    )
    async def request(self, action: str, params: dict[str, Any] | None = None) -> dict:
        """Make a request to the site API with rate limiting.

        Args:
            action: The API action to perform.
            params: Additional parameters for the request.

        Returns:
            The API response data.

        Raises:
            LoginError: If authentication fails.
            RequestFailedError: If the request fails.
            RetryableError: If network error persists after retries.
        """
        url = self.base_url + "/ajax.php"
        params = {"action": action, **(params or {})}
        timeout = aiohttp.ClientTimeout(total=5)

        try:
            async with (
                self._rate_limiter,
                aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
                session.get(url, params=params, headers=self.headers, allow_redirects=False) as resp,
            ):
                if cfg.upload.debug_tracker_connection:
                    click.secho("URL: ", fg="cyan", nl=False)
                    click.secho(url, fg="yellow")
                    click.secho("Params: ", fg="cyan", nl=False)
                    click.secho(str(params), fg="yellow")
                    click.secho("Response: ", fg="cyan", nl=False)
                    click.secho(str(resp.status), fg="yellow")

                resp_text = await resp.text()
                if cfg.upload.debug_tracker_connection:
                    click.secho("Response Text: ", fg="cyan", nl=False)
                    click.secho(resp_text, fg="green")

                try:
                    resp_json = json.loads(resp_text)
                except (json.JSONDecodeError, ValueError):
                    resp_json = {"status": "error", "error": resp_text}
                retry_after_header = resp.headers.get("Retry-After", "20")
        except JSONDecodeError as err:
            raise LoginError from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise RetryableError(f"Network error: {err}") from err

        if resp_json.get("status") != "success":
            error_msg = resp_json.get("error", resp_text)
            if "rate limit" in str(error_msg).lower():
                retry_after = float(retry_after_header)
                click.secho(f"Rate limit exceeded, waiting {retry_after} seconds...", fg="yellow")
                await asyncio.sleep(retry_after)
                raise RetryableError("Rate limit exceeded")
            else:
                raise RequestFailedError(str(error_msg))
        return cast(dict, resp_json["response"])

    async def torrentgroup(self, group_id: int) -> dict:
        """Get information about a torrent group.

        Args:
            group_id: The torrent group ID.

        Returns:
            The torrent group data.
        """
        await self.ensure_authenticated()
        return await self.request("torrentgroup", params={"id": group_id})

    async def get_redirect_torrentgroupid(self, torrentid: int) -> int | None:
        """Get torrent group ID from torrent ID via redirect.

        Args:
            torrentid: The torrent ID.

        Returns:
            The torrent group ID as int, or None if not found.
        """
        await self.ensure_authenticated()
        url = self.base_url + "/torrents.php"
        params = {"torrentid": torrentid}
        timeout = aiohttp.ClientTimeout(total=5)

        try:
            async with (
                aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
                session.get(url, params=params, headers=self.headers, allow_redirects=False) as resp,
            ):
                location = resp.headers.get("Location")
                if location:
                    parsed = urlparse(location)
                    query = parse_qs(parsed.query)
                    group_id = query.get("id", [None])[0]
                    return int(group_id) if group_id else None
                else:
                    click.secho(
                        "Couldn't retrieve torrent_group_id from torrent_id, no Redirect found!",
                        fg="red",
                    )
                    raise click.Abort()
        except TimeoutError:
            click.secho(
                "Connection to API timed out, try script again later. Gomen!",
                fg="red",
            )
            raise click.Abort() from None

    async def get_request(self, id: int) -> dict:
        """Get information about a request.

        Args:
            id: The request ID.

        Returns:
            The request data.
        """
        await self.ensure_authenticated()
        return await self.request("request", params={"id": id})

    async def artist_rls(self, artist: str):
        """Get all torrent groups belonging to an artist.

        Args:
            artist: The artist name.

        Returns:
            Tuple of (artist_id, list of releases).
        """
        await self.ensure_authenticated()
        resp = await self.request("artist", params={"artistname": artist})
        releases = []
        for group in resp["torrentgroup"]:
            # We do not put compilations or guest appearances in this list.
            if not group["artists"]:
                continue
            if group["releaseType"] == 7 and (
                not group["extendedArtists"]["6"]
                or artist.lower() not in {a["name"].lower() for a in group["extendedArtists"]["6"]}
            ):
                continue
            if group["releaseType"] in {1023, 1021, 1022, 1024}:
                continue

            releases.append(
                SearchReleaseData(
                    lossless=any(t["format"] == "FLAC" for t in group["torrent"]),
                    lossless_web=any(t["format"] == "FLAC" and t["media"] == "WEB" for t in group["torrent"]),
                    year=group["groupYear"],
                    artist=html.unescape(compile_artists(group["artists"], group["releaseType"])),
                    album=html.unescape(group["groupName"]),
                    release_type=INVERTED_RELEASE_TYPES[group["releaseType"]],
                    url=f"{self.base_url}/torrents.php?id={group['groupId']}",
                )
            )

        releases = list({r.url: r for r in releases}.values())  # Dedupe

        return resp["id"], releases

    async def label_rls(self, label, year=None):
        """
        Get all the torrent groups from a label on site.
        All groups without a FLAC will be highlighted.
        """
        browse_params = {"remasterrecordlabel": label}
        if year:
            browse_params["year"] = year
        first_request = await self.request("browse", params=browse_params)
        if "pages" in first_request:
            pages = first_request["pages"]
        else:
            return []
        all_results = first_request["results"]
        # Three is an arbitrary (low) number.
        # Hits to the site are slow because of rate limiting.
        # Should probably be spun out into its own pagnation function at some point.
        for i in range(2, max(3, pages)):
            browse_params["page"] = str(i)
            new_results = await self.request("browse", params=browse_params)
            all_results += new_results["results"]
        browse_params["page"] = "1"
        resp2 = await self.request("browse", params=browse_params)
        all_results = all_results + resp2["results"]
        releases = []
        for group in all_results:
            if not group["artist"]:
                if "artists" in group:
                    artist = html.unescape(compile_artists(group["artists"], group["releaseType"]))
                else:
                    artist = ""
            else:
                artist = group["artist"]
            releases.append(
                SearchReleaseData(
                    lossless=any(t["format"] == "FLAC" for t in group["torrents"]),
                    lossless_web=any(t["format"] == "FLAC" and t["media"] == "WEB" for t in group["torrents"]),
                    year=group["groupYear"],
                    artist=artist,
                    album=html.unescape(group["groupName"]),
                    release_type=group["releaseType"],
                    url=f"{self.base_url}/torrents.php?id={group['groupId']}",
                )
            )

        releases = list({r.url: r for r in releases}.values())  # Dedupe

        return releases

    async def fetch_log(self, page: int) -> str:
        """Fetch a page of the site log.

        Args:
            page: The page number.

        Returns:
            The page HTML text.
        """
        await self.ensure_authenticated()
        url = f"{self.base_url}/log.php"
        timeout = aiohttp.ClientTimeout(total=10)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.get(url, params={"page": page}, headers=self.headers) as resp,
        ):
            return await resp.text()

    async def fetch_riplog(self, torrentid: int) -> str:
        """Fetch rip log for a torrent.

        Args:
            torrentid: The torrent ID.

        Returns:
            The log text with some content stripped.
        """
        await self.ensure_authenticated()
        url = f"{self.base_url}/torrents.php"
        timeout = aiohttp.ClientTimeout(total=10)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.get(url, headers=self.headers, params={"action": "loglist", "torrentid": torrentid}) as resp,
        ):
            text = await resp.text()
            return re.sub(r" ?\([^)]+\)", "", text)

    async def get_uploads_from_log(self, max_pages: int = 10) -> list:
        """Crawl log pages and return uploads.

        Args:
            max_pages: Maximum number of pages to crawl.

        Returns:
            List of (torrent_id, artist, title) tuples.
        """
        recent_uploads = []
        tasks = [self.fetch_log(i) for i in range(1, max_pages)]
        for page_text in await asyncio.gather(*tasks):
            recent_uploads += self.parse_uploads_from_log_html(page_text)
        return recent_uploads

    async def api_key_upload(self, data: dict, files: FormData) -> tuple[int, int]:
        """Upload torrent via API.

        Args:
            data: Upload form data.
            files: FormData containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        await self.ensure_authenticated()
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey
        api_key_headers = {**self.headers, "Authorization": self.api_key}

        _compose_form_data(files, data)

        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.post(url, data=files, headers=api_key_headers) as response,
        ):
            try:
                resp = await response.json()
            except (JSONDecodeError, ValueError) as e:
                text = await response.text()
                click.secho("❌ Failed to decode JSON response", fg="red", err=True)
                click.secho(f"Status code: {response.status}", fg="red", err=True)
                click.secho(f"Response text: {repr(text)}", fg="red", err=True)
                raise click.Abort from e

        try:
            if resp["status"] != "success":
                raise RequestError(f"API upload failed: {resp['error']}")
            elif resp["status"] == "success":
                if ("requestid" in resp["response"] and resp["response"]["requestid"]) or (
                    "fillRequest" in resp["response"]
                    and resp["response"]["fillRequest"]
                    and resp["response"]["fillRequest"]["requestId"]
                ):
                    requestId = (
                        resp["response"]["requestid"]
                        if "requestid" in resp["response"]
                        else resp["response"]["fillRequest"]["requestId"]
                    )
                    if requestId == -1:
                        click.secho("Request fill failed!", fg="red")
                    else:
                        click.secho("Filled request: " + self.request_url(requestId), fg="green")
                torrent_id = 0
                group_id = 0
                if "torrentid" in resp["response"]:
                    torrent_id = resp["response"]["torrentid"]
                    group_id = resp["response"]["groupid"]
                elif "torrentId" in resp["response"]:
                    torrent_id = resp["response"]["torrentId"]
                    group_id = resp["response"]["groupId"]
                return torrent_id, group_id
        except TypeError as err:
            raise RequestError(f"API upload failed, response: {resp}") from err
        raise RequestError("API upload failed: unexpected response format")

    async def site_page_upload(self, data: dict, files: FormData) -> tuple[int, int]:
        """Upload torrent via upload.php.

        Args:
            data: Upload form data.
            files: FormData containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        await self.ensure_authenticated()
        if "groupid" in data:
            url = self.base_url + f"/upload.php?groupid={data['groupid']}"
        else:
            url = self.base_url + "/upload.php"
        data["auth"] = self.authkey

        _compose_form_data(files, data)

        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.post(url, data=files, headers=self.headers) as response,
        ):
            resp_text = await response.text()
            resp_url = str(response.url)

        if self.announce in resp_text:
            match = re.search(r'<p style="color: red; text-align: center;">(.+)<\/p>', resp_text)
            if match:
                raise RequestError(f"Site upload failed: {match[1]} ({response.status})")
        if "requests.php" in resp_url:
            try:
                torrent_id = self.parse_torrent_id_from_filled_request_page(resp_text)
                group_id = await self.get_redirect_torrentgroupid(torrent_id) or 0
                click.secho(f"Filled request: {resp_url}", fg="green")
                return torrent_id, group_id
            except (TypeError, ValueError) as err:
                soup = BeautifulSoup(resp_text, "lxml")
                error = soup.find("h2", text="Error")
                error_message = resp_text
                if error and error.parent and error.parent.parent:
                    p_tag = error.parent.parent.find("p")
                    if p_tag:
                        error_message = p_tag.text
                raise RequestError(f"Request fill failed: {error_message}") from err
        try:
            return self.parse_most_recent_torrent_and_group_id_from_group_page(resp_text)
        except TypeError as err:
            raise RequestError(f"Site upload failed, response text: {resp_text}") from err

    async def upload(self, data: dict, files: FormData) -> tuple[int, int]:
        """Upload torrent via API or upload.php.

        Args:
            data: Upload form data.
            files: FormData containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        if getattr(self, "api_key", None):
            return await self.api_key_upload(data, files)
        else:
            return await self.site_page_upload(data, files)

    async def report_lossy_master(self, torrent_id: int, comment: str, source: str) -> bool:
        """Report torrent for lossy master/web approval.

        Args:
            torrent_id: The torrent ID.
            comment: Report comment.
            source: Media source (e.g., "WEB").

        Returns:
            True if successful.

        Raises:
            RequestError: If report fails.
        """
        await self.ensure_authenticated()
        url = self.base_url + "/reportsv2.php"
        params = {"action": "takereport"}
        type_ = "lossywebapproval" if source == "WEB" else "lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }

        timeout = aiohttp.ClientTimeout(total=10)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.post(url, params=params, data=data, headers=self.headers) as r,
        ):
            resp_url = str(r.url)
            if "torrents.php" in resp_url:
                return True
            raise RequestError(f"Failed to report the torrent for lossy master, code {r.status}.")

    async def append_to_torrent_description(self, torrent_id: int, description_addition: str) -> None:
        """Add text to start of torrent description.

        Args:
            torrent_id: The torrent ID.
            description_addition: Text to prepend to description.

        Raises:
            RequestError: If edit fails.
        """
        await self.ensure_authenticated()
        current_details = await self.request("torrent", params={"id": torrent_id})
        new_data = {
            "action": "takeedit",
            "torrentid": torrent_id,
            "type": 1,
            "groupremasters": 0,
            "remaster_year": current_details["torrent"]["remasterYear"],
            "remaster_title": current_details["torrent"]["remasterTitle"],
            "remaster_record_label": current_details["torrent"]["remasterRecordLabel"],
            "remaster_catalogue_number": current_details["torrent"]["remasterCatalogueNumber"],
            "format": current_details["torrent"]["format"],
            "bitrate": current_details["torrent"]["encoding"],
            "other_bitrate": "",
            "media": current_details["torrent"]["media"],
            "release_desc": description_addition + current_details["torrent"]["description"],
            "auth": self.authkey,
        }

        url = self.base_url + "/torrents.php"
        timeout = aiohttp.ClientTimeout(total=10)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.post(url, data=new_data, headers=self.headers) as resp,
        ):
            resp_text = await resp.text()

        soup = BeautifulSoup(resp_text, "lxml")
        edit_error = soup.find("h2", text="Error")
        if edit_error and edit_error.parent and edit_error.parent.parent:
            p_tag = edit_error.parent.parent.find("p")
            error_message = p_tag.text if p_tag else "Unknown error"
            raise RequestError(f"Failed to edit torrent: {error_message}")
        else:
            click.secho("Added spectrals to the torrent description.", fg="green")

    """The following three parsing functions are part of the gazelle class
    in order that they be easily overwritten in the derivative site classes.
    It is not because they depend on anything from the class"""

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text: str) -> tuple[int, int]:
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        torrent_ids: list[int] = []
        group_ids: list[int] = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a", class_="tooltip"):
            href = pl.get("href", "")
            torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", str(href))
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        for pl in soup.find_all("a", class_="brackets"):
            href = pl.get("href", "")
            group_url = re.search(r"upload.php\?groupid=(\d+)", str(href))
            if group_url:
                group_ids.append(int(group_url[1]))

        if not torrent_ids or not group_ids:
            raise TypeError("Could not parse torrent/group id from group page")

        return max(torrent_ids), max(group_ids)

    def parse_torrent_id_from_filled_request_page(self, text: str) -> int:
        """
        Given the HTML (ew) response from filling a request,
        find the filling torrent (hopefully our upload)
        """
        torrent_ids: list[int] = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a"):
            if pl.string == "Yes":
                href = pl.get("href", "")
                torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", str(href))
                if torrent_url:
                    torrent_ids.append(int(torrent_url[1]))
        return max(torrent_ids)

    def parse_uploads_from_log_html(self, text: str) -> list[tuple[str, str, str]]:
        """Parses a log page and returns best guess at
        (torrent id, 'Artist', 'title') tuples for uploads"""
        log_uploads: list[tuple[str, str, str]] = []
        soup = BeautifulSoup(text, "lxml")
        for entry in soup.find_all("span", class_="log_upload"):
            anchor = entry.find("a")
            if not anchor:
                continue
            href = anchor.get("href", "")
            torrent_id = str(href)[23:]
            try:
                # it having class log_upload is no guarantee that is what it is. Nice one log.
                next_sib = anchor.next_sibling
                if not next_sib:
                    continue
                torrent_string = re.findall(r"\((.*?)\) \(", str(next_sib))[0].split(" - ")
            except (IndexError, TypeError):
                continue
            artist = torrent_string[0]
            if len(torrent_string) > 1:
                title = torrent_string[1]
            else:
                artist = ""
                title = torrent_string[0]
            log_uploads.append((torrent_id, artist, title))
        return log_uploads


def compile_artists(artists, release_type):
    """Generate a string to represent the artists."""
    if release_type == 7 or len(artists) > 3:
        return cfg.upload.formatting.various_artist_word
    return " & ".join([a["name"] for a in artists])
