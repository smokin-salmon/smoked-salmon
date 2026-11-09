from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import asyncclick as click
import msgspec

# Inside each_failed_part_said_once, the styled line of each store, storefront or region that failed so far, by what.
_failed_parts: ContextVar[dict[str, str] | None] = ContextVar("failed_search_parts", default=None)


@contextmanager
def each_failed_part_said_once() -> Iterator[None]:
    """Print each failed store, storefront or region's line once, when the searches inside have finished."""
    lines: dict[str, str] = {}
    token = _failed_parts.set(lines)
    try:
        yield
    finally:
        _failed_parts.reset(token)
    for line in lines.values():
        click.echo(line)


def _say_once(what: str, line: str, **style: Any) -> None:
    """Print a failure's line, or inside each_failed_part_said_once keep the first one for what failed."""
    lines = _failed_parts.get()
    if lines is None:
        click.secho(line, **style)
    else:
        lines.setdefault(what, click.style(line, **style))


def say_part_failed(part: str, reason: object) -> None:
    """Say that a store's storefront or region failed, as "Apple Music: storefront jp". The first reason is kept."""
    _say_once(part, f"{part} failed: {reason}", fg="yellow")


def say_store_failed(store: str, line: str) -> None:
    """Say that a whole store's search failed, in the line handle_scrape_errors gives. The first is kept."""
    _say_once(store, line, fg="red", bold=True)


class IdentData(msgspec.Struct, frozen=True):
    """Data structure for release identification."""

    artist: str
    album: str
    year: int | str | None
    track_count: int | None
    source: str


class SearchMixin(ABC):
    @abstractmethod
    async def search_releases(self, searchstr: str, limit: int) -> tuple[str, dict[str, Any] | None]:
        """Return (source name, {release id: result}); None instead of the dict means inactive (no credentials)."""
        pass

    @staticmethod
    def edition(*parts) -> str:
        """The parts that are present, joined with spaces, so a missing year or label shows nothing, not "None"."""
        return " ".join(str(part) for part in parts if part)

    @staticmethod
    def format_result(
        artists,
        title,
        edition,
        track_count=None,
        ed_title=None,
        country_code=None,
        explicit=False,
        clean=False,
        additional_info=None,
    ):
        """
        Take the attributes of a search result and format them into a
        string with ANSI bells and whistles.
        """
        artists = click.style(artists, fg="yellow")
        title = click.style(title, fg="yellow", bold=True)
        result = f"{artists} - {title}"

        if track_count:
            result += f" {{Tracks: {click.style(str(track_count), fg='green')}}}"
        if ed_title:
            result += f" {{{click.style(ed_title, fg='yellow')}}}"
        if edition:
            result += f" {click.style(edition, fg='green')}"
        if explicit:
            result = click.style("[E] ", fg="red", bold=True) + result
        if clean:
            result = click.style("[C] ", fg="cyan", bold=True) + result
        if country_code:
            result = f"[{country_code}] " + result
        # Add any additional information that might be helpful to identify the release
        if additional_info:
            result += f" {additional_info}"

        return result
