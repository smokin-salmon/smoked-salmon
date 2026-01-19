import asyncio
import html
import re
from collections import namedtuple
from json.decoder import JSONDecodeError
from urllib.parse import parse_qs, urlparse

import click
import requests
from bs4 import BeautifulSoup
from ratelimit import RateLimitException, limits, sleep_and_retry
from requests.exceptions import ConnectTimeout, ReadTimeout

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
    def __init__(self):
        "Base init class. Will generally be overridden by the specific site class."
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": cfg.upload.user_agent,
        }
        if not hasattr(self, "dot_torrents_dir"):
            self.dot_torrents_dir = cfg.directory.dottorrents_dir

        self.release_types = RELEASE_TYPES

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.authkey = None
        self.passkey = None
        self.authenticate()

    @property
    def announce(self):
        return f"{self.tracker_url}/{self.passkey}/announce"

    def request_url(self, id):
        "Given a request ID return a request URL"
        return f"{self.base_url}/requests.php?action=view&id={id}"

    def authenticate(self):
        """Make a request to the site API with the saved cookie and get our authkey."""
        self.session.cookies.clear()
        self.session.cookies["session"] = self.cookie
        try:
            acctinfo = asyncio.run(self.request("index"))
        except RequestError as err:
            raise LoginError from err
        self.authkey = acctinfo["authkey"]
        self.passkey = acctinfo["passkey"]

    @sleep_and_retry
    @limits(10, 10)
    async def request(self, action, **kwargs):
        """
        Make a request to the site API, accomodating the rate limit.
        This uses the ratelimit library to ensure that
        the 10 requests / 10 seconds rate limit isn't violated, while allowing
        short bursts of requests without a 2 second wait after each one
        (at the expense of a potentially longer wait later).
        """

        url = self.base_url + "/ajax.php"
        params = {"action": action, **kwargs}
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.session.get(url, params=params, timeout=5, allow_redirects=False),
            )

            if cfg.upload.debug_tracker_connection:
                click.secho("URL: ", fg="cyan", nl=False)
                click.secho(url, fg="yellow")

                click.secho("Params: ", fg="cyan", nl=False)
                click.secho(str(params), fg="yellow")

                click.secho("Response: ", fg="cyan", nl=False)
                click.secho(str(resp), fg="yellow")

                click.secho("Response Text: ", fg="cyan", nl=False)
                click.secho(resp.text, fg="green")

            resp_json = resp.json()
        except JSONDecodeError as err:
            raise LoginError from err
        except (ConnectTimeout, ReadTimeout):
            click.secho(
                "Connection to API timed out, try script again later. Gomen!",
                fg="red",
            )
            raise click.Abort() from None

        if resp_json["status"] != "success":
            if "rate limit" in resp_json["error"].lower():
                retry_after = float(resp.headers.get("Retry-After", "20"))
                click.secho(f"Rate limit exceeded, waiting {retry_after} seconds before retry...", fg="yellow")
                # Raise RateLimitException to trigger the @sleep_and_retry decorator
                raise RateLimitException("Rate limit exceeded", period_remaining=retry_after)
            else:
                raise RequestFailedError(resp_json["error"])
        return resp_json["response"]

    async def torrentgroup(self, group_id):
        """Get information about a torrent group."""
        return await self.request("torrentgroup", id=group_id)

    async def get_redirect_torrentgroupid(self, torrentid):
        url = self.base_url + "/torrents.php"
        params = {"torrentid": torrentid}

        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.session.get(url, params=params, timeout=5, allow_redirects=False),
            )
            location = resp.headers.get("Location")
            if location:
                parsed = urlparse(location)
                query = parse_qs(parsed.query)
                torrent_group_id = query.get("id", [None])[0]
                return torrent_group_id
            else:
                click.secho(
                    "Couldn't retrieve torrent_group_id from torrent_id, no Redirect found!",
                    fg="red",
                )
                raise click.Abort()
        except (ConnectTimeout, ReadTimeout):
            click.secho(
                "Connection to API timed out, try script again later. Gomen!",
                fg="red",
            )
            raise click.Abort() from None

    async def get_request(self, id):
        """Get information about a request."""
        data = {"id": id}
        return await self.request("request", **data)

    async def artist_rls(self, artist):
        """
        Get all the torrent groups belonging to an artist on site.
        All groups without a FLAC will be highlighted.
        """
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

    async def fetch_log(self, page):
        """Fetch a page of the log. No search. Search envokes the sphynx
        Doesn't use the API as there is no API endpoint."""
        url = f"{self.base_url}/log.php"
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.session.get(url, params={"page": page}, headers=self.headers),
        )
        return resp

    async def fetch_riplog(self, torrentid):
        """Fetch a page of the log. No search. Search envokes the sphynx
        Doesn't use the API as there is no API endpoint."""
        url = f"{self.base_url}/torrents.php"
        resp = await self.aiosession.get(
            url, headers=self.headers, params={"action": "loglist", "torrentid": torrentid}
        )
        return re.sub(r" ?\([^)]+\)", "", resp.text)

    def get_uploads_from_log(self, max_pages=10):
        "Crawls some pages of the log and returns uploads"
        recent_uploads = []
        tasks = [self.fetch_log(i) for i in range(1, max_pages)]
        for page in asyncio.run(asyncio.gather(*tasks)):
            recent_uploads += self.parse_uploads_from_log_html(page.text)
        return recent_uploads

    async def api_key_upload(self, data, files):
        """Attempt to upload a torrent to the site.
        using the API"""
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey
        # Shallow copy. We don't want the future requests to send the api key.
        api_key_headers = {**self.headers, "Authorization": self.api_key}
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.session.post(url, data=data, files=files, headers=api_key_headers),
        )
        try:
            resp = resp.json()
        except (requests.exceptions.JSONDecodeError, ValueError) as e:
            click.echo("❌ Failed to decode JSON response", fg="red", err=True)
            click.echo(f"Status code: {resp.status_code}", fg="red", err=True)
            click.echo(f"Response text: {repr(resp.text)}", fg="red", err=True)
            raise click.Abort from e
        # print(resp) debug

        try:
            if resp["status"] != "success":
                raise RequestError(f"API upload failed: {resp['error']}")
            elif resp["status"] == "success":
                if (
                    "requestid" in resp["response"]  # RED
                    and resp["response"]["requestid"]
                ) or (
                    "fillRequest" in resp["response"]  # OPS
                    and resp["response"]["fillRequest"]
                    and resp["response"]["fillRequest"]["requestId"]
                ):
                    requestId = (
                        resp["response"]["requestid"]
                        if "requestid" in resp["response"]
                        else resp["response"]["fillRequest"]["requestId"]
                    )
                    if requestId == -1:
                        click.secho(
                            "Request fill failed!",
                            fg="red",
                        )
                    else:
                        click.secho(
                            "Filled request: " + self.request_url(requestId),
                            fg="green",
                        )
                torrent_id = 0
                if "torrentid" in resp["response"]:
                    torrent_id = resp["response"]["torrentid"]
                    group_id = resp["response"]["groupid"]
                elif "torrentId" in resp["response"]:
                    torrent_id = resp["response"]["torrentId"]
                    group_id = resp["response"]["groupId"]
                return torrent_id, group_id
        except TypeError as err:
            raise RequestError(f"API upload failed, response text: {resp.text}") from err

    async def site_page_upload(self, data, files):
        """Attempt to upload a torrent to the site.
        using the upload.php"""
        if "groupid" in data:
            url = self.base_url + f"/upload.php?groupid={data['groupid']}"
        else:
            url = self.base_url + "/upload.php"
        data["auth"] = self.authkey
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.session.post(url, data=data, files=files, headers=self.headers),
        )

        if self.announce in resp.text:
            match = re.search(
                r'<p style="color: red; text-align: center;">(.+)<\/p>',
                resp.text,
            )
            if match:
                raise RequestError(f"Site upload failed: {match[1]} ({resp.status_code})")
        if "requests.php" in resp.url:
            try:
                torrent_id = self.parse_torrent_id_from_filled_request_page(resp.text)
                group_id = await self.get_redirect_torrentgroupid(torrent_id)
                click.secho(f"Filled request: {resp.url}", fg="green")
                return torrent_id, group_id
            except (TypeError, ValueError) as err:
                soup = BeautifulSoup(resp.text, "html.parser")
                error = soup.find("h2", text="Error")
                p_tag = error.parent.parent.find("p") if error else None
                error_message = p_tag.text if p_tag else resp.text
                raise RequestError(f"Request fill failed: {error_message}") from err
        try:
            return self.parse_most_recent_torrent_and_group_id_from_group_page(resp.text)
        except TypeError as err:
            raise RequestError(f"Site upload failed, response text: {resp.text}") from err

    async def upload(self, data, files):
        """Upload a torrent using upload.php
        or the API depending on whether an API key is set."""
        if hasattr(self, "api_key"):
            return await self.api_key_upload(data, files)
        else:
            return await self.site_page_upload(data, files)

    async def report_lossy_master(self, torrent_id, comment, source):
        """Automagically report a torrent for lossy master/web approval.
        Use LWA if the torrent is web, otherwise LMA."""

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
        r = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.session.post(url, params=params, data=data, headers=self.headers),
        )
        if "torrents.php" in r.url:
            return True
        raise RequestError(f"Failed to report the torrent for lossy master, code {r.status_code}.")

    async def append_to_torrent_description(self, torrent_id, description_additon):
        """Adds to the start of an individual torrent description
        Currently not supported by the API"""
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
            "release_desc": description_additon + current_details["torrent"]["description"],
        }

        url = self.base_url + "/torrents.php"
        new_data["auth"] = self.authkey
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.session.post(url, data=new_data, headers=self.headers),
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        edit_error = soup.find("h2", text="Error")
        if edit_error:
            error_message = edit_error.parent.parent.find("p").text
            raise RequestError(f"Failed to edit torrent: {error_message}")
        else:
            click.secho(
                "Added spectrals to the torrent description.",
                fg="green",
            )

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
        soup = BeautifulSoup(text, "html.parser")
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
        soup = BeautifulSoup(text, "html.parser")
        for pl in soup.find_all("a", string="Yes"):
            torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", pl["href"])
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        return max(torrent_ids)

    def parse_uploads_from_log_html(self, text):
        """Parses a log page and returns best guess at
        (torrent id, 'Artist', 'title') tuples for uploads"""
        log_uploads = []
        soup = BeautifulSoup(text, "html.parser")
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
