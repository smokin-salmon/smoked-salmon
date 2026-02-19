from abc import ABC, abstractmethod
from typing import Any

import asyncclick as click
import msgspec


class IdentData(msgspec.Struct, frozen=True):
    """Data structure for release identification."""

    artist: str
    album: str
    year: int | str | None
    track_count: int | None
    source: str


class ArtistRlsData(msgspec.Struct, frozen=True):
    """Data structure for artist release search results."""

    url: str
    quality: str | None
    year: int | str | None
    artist: str
    album: str
    label: str | None
    explicit: bool


class LabelRlsData(msgspec.Struct, frozen=True):
    """Data structure for label release search results."""

    url: str
    quality: str | None
    year: int | str | None
    artist: str
    album: str
    type: str | None
    explicit: bool


class SearchMixin(ABC):
    @abstractmethod
    async def search_releases(self, searchstr: str, limit: int) -> tuple[str, dict[str, Any] | None]:
        """
        Search the metadata site for a release string and return a dictionary
        of release IDs and search results strings.
        """
        pass

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
