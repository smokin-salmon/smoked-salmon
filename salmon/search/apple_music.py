import re
from itertools import zip_longest

import anyio

from salmon import cfg
from salmon.common import parse_copyright
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import AppleMusicBase


class Searcher(AppleMusicBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        # Group langs by storefront — search once per storefront
        sf_langs = _get_storefronts_and_langs()
        per_sf: dict[str, list] = {}

        async def _search_one(sf: str) -> None:
            try:
                resp = await self.get_json(
                    self.search_url,
                    params={
                        "media": "music",
                        "entity": "album",
                        "limit": min(limit, 25),
                        "term": searchstr,
                        "country": sf,
                    },
                )
            except Exception:
                per_sf[sf] = []
                return

            results = []
            for rls in resp.get("results", []):
                artists = rls.get("artistName", "")
                title = rls.get("collectionName", "")
                track_count = rls.get("trackCount")
                date = (rls.get("releaseDate") or "")[:10]
                year_match = re.search(r"(\d{4})", date)
                year = int(year_match[1]) if year_match else None
                copyright_str = parse_copyright(rls["copyright"]) if "copyright" in rls else None
                explicit = rls.get("collectionExplicitness") == "explicit"
                clean = rls.get("collectionExplicitness") == "cleaned"
                results.append(
                    (
                        sf,
                        rls["collectionId"],
                        (
                            IdentData(artists, title, year, track_count, "WEB"),
                            (explicit, clean, f"{year or ''} {copyright_str or ''}".strip()),
                        ),
                    )
                )
            per_sf[sf] = results

        async with anyio.create_task_group() as tg:
            for sf in sf_langs:
                tg.start_soon(_search_one, sf)

        # Interleave by rank across storefronts, then expand each result to all
        # configured langs for that storefront
        releases: dict = {}
        sf_order = list(sf_langs.keys())
        for rank in zip_longest(*[per_sf.get(sf, []) for sf in sf_order]):
            for entry in rank:
                if entry is None:
                    continue
                sf, collection_id, base_result = entry
                ident, _ = base_result
                for lang in sf_langs[sf]:
                    _, (explicit, clean, edition) = base_result
                    releases[(sf, lang, collection_id)] = (
                        ident,
                        self.format_result(
                            ident.artist,
                            ident.album,
                            edition,
                            track_count=ident.track_count,
                            country_code=f"{sf}:{lang}",
                            explicit=explicit,
                            clean=clean,
                        ),
                    )

        return "Apple Music", releases


def _get_storefronts_and_langs() -> dict[str, list[str]]:
    """Return ordered dict of storefront -> [lang, ...] from config.

    Preserves the order storefronts first appear, and the order of langs
    within each storefront.
    """
    result: dict[str, list[str]] = {}
    for entry in cfg.metadata.apple_music.storefronts:
        if ":" in entry:
            sf, lang = entry.split(":", 1)
        else:
            sf, lang = entry, "en-US"
        sf, lang = sf.strip(), lang.strip()
        if sf not in result:
            result[sf] = []
        if lang not in result[sf]:
            result[sf].append(lang)
    return result or {"us": ["en-US"]}
