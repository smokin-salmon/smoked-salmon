"""Upload only what a tracker's edition is missing (#1169): which of our formats the edition already holds."""

from dataclasses import dataclass
from typing import Any

import asyncclick as click

from salmon.uploader.conversion_choice import downconversion_format, get_downconversion_options, wanted_conversions
from salmon.uploader.dupe_checker import describe_torrent, held_in_group


@dataclass(frozen=True)
class Holding:
    """One format we would upload, and the edition's torrent that already holds it (None: missing)."""

    name: str
    torrent: dict[str, Any] | None
    reason: str


@dataclass(frozen=True)
class EditionHoldings:
    """What the edition holds of the source's format and of each conversion's."""

    source: Holding
    conversions: tuple[Holding, ...]

    @property
    def held_conversions(self) -> set[str]:
        """The names of the conversions the edition already holds."""
        return {holding.name for holding in self.conversions if holding.torrent is not None}

    @property
    def nothing_missing(self) -> bool:
        """Whether the edition holds the source's format and every conversion's: nothing is left to upload."""
        return self.source.torrent is not None and all(holding.torrent is not None for holding in self.conversions)


def edition_holdings(
    rset: dict[str, Any], release: dict[str, Any], conversions: dict[str, tuple[str, str]]
) -> EditionHoldings:
    """Decide, for the source and each conversion, whether the release's edition in the group already holds it.

    The one place that decides it. It reads only the group listing given (the one the group question printed: a
    search row or a fetched group) and sends no request. The match is held_in_group's: the edition's year and
    edition title, media, format and encoding.

    Args:
        rset: The group listing, a search row or a torrentgroup response.
        release: Our release, with its source, format, encoding, year and edition title.
        conversions: The format and encoding of each conversion the run would make, by name.

    Returns:
        Each decision with its reason, the source's first.
    """
    group_info = rset.get("group") or {}

    def decide(name: str, fmt: str, encoding: str) -> Holding:
        held = held_in_group(rset, {**release, "format": fmt, "encoding": encoding})
        if held:
            return Holding(name, held[0], f"this edition has {describe_torrent(held[0], group_info)}")
        return Holding(name, None, f"no torrent of this edition is {release.get('source')} / {fmt} / {encoding}")

    return EditionHoldings(
        source=decide("source", release.get("format") or "", release.get("encoding") or ""),
        conversions=tuple(decide(name, fmt, encoding) for name, (fmt, encoding) in conversions.items()),
    )


def holdings_for_turn(
    site: str,
    group: dict[str, Any],
    metadata: dict[str, Any],
    rls_data: dict[str, Any],
    track_data: dict[str, Any],
    conversions: tuple[str, ...] | None,
) -> EditionHoldings:
    """What a tracker's edition holds of what this run would upload there, each held format said in one line.

    The conversions are those the run makes by default (--conversions, or upload.conversions), as -yyy makes them.

    Args:
        site: The tracker's name, for the lines printed.
        group: The group listing the group question printed.
        metadata: The reviewed release.
        rls_data: The release data the conversions are offered for.
        track_data: The tracks' audio info.
        conversions: The conversion kinds selected for the run (--conversions), or None for upload.conversions.
    """
    options = wanted_conversions(get_downconversion_options(rls_data, track_data, quiet=True), set(), conversions)
    wanted = {option["name"]: downconversion_format(option) for option in options}
    holdings = edition_holdings(group, metadata, wanted)
    source = f"{metadata.get('source')} / {metadata.get('format')} / {metadata.get('encoding')}"
    for holding in (holdings.source, *holdings.conversions):
        if holding.torrent is not None:
            name = source if holding is holdings.source else holding.name
            click.secho(f"\nNot uploading {name} to {site}: {holding.reason}.", fg="yellow")
    return holdings
