import time
from typing import Any

import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common import handle_scrape_errors
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import BeatportBase
from salmon.sources.beatport import TokenStorage


class Searcher(BeatportBase, SearchMixin):
    async def search_releases_api(self, token_storage: TokenStorage, searchstr: str, limit: int) -> tuple[str, dict]:
        params = {"q": searchstr, "type": "releases"}
        resp = await self.get_json("/catalog/search/", params=params, headers=token_storage.get_auth_header())

        releases: dict[Any, Any] = {}
        for release in resp["releases"]:
            try:
                rls_id = release["id"]

                main_artists = [a["name"] for a in release["artists"]]
                artists = (
                    ", ".join(main_artists) if len(main_artists) < 4 else cfg.upload.formatting.various_artist_word
                )

                title = release["name"]
                year = time.strptime(release["publish_date"], "%Y-%m-%d").tm_year
                track_count = release["track_count"]
                explicit = release["is_explicit"]

                label = edition = release["label"]["name"]
                catno = release["catalog_number"]
                if catno != release["upc"]:
                    edition += " " + catno

                if label.lower() not in cfg.upload.search.excluded_labels:
                    releases[rls_id] = (
                        IdentData(artists, title, year, track_count, "WEB"),
                        self.format_result(artists, title, edition, track_count, explicit=explicit),
                    )
            except (KeyError, IndexError) as e:
                raise ScrapeError("Failed to parse search result item") from e

            if len(releases) == limit:
                break

        return "Beatport", releases

    async def search_releases_scraping(self, searchstr: str, limit: int) -> tuple[str, dict]:
        releases: dict[Any, Any] = {}
        soup = await self.fetch_page(self.search_url, params={"q": searchstr})
        try:
            script_tag = soup.find("script", id="__NEXT_DATA__")
            if not script_tag or not script_tag.text:
                raise ScrapeError("Could not find Next.js data script tag")

            try:
                data = msgspec.json.decode(script_tag.text)
            except msgspec.DecodeError as e:
                raise ScrapeError("Failed to parse Beatport JSON data") from e
            search_results = data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["data"]
            for result in search_results:
                try:
                    rls_id = result["release_id"]

                    # Filter artists to get only main artists (not remixers)
                    main_artists = [a["artist_name"] for a in result["artists"] if a["artist_type_name"] == "Artist"]

                    title = result["release_name"]
                    artists = (
                        ", ".join(main_artists) if len(main_artists) < 4 else cfg.upload.formatting.various_artist_word
                    )
                    label = result["label"]["label_name"]

                    if label.lower() not in cfg.upload.search.excluded_labels:
                        releases[rls_id] = (
                            IdentData(artists, title, None, None, "WEB"),
                            self.format_result(artists, title, label),
                        )
                except (KeyError, IndexError) as e:
                    raise ScrapeError("Failed to parse search result item") from e

                if len(releases) == limit:
                    break

        except (KeyError, IndexError) as e:
            raise ScrapeError("Failed to parse scraped search results") from e

        return "Beatport", releases

    async def search_releases(self, searchstr: str, limit: int) -> tuple[str, dict]:
        token_storage = await self.load_token_storage()
        if token_storage:
            resp: tuple[str, dict] | None = await handle_scrape_errors(
                self.search_releases_api(token_storage, searchstr, limit)
            )
            if resp:
                return resp
            else:
                click.secho("Beatport API error, falling back to scraper", fg="yellow")

        return await self.search_releases_scraping(searchstr, limit)
