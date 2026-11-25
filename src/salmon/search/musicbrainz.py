import re
from typing import Any

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.search.hints import SearchHints
from salmon.sources import MusicBrainzBase, is_placeholder_catno

# Lucene's special characters, as Picard escapes them.
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]\^"~*?:\\/])')


def fielded_query(hints: SearchHints) -> str:
    """The release search for the files' hints: artist, release, tracks and date terms, each left out when its hint
    is missing (#1178).

    A value is lower-cased, so that an upper-case AND, OR or NOT in a title is a word and not an operator (MusicBrainz
    matches case-blind), then escaped.
    """
    track_count = str(hints.track_count) if hints.track_count else None
    fields = [("artist", hints.artist), ("release", hints.album), ("tracks", track_count), ("date", hints.year)]
    values = [(field, value.strip().lower()) for field, value in fields if value]
    return " ".join(f"{field}:({_LUCENE_SPECIAL.sub(r'\\\1', value)})" for field, value in values if value)


def _label_and_catno(rls: dict[str, Any]) -> tuple[str, str]:
    """The first label-info's label name and catalogue number, "" for each it lacks; no catalogue number without a
    label, as the edition shows them."""
    infos = rls.get("label-info") or []
    label = (infos[0].get("label") or {}).get("name") if infos else None
    if not label:
        return "", ""
    catno = infos[0].get("catalog-number")
    return label, catno if catno and not is_placeholder_catno(catno) else ""


class Searcher(MusicBrainzBase, SearchMixin):
    def queries(self, searchstrs: list[str], hints: SearchHints | None) -> list[str]:
        """One fielded search when the files' hints say anything, in place of every searchstr; else the searchstrs."""
        query = fielded_query(hints) if hints is not None else ""
        return [query] if query else searchstrs

    async def search_releases(self, searchstr: str, limit: int) -> tuple[str, dict[str, Any]]:
        """Search for releases on MusicBrainz.

        Args:
            searchstr: Search string, sent as MusicBrainz's query unchanged.
            limit: Maximum number of results.

        Returns:
            Tuple of (source name, releases dict).
        """
        releases = {}
        result = await self.get_json("/release", {"query": searchstr, "limit": str(limit)})
        for rls in result["releases"]:
            try:
                parsed = self._parse_result(rls)
            except (TypeError, IndexError) as e:
                raise ScrapeError("Failed to parse scraped search results.") from e
            if parsed is not None:
                releases[rls["id"]] = parsed
            if len(releases) == limit:
                break
        return "MusicBrainz", releases

    def _parse_result(self, rls: dict[str, Any]) -> tuple[IdentData, str] | None:
        """One result's ident data and line; None for a release on an excluded label."""
        artists = "".join(credit["name"] + (credit.get("joinphrase") or "") for credit in rls["artist-credit"])
        track_count = rls.get("track-count")
        label, catno = _label_and_catno(rls)
        media = rls.get("media")
        # As from the XML: no media, no format; an empty list fails the search (IndexError).
        source = None if media is None else media[0].get("format")
        if label.lower() in cfg.upload.search.excluded_labels:
            return None
        return (
            IdentData(artists, rls["title"], None, track_count, source or ""),
            self.format_result(
                artists, rls["title"], self.edition(label, catno), ed_title=source, track_count=track_count
            ),
        )
