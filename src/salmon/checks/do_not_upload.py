"""Whether a tracker's Do-Not-Upload list forbids a release (#533).

RED and OPS each keep a list of releases they refuse: fakes, unreleased albums, bootlegs, some whole
discographies and labels. salmon ships a copy of each in data/do_not_upload/, which the maintainers keep current
with salmon's releases, so nothing is fetched. A release on a tracker's list never goes to that tracker; other
trackers are not affected, and a tracker with no list (DIC) refuses nothing.

The matching is chodeus's fork's (checks/blacklist.py), plus entries that name one media only.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from salmon.constants import ARTIST_IMPORTANCES, SOURCES

LISTS_DIR = Path(__file__).parents[1] / "data" / "do_not_upload"
# The file of each tracker that has a list.
LISTS = {"RED": "red.toml", "OPS": "ops.toml"}

_APOSTROPHES = re.compile(r"['’]")
_WORD = re.compile(r"[^\W_]+")


def _words(text: str) -> frozenset[str]:
    """The words of a name, whatever their case, accents and punctuation; "&" is "and", "World's" is "worlds"."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return frozenset(_WORD.findall(_APOSTROPHES.sub("", folded.replace("&", " and "))))


class Entry(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    """One entry of a list: an artist's whole discography, one release, any artist's release (a Various Artists
    compilation) or a label, limited to one media when `media` is given."""

    note: str
    artist: str = ""
    album: str = ""
    label: str = ""
    media: str = ""

    def __post_init__(self) -> None:
        if not self.note.strip():
            raise ValueError("an entry needs a note saying why")
        names = {"artist": self.artist, "album": self.album, "label": self.label}
        if not any(names.values()):
            raise ValueError("an entry needs an artist, an album or a label")
        for key, value in names.items():
            if value and not _words(value):
                raise ValueError(f"the {key} {value!r} has no word to match")
        if self.label and (self.artist or self.album):
            raise ValueError("a label entry names no artist or album")
        if self.media and self.media not in SOURCES.values():
            raise ValueError(f"the media {self.media!r} is none of {', '.join(SOURCES.values())}")

    def matches(self, release: "Candidate") -> bool:
        """Whether the release is this entry's: the artist is one of its main artists exactly ("Viper" is not
        "Viper UK"), and every word of the album is in its title and edition title, of the label in one label."""
        if self.media and self.media.casefold() != release.media.casefold():
            return False
        if self.artist and _words(self.artist) not in {_words(artist) for artist in release.artists}:
            return False
        if self.album and not _words(self.album) <= _words(f"{release.title} {release.edition_title}"):
            return False
        return not self.label or any(_words(self.label) <= _words(label) for label in release.labels)

    def describe(self) -> str:
        if self.label:
            subject = f"the label {self.label}"
        elif self.artist and self.album:
            subject = f"{self.artist} - {self.album}"
        elif self.artist:
            subject = f"{self.artist} (the whole discography)"
        else:
            subject = f"{self.album} (whatever the artist)"
        return f"{subject}{f' ({self.media} only)' if self.media else ''}"


class _List(msgspec.Struct, forbid_unknown_fields=True):
    entry: list[Entry] = msgspec.field(default_factory=list)


@dataclass(frozen=True)
class Candidate:
    """A release as it would be uploaded, for matching against the lists."""

    artists: tuple[str, ...]  # The main artists
    title: str
    edition_title: str = ""
    labels: tuple[str, ...] = ()  # The edition's and the group's
    media: str = ""

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> "Candidate":
        """The release `salmon up` uploads, from its reviewed metadata."""
        return cls(
            artists=tuple(name for name, role in metadata["artists"] if role == "main"),
            title=metadata["title"] or "",
            edition_title=metadata.get("edition_title") or "",
            labels=tuple(label for label in (metadata.get("label"),) if label),
            media=metadata.get("source") or "",
        )

    @classmethod
    def from_form(cls, data: dict[str, Any]) -> "Candidate":
        """The release an upload form describes."""
        roles = zip(data["artists[]"], data["importance[]"], strict=True)
        return cls(
            artists=tuple(name for name, importance in roles if importance == ARTIST_IMPORTANCES["main"]),
            title=data["title"] or "",
            edition_title=data.get("remaster_title") or "",
            labels=tuple(label for label in (data.get("remaster_record_label"), data.get("record_label")) if label),
            media=data.get("media") or "",
        )


def load_list(tracker: str) -> list[Entry]:
    """The entries of a tracker's list; none for a tracker without one.

    Raises:
        OSError, UnicodeDecodeError, msgspec.MsgspecError: If the file cannot be read, or an entry is not valid.
    """
    if tracker not in LISTS:
        return []
    return msgspec.toml.decode((LISTS_DIR / LISTS[tracker]).read_bytes(), type=_List).entry


def do_not_upload_reason(tracker: str, release: Candidate) -> str | None:
    """Why the tracker's Do-Not-Upload list forbids this release, or None if it does not.

    A list that cannot be read forbids everything: the reason then names its file.
    """
    try:
        entries = load_list(tracker)
    except (OSError, UnicodeDecodeError, msgspec.MsgspecError) as error:
        return (
            f"salmon cannot read its copy of {tracker}'s Do-Not-Upload list, {LISTS_DIR / LISTS[tracker]} ({error}), "
            f"so it cannot tell whether {tracker} refuses this release. Reinstall salmon to get the file back."
        )
    for entry in entries:
        if entry.matches(release):
            return (
                f"{entry.describe()} is on {tracker}'s Do-Not-Upload list: {entry.note} If yours is a legitimate "
                f"copy, {tracker} wants a message to its staff, with proof, before it is uploaded."
            )
    return None
