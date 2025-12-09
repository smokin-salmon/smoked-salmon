from abc import ABC, abstractmethod
from collections import namedtuple

import asyncclick as click

IdentData = namedtuple("IdentData", ["artist", "album", "year", "track_count", "source"])
ArtistRlsData = namedtuple("ArtistRlsData", ["url", "quality", "year", "artist", "album", "label", "explicit"])
LabelRlsData = namedtuple("LabelRlsData", ["url", "quality", "year", "artist", "album", "type", "explicit"])


class SearchMixin(ABC):
    @abstractmethod
    async def search_releases(self, searchstr, limit):
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
