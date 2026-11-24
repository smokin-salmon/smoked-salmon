"""Upload only what a tracker's edition is missing (#1169): which of our formats the edition already holds."""

from dataclasses import dataclass
from typing import Any

import asyncclick as click

from salmon.uploader.conversion_choice import downconversion_format, get_downconversion_options, wanted_conversions
from salmon.uploader.dupe_checker import _site_edition as site_edition
from salmon.uploader.dupe_checker import describe_torrent, held_in_group


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


def edition_holdings(
    rset: dict[str, Any],
    release: dict[str, Any],
    conversions: dict[str, tuple[str, str]],
    wanted: frozenset[str] | None = None,
    source: dict[str, Any] | None = None,
) -> EditionHoldings:
    """Decide, for the source and each conversion, whether the release's edition in the group already holds it.

    The one place that decides it. It reads only the group listing given and sends no request. The match is
    held_in_group's: the edition's year and edition title, media, format and encoding.

    Args:
        rset: The group listing, a search row or a torrentgroup response.
        release: Our release, with its source, format, encoding, year and edition title.
        conversions: The format and encoding of each conversion offered, by name.
        wanted: The names of the conversions the run makes by default; None: all of them.
        source: The held torrent the conversions are filed as transcodes of, picked among several: the source is
            that one, and a conversion counts as held only in its edition as the site files it (catalogue number
            included). None: every match counts.

    Returns:
        Each decision with its reason, the source's first.
    """
    group_info = rset.get("group") or {}
    filed_under = site_edition(source, rset) if source is not None else None

    def decide(name: str, fmt: str, encoding: str) -> Holding:
        held = tuple(
            torrent
            for torrent in held_in_group(rset, {**release, "format": fmt, "encoding": encoding})
            if filed_under is None or site_edition(torrent, rset) == filed_under
        )
        if held:
            return Holding(name, held, f"this edition has {describe_torrent(held[0], group_info)}")
        return Holding(name, (), f"no torrent of this edition is {release.get('source')} / {fmt} / {encoding}")

    return EditionHoldings(
        source=(
            Holding("source", (source,), f"this edition has {describe_torrent(source, group_info)}")
            if source is not None
            else decide("source", release.get("format") or "", release.get("encoding") or "")
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
