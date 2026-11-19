"""File an upload under the edition a group already holds when that edition names the release in other words."""

import html
from typing import TYPE_CHECKING, Any

import asyncclick as click

from salmon import interaction
from salmon.common.strings import comparable
from salmon.errors import RequestError
from salmon.uploader.dupe_checker import remaster_text
from salmon.uploader.upload import generate_catno

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

# In remaster_text's order: site_edition zips the two.
_FIELDS = ("year", "edition_title", "label", "catno")
# Not the year: an edition is adopted only in our year.
_ADOPTED = ("edition_title", "label", "catno")


def _ours(metadata: dict[str, Any]) -> dict[str, str]:
    """Our edition as the upload form sends it."""
    sent = {**metadata, "catno": generate_catno(metadata)}
    return {field: str(sent.get(field) or "").strip() for field in _FIELDS}


def site_edition(torrent: Any) -> dict[str, str] | None:
    """A torrent's edition in the site's words, unescaped; None for an original release or a value not a dict."""
    text = remaster_text(torrent) if isinstance(torrent, dict) else None
    if text is None:
        return None
    return {field: html.unescape(value).strip() for field, value in zip(_FIELDS, text, strict=True)}


def in_edition_of(torrent: Any, metadata: dict[str, Any]) -> bool:
    """Whether the form files an upload in this torrent's edition, word for word."""
    held = site_edition(torrent)
    return held is not None and _ours(metadata) == held


def _is_barcode(catno: str) -> bool:
    """Whether a catalogue number is a barcode (a UPC or EAN: 12 digits or more), which names one product."""
    key = comparable(catno)
    return key.isdecimal() and len(key) >= 12


def _catno_key(catno: str) -> str:
    """comparable(), with a barcode stripped of leading zeros: a UPC-A and its EAN-13 are one catno."""
    key = comparable(catno)
    # "#" keeps a barcode apart from a label's number of the same digits: comparable() leaves no "#".
    return "#" + (key.lstrip("0") or "0") if _is_barcode(catno) else key


def names_ours(held: dict[str, str], ours: dict[str, str]) -> bool:
    """Whether a held edition is ours: year, label and catno agree, and the title too unless only the site has one."""
    if comparable(held["year"]) != comparable(ours["year"]) or comparable(held["label"]) != comparable(ours["label"]):
        return False
    catno = _catno_key(ours["catno"])
    if _catno_key(held["catno"]) != catno:
        return False
    title = comparable(ours["edition_title"])
    # A title only the site has ("feat. X") needs a barcode catno: a label's number may name several editions.
    return comparable(held["edition_title"]) == title or (
        not title and (_is_barcode(held["catno"]) or _is_barcode(ours["catno"]))
    )


def held_editions(group: Any, metadata: dict[str, Any]) -> list[dict[str, str]]:
    """The distinct editions of a torrentgroup response that name our release, in the site's words."""
    torrents = group.get("torrents") if isinstance(group, dict) else None
    if not isinstance(torrents, list):
        return []
    ours = _ours(metadata)
    editions: list[dict[str, str]] = []
    for torrent in torrents:
        held = site_edition(torrent)
        if held is not None and held not in editions and names_ours(held, ours):
            editions.append(held)
    return editions


def _line(edition: dict[str, str]) -> str:
    return " / ".join(edition[field] for field in _FIELDS if edition[field]) or "no edition fields"


async def adopt_edition(
    gazelle_site: "BaseGazelleApi", metadata: dict[str, Any], editions: list[dict[str, str]]
) -> dict[str, Any]:
    """metadata, or a copy filed under the one edition given that words it differently, if the user agrees."""
    ours = _ours(metadata)
    if not editions or ours in editions:
        return metadata
    site = gazelle_site.site_string
    if len(editions) > 1:
        click.secho(
            f"\n{len(editions)} editions on {site} look like this release: sending ours ({_line(ours)}).",
            fg="yellow",
        )
        return metadata
    held = editions[0]
    click.secho(f"\n{site} files this release as: {_line(held)}", fg="yellow", bold=True)
    click.secho(f"Ours reads: {_line(ours)}", fg="yellow")
    if await interaction.assume_defaults():
        click.secho(f"Using {site}'s edition fields (-yyy).", fg="yellow")
    elif not await interaction.confirm(click.style(f"Use {site}'s edition fields?", fg="magenta"), default=True):
        return metadata
    adopted = {field: held[field] for field in _ADOPTED}
    # A blank catno stays blank: generate_catno would send the UPC instead.
    return {**metadata, **adopted, **({} if held["catno"] else {"upc": ""})}


async def edition_in_group(
    gazelle_site: "BaseGazelleApi", group_id: int | None, metadata: dict[str, Any], group: dict[str, Any] | None = None
) -> dict[str, Any]:
    """metadata for an upload into group_id (None: a new group): see adopt_edition."""
    if not group_id:
        return metadata
    # group: as already fetched, or {} when that fetch failed, so it is not fetched again.
    try:
        group = group if group is not None else await gazelle_site.torrentgroup(group_id)
    except RequestError as err:
        click.secho(
            f"\nCould not read the editions of group {group_id} on {gazelle_site.site_string} ({err}): sending ours "
            f"({_line(_ours(metadata))}). Check the upload is not filed as a second edition.",
            fg="red",
        )
        return metadata
    return await adopt_edition(gazelle_site, metadata, held_editions(group, metadata))


async def edition_of_source(
    gazelle_site: "BaseGazelleApi", source_flac: dict[str, Any] | None, metadata: dict[str, Any]
) -> dict[str, Any]:
    """metadata for transcodes of a FLAC on the site, filed with it if it is in our year: see adopt_edition."""
    held = site_edition(source_flac)
    in_our_year = held is not None and comparable(held["year"]) == comparable(_ours(metadata)["year"])
    return await adopt_edition(gazelle_site, metadata, [held] if held is not None and in_our_year else [])
