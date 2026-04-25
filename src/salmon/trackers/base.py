import asyncio
import html
import re
from contextlib import suppress
from http import HTTPStatus
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
from salmon.common import UploadFiles
from salmon.constants import RELEASE_TYPES
from salmon.errors import (
    LoginError,
    RequestError,
    RequestFailedError,
)
from salmon.proxy import session_kwargs

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

_SENSITIVE_KEYS = re.compile(
    r'"(authkey|passkey|auth|api_key|Authorization)"\s*:\s*"[^"]*"',
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Redact sensitive keys from debug output strings.

    Replaces values of known sensitive fields (authkey, passkey, auth, etc.)
    with [REDACTED] to prevent accidental exposure in logs.

    Args:
        text: The string to redact.

    Returns:
        The string with sensitive values replaced.
    """
    return _SENSITIVE_KEYS.sub(lambda m: f'"{m.group(1)}": "[REDACTED]"', text)


def _add_form_field(form: FormData, key: str, value: Any) -> None:
    """Add a single value to FormData, coercing types as needed.

    aiohttp FormData only accepts str/bytes/IO types, so bool and int
    values are converted accordingly. False and None are skipped.

    Args:
        form: The FormData instance to add the field to.
        key: The field name.
        value: The field value.
    """
    if value is True:
        form.add_field(key, "on")
    elif value is False or value is None:
        return
    elif isinstance(value, int):
        form.add_field(key, str(value))
    else:
        form.add_field(key, value)


def _compose_form_data(files: UploadFiles, data: dict[str, Any]) -> FormData:
    """Compose FormData by converting UploadFiles and adding data fields.

    Args:
        files: The UploadFiles object containing file uploads.
        data: Dictionary of field names and values to add.

    Returns:
        A new FormData object with all files and fields added.
    """
    form = FormData()
    form.add_field(
        "file_input",
        files.torrent_data,
        filename="meowmeow.torrent",
        content_type="application/octet-stream",
    )
    for log_name, log_data in files.log_files:
        form.add_field(
            "logfiles[]",
            log_data,
            filename=log_name,
            content_type="application/octet-stream",
        )
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                _add_form_field(form, key, item)
        else:
            _add_form_field(form, key, value)
    return form


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


class HttpResponse(msgspec.Struct, frozen=True):
    """HTTP response data extracted from aiohttp.ClientResponse."""

    text: str
    url: str
    status: int


class BaseGazelleApi:
    """Base API client for Gazelle-based trackers."""

    # Subclasses must set these attributes before calling __init__
    cookie: str
    base_url: str
    tracker_url: str
    site_code: str
    site_string: str
    api_key: str = ""  # Optional, only for API key upload
    proxy_service: str = ""  # Set in subclasses to enable proxy routing

    # Rate limiter: 5 requests per 10 seconds (shared across all instances)
    _rate_limiter = AsyncLimiter(5, 10)

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
        acctinfo = await self.api_call("index")
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
    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
        timeout_secs: int = 10,
        prefer_api_key: bool = False,
    ) -> HttpResponse:
        """Authenticated HTTP request, returns response data.

        Args:
            method: HTTP method (e.g. "GET", "POST").
            url: The URL to request.
            params: Query parameters.
            data: POST body data.
            timeout_secs: Request timeout in seconds.
            prefer_api_key: If True and api_key is set, use Authorization header
                only (no cookie). If False or api_key is empty, use cookie only
                (no Authorization header).

        Returns:
            HttpResponse with text, url, status, and headers.
        """
        if not (params and params.get("action") == "index"):
            await self.ensure_authenticated()

        use_api_key = prefer_api_key and bool(self.api_key)
        headers = {
            **self.headers,
            **({"Authorization": self.api_key} if use_api_key else {}),
        }
        cookies = {} if use_api_key else self._get_cookies()

        if cfg.upload.debug_tracker_connection:
            click.secho(f"[DEBUG] {method} {url}", fg="cyan")
            click.secho(f"[DEBUG] params: {_redact(msgspec.json.encode(params).decode())}", fg="cyan")
            click.secho(f"[DEBUG] use_api_key: {use_api_key}", fg="cyan")

        try:
            timeout = aiohttp.ClientTimeout(total=timeout_secs)
            async with (
                self._rate_limiter,
                aiohttp.ClientSession(
                    timeout=timeout,
                    cookies=cookies,
                    headers=headers,
                    **session_kwargs(self.proxy_service),
                ) as session,
                session.request(method, url, params=params, data=data) as resp,
            ):
                text = await resp.text()

                if cfg.upload.debug_tracker_connection:
                    click.secho(f"[DEBUG] status: {resp.status}", fg="cyan")
                    click.secho(
                        f"[DEBUG] response headers: {_redact(msgspec.json.encode(dict(resp.headers)).decode())}",
                        fg="cyan",
                    )
                    click.secho(f"[DEBUG] response body: {_redact(text)}", fg="green")

                if not resp.ok:
                    error_msg = text
                    with suppress(msgspec.DecodeError, ValueError):
                        error_msg = msgspec.json.encode(msgspec.json.decode(text)["error"]).decode()

                    if resp.status == HTTPStatus.TOO_MANY_REQUESTS or "rate limit" in error_msg.lower():
                        retry_after = float(resp.headers.get("Retry-After", "20"))
                        click.secho(f"Rate limit exceeded, waiting {retry_after} seconds...", fg="yellow")
                        await asyncio.sleep(retry_after)
                        raise RetryableError("Rate limit exceeded")

                    if resp.status == HTTPStatus.UNAUTHORIZED:
                        click.secho(
                            f"Authentication to {self.site_string} failed: {error_msg}.\nYour API key may be invalid.",
                            fg="red",
                        )
                        raise LoginError(error_msg)

                    if resp.status in (
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        HTTPStatus.BAD_GATEWAY,
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        HTTPStatus.GATEWAY_TIMEOUT,
                    ):
                        raise RetryableError(f"Server error {resp.status}, retrying...")

                    click.secho(
                        f"Request to {self.site_string} failed ({resp.status}): {error_msg}",
                        fg="red",
                    )
                    raise RequestFailedError(error_msg)

                return HttpResponse(
                    text=text,
                    url=str(resp.url),
                    status=resp.status,
                )
        except aiohttp.TooManyRedirects as err:
            click.secho(
                "Too many redirects. Your cookies may be invalid or expired.",
                fg="red",
                bold=True,
            )
            raise LoginError from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise RetryableError(f"Network error: {err}") from err

    async def api_call(self, action: str, params: dict[str, Any] | None = None) -> dict:
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

        resp = await self._request("GET", url, params=params, timeout_secs=5, prefer_api_key=True)

        try:
            resp_json = msgspec.json.decode(resp.text)
        except (msgspec.DecodeError, ValueError):
            resp_json = {"status": "error", "error": resp.text}

        if resp_json.get("status") != "success":
            raise RequestFailedError(str(resp_json.get("error", resp.text)))
        return cast("dict", resp_json["response"])

    async def torrentgroup(self, group_id: int) -> dict:
        """Get information about a torrent group.

        Args:
            group_id: The torrent group ID.

        Returns:
            The torrent group data.
        """
        return await self.api_call("torrentgroup", params={"id": group_id})

    async def get_redirect_torrentgroupid(self, torrentid: int) -> int | None:
        """Get torrent group ID from torrent ID via redirect.

        Args:
            torrentid: The torrent ID.

        Returns:
            The torrent group ID as int, or None if not found.
        """
        url = self.base_url + "/torrents.php"
        try:
            resp = await self._request("GET", url, params={"torrentid": torrentid}, timeout_secs=5)
        except TimeoutError:
            click.secho("Connection to API timed out, try script again later. Gomen!", fg="red")
            raise click.Abort() from None
        parsed = urlparse(resp.url)
        query = parse_qs(parsed.query)
        group_id = query.get("id", [None])[0]
        if group_id:
            return int(group_id)
        click.secho("Couldn't retrieve torrent_group_id from torrent_id, no Redirect found!", fg="red")
        raise click.Abort()

    async def get_request(self, id: int) -> dict:
        """Get information about a request.

        Args:
            id: The request ID.

        Returns:
            The request data.
        """
        return await self.api_call("request", params={"id": id})

    async def artist_rls(self, artist: str):
        """Get all torrent groups belonging to an artist.

        Args:
            artist: The artist name.

        Returns:
            Tuple of (artist_id, list of releases).
        """
        resp = await self.api_call("artist", params={"artistname": artist})
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
        first_request = await self.api_call("browse", params=browse_params)
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
            new_results = await self.api_call("browse", params=browse_params)
            all_results += new_results["results"]
        browse_params["page"] = "1"
        resp2 = await self.api_call("browse", params=browse_params)
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
        url = f"{self.base_url}/log.php"
        resp = await self._request("GET", url, params={"page": page})
        return resp.text

    async def fetch_riplog(self, torrentid: int) -> str:
        """Fetch rip log for a torrent.

        Args:
            torrentid: The torrent ID.

        Returns:
            The log text with some content stripped.
        """
        url = f"{self.base_url}/torrents.php"
        resp = await self._request("GET", url, params={"action": "loglist", "torrentid": torrentid})
        return re.sub(r" ?\([^)]+\)", "", resp.text)

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

    async def api_key_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via API.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey

        response = await self._request(
            "POST", url, data=_compose_form_data(files, data), timeout_secs=30, prefer_api_key=True
        )
        try:
            resp = msgspec.json.decode(response.text)
        except (msgspec.DecodeError, ValueError) as e:
            click.secho("❌ Failed to decode JSON response", fg="red", err=True)
            click.secho(f"Status code: {response.status}", fg="red", err=True)
            click.secho(f"Response text: {repr(response.text)}", fg="red", err=True)
            raise click.Abort from e

        try:
            if resp["status"] != "success":
                raise RequestError(f"API upload failed: {resp['error']}")
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

    async def site_page_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via upload.php.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        if "groupid" in data:
            url = self.base_url + f"/upload.php?groupid={data['groupid']}"
        else:
            url = self.base_url + "/upload.php"
        data["auth"] = self.authkey

        response = await self._request("POST", url, data=_compose_form_data(files, data), timeout_secs=30)
        resp_text = response.text
        resp_url = response.url

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

    async def upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via API or upload.php.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        if self.api_key:
            return await self.api_key_upload(data, files)

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
        url = self.base_url + "/reportsv2.php"
        type_ = "lossywebapproval" if source == "WEB" else "lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }
        resp = await self._request("POST", url, params={"action": "takereport"}, data=data)
        if "torrents.php" in resp.url:
            return True
        raise RequestError(f"Failed to report the torrent for lossy master, code {resp.status}.")

    async def append_to_torrent_description(self, torrent_id: int, description_addition: str) -> None:
        """Add text to start of torrent description.

        Args:
            torrent_id: The torrent ID.
            description_addition: Text to prepend to description.

        Raises:
            RequestError: If edit fails.
        """
        current_details = await self.api_call("torrent", params={"id": torrent_id})
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
        resp = await self._request("POST", url, data=new_data)
        resp_text = resp.text

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
