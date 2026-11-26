"""Upload only what a tracker's edition is missing (#1169): which of our formats the edition already holds."""

from dataclasses import dataclass
from typing import Any

import asyncclick as click

from salmon.uploader.conversion_choice import downconversion_format, get_downconversion_options, wanted_conversions
from salmon.uploader.dupe_checker import describe_torrent, filed_edition, held_in_group


@dataclass(frozen=True)
class Holding:
    """One format we would upload, and the edition's torrents that already hold it (none: missing)."""

    name: str
    torrents: tuple[dict[str, Any], ...]
    reason: str

    @property
    def torrent(self) -> dict[str, Any] | None:
        """The first torrent holding it, None when it is missing."""
        return self.torrents[0] if self.torrents else None


@dataclass(frozen=True)
class EditionHoldings:
    """What the edition holds of the source's format and of each conversion's."""

    source: Holding
    conversions: tuple[Holding, ...]
    # The conversions the run makes by default, by name; None: all of them.
    wanted: frozenset[str] | None = None

    @property
    def held_conversions(self) -> set[str]:
        """The names of the conversions the edition already holds."""
        return {holding.name for holding in self.conversions if holding.torrents}

    @property
    def wanted_conversions(self) -> tuple[Holding, ...]:
        """The conversions the run makes by default."""
        return tuple(holding for holding in self.conversions if self.wanted is None or holding.name in self.wanted)

    @property
    def nothing_missing(self) -> bool:
        """Whether the edition holds the source's format and every conversion made by default: nothing is left."""
        return bool(self.source.torrents) and all(holding.torrents for holding in self.wanted_conversions)


def _torrent_id(torrent: dict[str, Any]) -> str | None:
    """A listed torrent's id as text (an id sent as text is the same torrent); None when it has none."""
    return str(torrent["id"]) if torrent.get("id") is not None else None


def _source_reason(shown: dict[str, Any], source_id: str | None, group_info: dict[str, Any]) -> str:
    """Why the source's format is held: the source as listed, or as given when the listing lacks it."""
    if all(shown.get(key) for key in ("media", "format", "encoding")):
        return f"this edition has {describe_torrent(shown, group_info)}"
    # held_formats' callers may pass an id alone, of a torrent the listing given does not have.
    return f"torrent {source_id} is the source"


def edition_holdings(
    rset: dict[str, Any],
    release: dict[str, Any],
    conversions: dict[str, tuple[str, str]],
    wanted: frozenset[str] | None = None,
    source: dict[str, Any] | None = None,
    *,
    same_edition: bool = True,
) -> EditionHoldings:
    """Decide, for the source and each conversion, whether the release's edition in the group already holds it.

    The one place that decides it, for uploading only what is missing, --skip-flac-upload and cross-upload's
    conversions (held_formats). It reads only the group listing given and sends no request. The match is
    held_in_group's: the edition's year and edition title, media, format and encoding.

    Args:
        rset: The group listing, a search row or a torrentgroup response.
        release: Our release, with its source, format, encoding, year and edition title.
        conversions: The format and encoding of each conversion offered, by name.
        wanted: The names of the conversions the run makes by default; None: all of them.
        source: The FLAC the conversions are made from and filed as transcodes of: the held torrent picked among
            several, our FLAC just uploaded, or the one --skip-flac-upload transcodes. It is the source, and never
            holds a conversion. Found in the group (by id), a conversion counts as held only in its edition as the
            site files it (catalogue number included). None, or not in the group: every match counts.
        same_edition: Whether the conversions are filed under the source's edition. False: every match counts,
            the source's own torrent aside.

    Returns:
        Each decision with its reason, the source's first.
    """
    group_info = rset.get("group") or {}
    # A source with no id is no torrent of the listing: none without an id is taken for it.
    source_id = _torrent_id(source) if source is not None else None
    placed = next((t for t in rset.get("torrents") or [] if _torrent_id(t) == source_id), None) if source_id else None
    filed_under = filed_edition(placed, rset) if placed is not None and same_edition else None

    def decide(name: str, fmt: str, encoding: str) -> Holding:
        held = tuple(
            torrent
            for torrent in held_in_group(rset, {**release, "format": fmt, "encoding": encoding})
            if (source_id is None or _torrent_id(torrent) != source_id)
            and (filed_under is None or filed_edition(torrent, rset) == filed_under)
        )
        if held:
            return Holding(name, held, f"this edition has {describe_torrent(held[0], group_info)}")
        return Holding(name, (), f"no torrent of this edition is {release.get('source')} / {fmt} / {encoding}")

    return EditionHoldings(
        source=(
            decide("source", release.get("format") or "", release.get("encoding") or "")
            if source is None
            else Holding("source", (source,), _source_reason(placed or source, source_id, group_info))
        ),
        conversions=tuple(decide(name, fmt, encoding) for name, (fmt, encoding) in conversions.items()),
        wanted=wanted,
    )


def holdings_for_turn(
    site: str,
    group: dict[str, Any],
    metadata: dict[str, Any],
    rls_data: dict[str, Any],
    track_data: dict[str, Any],
    conversions: tuple[str, ...] | None,
    source: dict[str, Any] | None = None,
) -> EditionHoldings:
    """What a tracker's edition holds of what this run would upload there, each held format said in one line.

    Every conversion offered is decided, as the conversion question may pick any; the run makes those of
    --conversions, or upload.conversions, by default, as -yyy makes them.

    Args:
        site: The tracker's name, for the lines printed.
        group: The group, as fetched when the user chose to upload only what is missing.
        metadata: The reviewed release.
        rls_data: The release data the conversions are offered for.
        track_data: The tracks' audio info.
        conversions: The conversion kinds selected for the run (--conversions), or None for upload.conversions.
        source: The held torrent picked among several, if any (see edition_holdings).
    """
    options = get_downconversion_options(rls_data, track_data, quiet=True)
    wanted = frozenset(option["name"] for option in wanted_conversions(options, set(), conversions))
    offered = {option["name"]: downconversion_format(option) for option in options}
    holdings = edition_holdings(group, metadata, offered, wanted, source)
    ours = f"{metadata.get('source')} / {metadata.get('format')} / {metadata.get('encoding')}"
    for holding in (holdings.source, *holdings.wanted_conversions):
        if holding.torrents:
            name = ours if holding is holdings.source else holding.name
            click.secho(f"\nNot uploading {name} to {site}: {holding.reason}.", fg="yellow")
    return holdings
