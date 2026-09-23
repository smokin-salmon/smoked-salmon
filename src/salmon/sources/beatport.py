import os.path
import re
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import asyncclick as click
import msgspec
from aiohttp import ClientSession
from bs4 import BeautifulSoup

from salmon import cfg
from salmon.common import handle_scrape_errors
from salmon.config import get_user_cfg_path
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper


class TokenStorage(msgspec.Struct):
    """
    Helper struct, holds auth tokens and takes care of persistence.
    """

    bearer: str
    expires: float
    refresh_token: str
    client_id: str

    @staticmethod
    def from_response(json: dict, client_id: str) -> Any:
        self = TokenStorage(
            bearer=json["access_token"],
            expires=time() + json["expires_in"],
            refresh_token=json["refresh_token"],
            client_id=client_id,
        )
        self._save()
        return self

    @staticmethod
    def _get_storage_file() -> Path:
        return get_user_cfg_path().with_name("beatport_auth.json")

    def _save(self):
        file = self._get_storage_file()
        if file.exists():
            file.touch()
        file.write_bytes(msgspec.json.encode(self))

    @classmethod
    def load(cls) -> Any | None:
        file = TokenStorage._get_storage_file()
        if os.path.exists(file):
            try:
                self = msgspec.json.decode(file.read_bytes(), type=TokenStorage)
                return self
            except msgspec.MsgspecError as e:
                click.secho(f"Error reading token storage: {e}", fg="red")
        return None

    def get_auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.bearer}"}


class BeatportBase(BaseScraper):
    api_domain = "https://api.beatport.com"
    url = api_domain + "/v4"
    oauth_redirect_uri = url + "/auth/o/post-message/"
    site_url = "https://beatport.com"
    search_url = "https://beatport.com/search/releases"
    release_format = "/release/{rls_name}/{rls_id}"
    regex = re.compile(r"^https?://(?:(?:www|classic)\.)?beatport\.com/release/.+?/(\d+)/?$")

    is_json_api = True

    async def load_token_storage(self) -> TokenStorage | None:
        """
        Loads the TokenStorage from the disk.
        Refreshes the token if expired.
        If it doesn't exist, runs the initial setup.
        :return: TokenStorage containing a valid bearer token, or None if no credentials configured
        """
        token_storage = TokenStorage.load() or await self.initial_setup()
        if token_storage and token_storage.expires < time() + 10:  # leave some wiggle room
            token_storage = await self.refresh_bearer_token(token_storage)
        return token_storage

    async def refresh_bearer_token(self, token_storage: TokenStorage) -> TokenStorage:
        """
        Refreshes a TokenStorage's bearer token. The new tokens are saved automatically.
        :param token_storage:
        :return: new TokenStorage with valid credentials.
        """
        click.secho("Refreshing Beatport token", fg="yellow")
        async with aiohttp.ClientSession() as session:
            headers = token_storage.get_auth_header()
            payload = {
                "client_id": token_storage.client_id,
                "refresh_token": token_storage.refresh_token,
                "grant_type": "refresh_token",
            }
            resp = await session.post(self.url + "/auth/o/token/", headers=headers, data=payload)
            json = await self.handle_json_response(resp)
            return TokenStorage.from_response(json, token_storage.client_id)

    async def _get_client_id(self, session: ClientSession) -> str:
        """
        Scrapes the OAuth client ID used by the API docs
        :param session:
        :return: The Client ID
        """
        soup = await self.fetch_page(self.url + "/docs/")
        client_id_pattern = re.compile(r"API_CLIENT_ID: \'(.*)\'")

        for script in soup.find_all("script", src=True):
            url = self.api_domain + str(script.get("src"))
            resp = await session.get(url)
            text = await resp.text("utf-8")
            client_id_matches = client_id_pattern.findall(text)
            if client_id_matches:
                return client_id_matches[0]
        raise Exception("Failed to find beatport client ID")

    async def initial_setup(self) -> TokenStorage | None:
        """
        Initial beatport setup. Uses user credentials to acquire a valid TokenStorage, which can be refreshed later.
        :return: TokenStorage containing valid auth tokens. It is persisted automatically.
        """
        username = cfg.metadata.beatport.username
        password = cfg.metadata.beatport.password
        if not username or not password:  # Not configured
            return None

        async with aiohttp.ClientSession() as session:
            click.secho("Logging into Beatport for the first time", fg="green")

            # Step 1: get client id from the docs. if this ever fails, we want it to fail first
            click.echo("Fetching Client ID")
            client_id = await self._get_client_id(session)

            # Step 2: login with username / password to set cookies
            click.secho("Logging in...")
            payload = {"username": username, "password": password}
            resp = await session.post(self.url + "/auth/login/", json=payload)
            await self.handle_json_response(resp)

            # Step 3: get authorization code from the docs' OAuth request
            params = {
                "client_id": client_id,
                "redirect_uri": self.oauth_redirect_uri,
                "response_type": "code",
            }
            resp = await session.get(self.url + "/auth/o/authorize/", params=params, allow_redirects=False)
            if resp.status < 300 or resp.status >= 400:
                data = await resp.read()
                soup = BeautifulSoup(data, "lxml")
                error = (soup.find("body") or soup).get_text("\n")
                raise Exception(f"Error during beatport OAuth flow:\n{error}")

            location = resp.headers.get("location")
            if not location:
                raise Exception(
                    f"Error during beatport OAuth flow: status is {resp.status}, but Location header is missing"
                )
            parsed = urlsplit(location)
            code = parse_qs(parsed.query).get("code")
            if not code:
                raise Exception(f"Error during beatport OAuth flow: no authorization code in redirect: {location}")

            # Step 4: finish code grant flow
            payload = {
                "client_id": client_id,
                "code": code,
                "redirect_uri": self.oauth_redirect_uri,
                "grant_type": "authorization_code",
            }
            resp = await session.post(self.url + "/auth/o/token/", data=payload)
            json = await self.handle_json_response(resp)
            token_storage = TokenStorage.from_response(json, client_id)

        click.secho("Done! Salmon will remember this login.", fg="green")
        return token_storage

    async def fetch_data_api(
        self, url, token_storage: TokenStorage, params: dict, rls_id: str | None = None
    ) -> dict[str, Any]:
        if not rls_id:
            match = self.regex.match(url)
            if not match:
                raise ScrapeError("Invalid Beatport URL format")
            rls_id = match.group(1)

        params = params or {}
        release_resp = await self.get_json(
            f"/catalog/releases/{rls_id}/", params=params, headers=token_storage.get_auth_header()
        )

        tracks = []
        params["release_id"] = rls_id
        params["per_page"] = 100

        for page in range(1, 9999):
            params["page"] = page
            resp = await self.get_json("/catalog/tracks/", params=params, headers=token_storage.get_auth_header())
            tracks.extend(resp["results"])
            if not resp["next"]:
                break

        return {"tracks": tracks, "release": release_resp}

    async def fetch_data_scraping(
        self,
        url: str,
        params: dict | None,
    ) -> dict[str, Any]:
        """Extract JSON track data from Beatport's HTML page.

        Args:
            url: The Beatport URL.
            params: Optional query parameters.

        Returns:
            Track query dict extracted from page data.

        Raises:
            ScrapeError: If extraction fails.
        """
        soup = await self.fetch_page(url, params)
        try:
            script_tag = soup.find("script", id="__NEXT_DATA__")
            if not script_tag or not script_tag.string:
                raise ScrapeError("Could not find Next.js data script tag")

            data = msgspec.json.decode(str(script_tag.string))
            queries = data["props"]["pageProps"]["dehydratedState"]["queries"]

            track_query = next((q for q in queries if q.get("queryKey") and q["queryKey"][0] == "tracks"), None)
            if not track_query:
                raise ScrapeError("Could not find track data in page")

            release_query_regex = re.compile(r"release-\d+")
            release_query = next(
                (q for q in queries if q.get("queryKey") and release_query_regex.match(q["queryKey"][0])), None
            )
            if not release_query:
                raise ScrapeError("Could not find release data in page")

            return {"tracks": track_query["state"]["data"]["results"], "release": release_query["state"]["data"]}

        except msgspec.DecodeError as e:
            raise ScrapeError("Failed to parse Beatport JSON data") from e
        except (KeyError, AttributeError) as e:
            raise ScrapeError(f"Failed to extract required data from Beatport page: {e}") from e

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict:
        """
        Fetches JSON track data:
        - from the official API, if configured
        - fallback to the internal API, by scraping the page

        :param url: The Beatport URL.
        :param params: Optional query parameters.
        :param headers: Inherited, ignored.
        :param follow_redirects: Inherited, ignored.
        :param rls_id: skips parsing ID from the URL; ignored when scraping.
        :return:
        """
        params_dict: dict = params or {}

        token_storage = await self.load_token_storage()
        if token_storage:
            resp = await handle_scrape_errors(self.fetch_data_api(url, token_storage, params_dict, rls_id=rls_id))
            if resp:
                return resp
            else:
                click.secho("Beatport API error, falling back to scraper", fg="yellow")

        return await self.fetch_data_scraping(url, params_dict)
