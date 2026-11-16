"""Artist credits: the types of the release and track metadata, each source's claims, and the one function that
decides between them (ADR 0007).

Nothing here changes the metadata at runtime: the release and track stay dicts, and a module adopts the TypedDicts
when a PR touches it anyway.
"""

# pyright: strict

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, NotRequired, TypedDict

import msgspec

# The roles of ARTIST_IMPORTANCES. A source's role that maps to none of them is None, and never becomes main.
Role = Literal["main", "guest", "remixer", "composer", "conductor", "djcompiler", "producer", "arranger"]

# A credit as the metadata holds it: a name and a role.
Credit = tuple[str, Role]

# The roles a store's parser reads only on a release of the genre, beside its main, guest and remixer credits: RED's
# convention today.
EXTRA_ROLES: dict[str, frozenset[Role]] = {"classical": frozenset({"composer", "conductor"})}

# Who said it: a METASOURCES name, or the files' own tags.
Source = Literal["files", "MusicBrainz", "Apple Music", "Deezer", "Discogs", "Beatport", "Qobuz", "Tidal", "Bandcamp"]

# Main and guest are one performing credit per name per track (#852); the other roles are added together.
_PERFORMING = frozenset({"main", "guest"})


# The track, as the stores' generate_track and the files' create_track_list build it. "track#" and "disc#" aren't
# identifiers, hence the functional form.
TrackMetadata = TypedDict(
    "TrackMetadata",
    {
        "track#": str,
        "disc#": str,
        "tracktotal": Any,
        "disctotal": Any,
        "artists": list[Credit],
        "title": str | None,
        "replay_gain": Any,
        "peak": Any,
        "explicit": bool | None,
        "isrc": str | None,
        "format": str | None,
        "streamable": bool | None,
        "duration": int | None,
        # A store's only.
        "stream_id": NotRequired[str | int | None],
        "md5_origin": NotRequired[str | None],
        "media_version": NotRequired[str | None],
        "lossless": NotRequired[bool | None],
        "mp3_320": NotRequired[bool | None],
        "credited_remixers": NotRequired[list[str]],
        "artist_variations": NotRequired[list[dict[str, str]]],
        "assumed_artists": NotRequired[list[Credit]],  # a stating store's guesses, among its artists
        # The files' only.
        "numbered": NotRequired[bool],
    },
)


class ReleaseMetadata(TypedDict):
    """The release, as a store's scrape_release and the files' construct_rls_data build it."""

    artists: list[Credit]
    title: str | None
    group_year: Any
    year: Any
    date: str | None
    edition_title: str | None
    label: str | None
    catno: str | None
    rls_type: str | None
    genres: list[str]
    encoding: str | None
    encoding_vbr: bool | None
    scene: bool | None
    source: str | None
    cover: str | None
    upc: str | None
    comment: str | None
    urls: list[str]
    tracks: dict[str, dict[str, TrackMetadata]]
    format: NotRequired[str | None]  # the files'
    media: NotRequired[str | None]  # a store's
    url: NotRequired[str]  # a store's, until the merge
    tags: NotRequired[str]  # added when the metadata is confirmed


class RoleWords:
    """A store's words for its credits' roles, each to one of the eight or to None (a word salmon doesn't map).

    Words match whatever their case, spacing and punctuation: Qobuz's "Main Artist" is its "MainArtist".
    """

    def __init__(self, words: Mapping[str, Role | None]) -> None:
        self._roles: dict[str, Role | None] = {_fold(word): role for word, role in words.items()}

    def __contains__(self, word: str) -> bool:
        """Whether the word is one of the store's role words, mapped or not."""
        return _fold(word) in self._roles

    def role_of(self, word: str) -> Role | None:
        """The word's role; None for a word salmon doesn't map or the store doesn't use."""
        return self._roles.get(_fold(word))


def _fold(word: str) -> str:
    return "".join(char for char in word.casefold() if char.isalnum())


class Claim(msgspec.Struct, frozen=True):
    """What one source said about one artist on one track."""

    source: Source
    name: str
    credited_as: str  # the name exactly as this source printed it
    role: Role | None  # None: the source's role maps to none of the eight
    assumed: bool  # the role is a guess, not data the source gave

    def __post_init__(self) -> None:
        if self.assumed and self.role is None:
            raise ValueError(f"An assumed claim needs a role: {self.name} from {self.source}.")


Rule = Literal["stated over assumed", "first"]


class Reason(msgspec.Struct, frozen=True):
    """Why a credit was decided as it was."""

    rule: Rule | None  # the tie-break that decided a disputed credit; None: no source disputed it
    from_sources: tuple[Source, ...]  # the sources that gave the credit, the deciding one first
    disputed_by: tuple[Source, ...]  # the other sources that gave the same name another performing role


def resolve_credits[K](tracks: Mapping[K, Sequence[Claim]]) -> dict[K, list[tuple[Credit, Reason]]]:
    """Each track's credits, from the claims of every source, each with its reason.

    Main and guest are one credit per name per track, the other roles are added together. A stated role beats an
    assumed one; otherwise the first claim wins: the claims come in the merge's order, the base's first. The credits
    keep the order their names were first claimed in. A claim whose role maps to none is left out.

    Names are matched as given: which spellings are one artist is the caller's to settle first (ADR 0007).

    Args:
        tracks: Each track's claims, by any key the caller keeps.
    """
    return {key: _resolve_track(claims) for key, claims in tracks.items()}


def _resolve_track(claims: Sequence[Claim]) -> list[tuple[Credit, Reason]]:
    groups: dict[tuple[str, str], list[tuple[Claim, Role]]] = {}
    for claim in claims:
        if claim.role is not None:
            key = (claim.name, "" if claim.role in _PERFORMING else claim.role)
            groups.setdefault(key, []).append((claim, claim.role))
    decided: list[tuple[Credit, Reason]] = []
    for group in groups.values():
        winner, role = next((pair for pair in group if not pair[0].assumed), group[0])
        agreeing = _sources([winner, *(claim for claim, other in group if other == role)])
        # A source that also gave the kept role (a files' ARTIST naming Bob as main and as guest) disputes nothing.
        against = [claim for claim, other in group if other != role and claim.source not in agreeing]
        rule: Rule | None = None
        if against:
            stated = not winner.assumed and all(claim.assumed for claim in against)
            rule = "stated over assumed" if stated else "first"
        decided.append(((winner.name, role), Reason(rule, agreeing, _sources(against))))
    return decided


def _sources(claims: Iterable[Claim]) -> tuple[Source, ...]:
    return tuple(dict.fromkeys(claim.source for claim in claims))
