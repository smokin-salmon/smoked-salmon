import asyncio
import html
import re
from collections import namedtuple
from json.decoder import JSONDecodeError
from urllib.parse import parse_qs, urlparse

import aiohttp
import asyncclick as click
from bs4 import BeautifulSoup
from ratelimit import RateLimitException, limits, sleep_and_retry

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


SearchReleaseData = namedtuple(
    "SearchReleaseData",
    ["lossless", "lossless_web", "year", "artist", "album", "release_type", "url"],
)


class BaseGazelleApi:
    """Base API client for Gazelle-based trackers."""

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

    @limits(10, 10)
    async def request(self, action: str, **kwargs) -> dict:
        """Make a request to the site API with rate limiting.

        Args:
            action: The API action to perform.
            **kwargs: Additional parameters for the request.

        Returns:
            The API response data.

        Raises:
            LoginError: If authentication fails.
            RequestFailedError: If the request fails.
        """
        url = self.base_url + "/ajax.php"
        params = {"action": action, **kwargs}
        timeout = aiohttp.ClientTimeout(total=5)

        try:
            async with (
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

                resp_json = await resp.json()
        except JSONDecodeError as err:
            raise LoginError from err
        except TimeoutError:
            click.secho(
                "Connection to API timed out, try script again later. Gomen!",
                fg="red",
            )
            raise click.Abort() from None

        if resp_json["status"] != "success":
            if "rate limit" in resp_json["error"].lower():
                retry_after = float(resp.headers.get("Retry-After", "20"))
                click.secho(f"Rate limit exceeded, waiting {retry_after} seconds before retry...", fg="yellow")
                raise RateLimitException("Rate limit exceeded", period_remaining=retry_after)
            else:
                raise RequestFailedError(resp_json["error"])
        return resp_json["response"]

    async def torrentgroup(self, group_id: int) -> dict:
        """Get information about a torrent group.

        Args:
            group_id: The torrent group ID.

        Returns:
            The torrent group data.
        """
        await self.ensure_authenticated()
        return await self.request("torrentgroup", id=group_id)

    async def get_redirect_torrentgroupid(self, torrentid: int) -> str | None:
        """Get torrent group ID from torrent ID via redirect.

        Args:
            torrentid: The torrent ID.

        Returns:
            The torrent group ID.
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
                    return query.get("id", [None])[0]
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
        return await self.request("request", id=id)

    async def artist_rls(self, artist: str):
        """Get all torrent groups belonging to an artist.

        Args:
            artist: The artist name.

        Returns:
            Tuple of (artist_id, list of releases).
        """
        await self.ensure_authenticated()
        resp = await self.request("artist", artistname=artist)
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
        params = {"remasterrecordlabel": label}
        if year:
            params["year"] = year
        first_request = await self.request("browse", **params)
        if "pages" in first_request:
            pages = first_request["pages"]
        else:
            return []
        all_results = first_request["results"]
        # Three is an arbitrary (low) number.
        # Hits to the site are slow because of rate limiting.
        # Should probably be spun out into its own pagnation function at some point.
        for i in range(2, max(3, pages)):
            params["page"] = str(i)
            new_results = await self.request("browse", **params)
            all_results += new_results["results"]
        params["page"] = "1"
        resp2 = await self.request("browse", **params)
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

    async def api_key_upload(self, data: dict, files: dict) -> tuple[int, int]:
        """Upload torrent via API.

        Args:
            data: Upload form data.
            files: Files to upload (torrent file).

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        await self.ensure_authenticated()
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey
        api_key_headers = {**self.headers, "Authorization": self.api_key}

        form_data = aiohttp.FormData()
        for key, value in data.items():
            form_data.add_field(key, str(value))
        for key, (filename, file_obj, content_type) in files.items():
            form_data.add_field(key, file_obj, filename=filename, content_type=content_type)

        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.post(url, data=form_data, headers=api_key_headers) as response,
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
                if "torrentid" in resp["response"]:
                    torrent_id = resp["response"]["torrentid"]
                    group_id = resp["response"]["groupid"]
                elif "torrentId" in resp["response"]:
                    torrent_id = resp["response"]["torrentId"]
                    group_id = resp["response"]["groupId"]
                return torrent_id, group_id
        except TypeError as err:
            raise RequestError(f"API upload failed, response: {resp}") from err

    async def site_page_upload(self, data: dict, files: dict) -> tuple[int, int]:
        """Upload torrent via upload.php.

        Args:
            data: Upload form data.
            files: Files to upload (torrent file).

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

        form_data = aiohttp.FormData()
        for key, value in data.items():
            form_data.add_field(key, str(value))
        for key, (filename, file_obj, content_type) in files.items():
            form_data.add_field(key, file_obj, filename=filename, content_type=content_type)

        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.post(url, data=form_data, headers=self.headers) as response,
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
                group_id = await self.get_redirect_torrentgroupid(torrent_id)
                click.secho(f"Filled request: {resp_url}", fg="green")
                return torrent_id, group_id
            except (TypeError, ValueError) as err:
                soup = BeautifulSoup(resp_text, "lxml")
                error = soup.find("h2", text="Error")
                p_tag = error.parent.parent.find("p") if error else None
                error_message = p_tag.text if p_tag else resp_text
                raise RequestError(f"Request fill failed: {error_message}") from err
        try:
            return self.parse_most_recent_torrent_and_group_id_from_group_page(resp_text)
        except TypeError as err:
            raise RequestError(f"Site upload failed, response text: {resp_text}") from err

    async def upload(self, data: dict, files: dict) -> tuple[int, int]:
        """Upload torrent via API or upload.php.

        Args:
            data: Upload form data.
            files: Files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        if hasattr(self, "api_key"):
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
        current_details = await self.request("torrent", id=torrent_id)
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
        if edit_error:
            error_message = edit_error.parent.parent.find("p").text
            raise RequestError(f"Failed to edit torrent: {error_message}")
        else:
            click.secho("Added spectrals to the torrent description.", fg="green")

    """The following three parsing functions are part of the gazelle class
    in order that they be easily overwritten in the derivative site classes.
    It is not because they depend on anything from the class"""

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text):
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        torrent_ids = []
        group_ids = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a", class_="tooltip"):
            torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", pl["href"])
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        for pl in soup.find_all("a", class_="brackets"):
            group_url = re.search(r"upload.php\?groupid=(\d+)", pl["href"])
            if group_url:
                group_ids.append(int(group_url[1]))

        return max(torrent_ids), max(group_ids)

    def parse_torrent_id_from_filled_request_page(self, text):
        """
        Given the HTML (ew) response from filling a request,
        find the filling torrent (hopefully our upload)
        """
        torrent_ids = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a", string="Yes"):
            torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", pl["href"])
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        return max(torrent_ids)

    def parse_uploads_from_log_html(self, text):
        """Parses a log page and returns best guess at
        (torrent id, 'Artist', 'title') tuples for uploads"""
        log_uploads = []
        soup = BeautifulSoup(text, "lxml")
        for entry in soup.find_all("span", class_="log_upload"):
            torrent_id = entry.find("a")["href"][23:]
            try:
                # it having class log_upload is no guarantee that is what it is. Nice one log.
                torrent_string = re.findall(r"\((.*?)\) \(", entry.find("a").next_sibling)[0].split(" - ")
            except BaseException:
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
