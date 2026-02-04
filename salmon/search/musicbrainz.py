import asyncio

import musicbrainzngs

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import MusicBrainzBase


class Searcher(MusicBrainzBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        releases = {}
        loop = asyncio.get_running_loop()
        soup = await loop.run_in_executor(None, musicbrainzngs.search_releases, searchstr, 10)
        for rls in soup["release-list"]:
            try:
                artists = rls["artist-credit-phrase"]
                try:
                    track_count = rls["medium-track-count"]
                except KeyError:
                    track_count = None
                label = catno = ""
                if (
                    "label-info-list" in rls
                    and rls["label-info-list"]
                    and "label" in rls["label-info-list"][0]
                    and "name" in rls["label-info-list"][0]["label"]
                ):
                    label = rls["label-info-list"][0]["label"]["name"]
                    if "catalog_number" in rls["label-info-list"][0]:
                        catno = rls["label-info-list"][0]["catalog_number"]

                try:
                    source = rls["medium-list"][0]["format"]
                except KeyError:
                    source = None

                edition = ""
                if label:
                    edition += label
                if catno:
                    edition += " " + catno

                if label.lower() not in cfg.upload.search.excluded_labels:
                    releases[rls["id"]] = (
                        IdentData(artists, rls["title"], None, track_count, source),
                        self.format_result(
                            artists,
                            rls["title"],
                            edition,
                            ed_title=source,
                            track_count=track_count,
                        ),
                    )
            except (TypeError, IndexError) as e:
                raise ScrapeError("Failed to parse scraped search results.") from e
            if len(releases) == limit:
                break
        return "MusicBrainz", releases
