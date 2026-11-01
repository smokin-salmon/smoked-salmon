"""Images uploaded already, given to ``salmon up`` by URL so that salmon does not upload them again (#795).

salmon never fetches a URL given here: whoever gives it vouches for the image. It only checks, before the run starts,
that the URL is on one of its image hosts and that every tracker of the run may show that host's images.
"""

import re
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from salmon.config.image_hosts import display_refusal, spectrals_refusal
from salmon.images import HOSTS, host_of_url

# A track's spectral images: Full, then Zoom.
MAX_SPECTRAL_URLS = 2
# What a URL may hold: the characters RFC 3986 allows, but brackets (an IPv6 host, which no image host uses), so that
# it cannot close the BBCode tag it goes into. No space, quote or angle bracket either.
_URL_CHARACTERS = re.compile(r"[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%]+")


def parse_spectral_urls(values: Iterable[str]) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Parse --spectral-url values, TRACK=URL[,URL] each, into (track, URLs) pairs in the order given.

    Raises:
        ValueError: A value is malformed or names a track already given; the message says which and why.
    """
    parsed: dict[int, tuple[str, ...]] = {}
    for value in values:
        track_text, equals, urls_text = value.partition("=")
        if not equals:
            raise ValueError(f"{value!r} is not TRACK=URL[,URL]")
        track_text = track_text.strip()
        if not track_text.isdigit() or int(track_text) < 1:
            raise ValueError(f"{track_text!r} in {value!r} is not a track number")
        track = int(track_text)
        if track in parsed:
            raise ValueError(f"track {track} is given twice")
        urls = tuple(url.strip() for url in urls_text.split(","))
        if not all(urls):
            raise ValueError(f"{value!r} has an empty URL")
        if len(urls) > MAX_SPECTRAL_URLS:
            raise ValueError(f"{value!r} has {len(urls)} URLs: a track takes at most two, Full then Zoom")
        parsed[track] = urls
    return tuple(parsed.items())


def track_refusal(tracks: Iterable[int], audio_files: Sequence[str]) -> str | None:
    """Why a track number given is not one of the album's, numbered as --spectrals numbers them; None if all are."""
    for track in tracks:
        if track > len(audio_files):
            return f"track {track} is not one of the album's: it has {len(audio_files)} audio file(s)"
    return None


def url_refusal(url: str, trackers: Sequence[str], spectrals: bool = False) -> str | None:
    """Why url may not stand for an image of an upload to trackers, or None if it may.

    Args:
        url: The image's URL.
        trackers: The site codes of every tracker the run may upload to.
        spectrals: Whether the image is a spectral, which some hosts may not take.
    """
    if not _URL_CHARACTERS.fullmatch(url):
        # A "]" would end the description's [img=...] tag and let the rest of the text add an image of any host.
        return f"{url} has characters an image URL in a description cannot have"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"{url} is not an http:// or https:// URL"
    if parts.username is not None or parts.query or parts.fragment:
        # The URL goes into a public description: RED's signed image URLs carry the viewer's signature in theirs.
        return f"{parts.hostname}: give the image's own URL, with no query, fragment or user name"
    host = host_of_url(url)
    if host is None:
        return f"{parts.hostname} is not one of salmon's image hosts ({', '.join(HOSTS)})"
    for tracker in trackers:
        reason = spectrals_refusal(host, tracker) if spectrals else display_refusal(host, tracker)
        if reason is not None:
            kind = "spectrals" if spectrals else "images"
            return f"{url} is on {host}, which can't be used for {tracker.upper()}'s {kind}: {reason}"
    return None
