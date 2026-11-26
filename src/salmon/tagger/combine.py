import contextlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Collection
from itertools import chain

import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common import normalize_accents
from salmon.common.credits import Claim, resolve_credits
from salmon.common.numbers import natural_key, number_or_text
from salmon.common.strings import comparable, restore_accents, same_artist_key, unbracketed
from salmon.errors import TrackCombineError
from salmon.tagger.pairing import pair_by_content
from salmon.tagger.sources import METASOURCES, without_merge_only_keys
from salmon.tagger.sources.base import (
    artist_spellings,
    construct_replacement_list,
    determine_label_type,
    featuring_clause,
    fix_artists_list,
    folded_title,
    generate_artists,
    standardize_genres,
    title_with_version,
)

PREFERENCES = [
    "Tidal",
    "Deezer",
    "Qobuz",
    "Bandcamp",
    "MusicBrainz",
    "Discogs",
    "Beatport",
    "Apple Music",
]


def get_source_from_link(url):
    if not url:
        return None
    for name, source in METASOURCES.items():
        if source.Scraper.regex.match(url):
            return name


def combine_metadatas(*metadatas, base=None, source_url=None):  # noqa: C901
    """
    This function takes a bunch of chosen metadata and splices
    together values to form one unified metadata dictionary.
    It runs through them in the order of the sources specified in
    the PREFERENCES list. Nonexistent data is replaced by existing
    data, and some are combined, like release comments. Due to this,
    it's fairly important that the base metadata contain the correct
    number of tracks.
    """
    url_sources: set[str] = set()
    if base and base.get("url", False):
        link_source = get_source_from_link(base["url"])
        if link_source:
            url_sources.add(link_source)

    sources = sort_metadatas(metadatas)

    source = get_source_from_link(source_url)
    ordered_preferences = ([source] if source in PREFERENCES else []) + [p for p in PREFERENCES if p != source]

    from_preferred_source = True
    # Who the base's credits come from: the files, or with none the first store (descgen, salmon web).
    base_source = "files" if base is not None else None
    claims = {}  # each base track's claims, by id(track), for _decide_credits

    for pref in ordered_preferences:
        for metadata in sources[pref]:
            if base is None:
                base = metadata
                base_source = pref
                if base.get("url", False):
                    link_source = get_source_from_link(base["url"])
                    if link_source:
                        url_sources.add(link_source)
                from_preferred_source = False
                continue

            base["genres"] += metadata["genres"]

            # The first selected metadata source should be able to replace stale
            # embedded/local title data. Otherwise WEB uploads can preserve a
            # composite ALBUM tag from the source files even after selecting a
            # clean scraped release title.
            if from_preferred_source:
                if metadata["title"]:
                    base["title"] = metadata["title"]
                if metadata["edition_title"] is not None:
                    base["edition_title"] = metadata["edition_title"]

            with contextlib.suppress(TrackCombineError):
                base["tracks"] = combine_tracks(
                    base["tracks"], metadata["tracks"], from_preferred_source, claims, (base_source, pref)
                )

            if (
                (not base["catno"] or not base["label"])
                and metadata["label"]
                and metadata["catno"]
                and (not base["label"] or any(w in metadata["label"] for w in base["label"].split()))
            ) and (base["source"] != "WEB" or (base["source"] == "WEB" and from_preferred_source)):
                base["label"] = metadata["label"]
                base["catno"] = metadata["catno"]

            if not base["label"] and metadata["label"]:
                base["label"] = metadata["label"]
                if not base["catno"] and metadata["catno"]:
                    base["catno"] = metadata["catno"]

            if metadata["comment"]:
                if not base["comment"]:
                    base["comment"] = metadata["comment"]
                else:
                    base["comment"] += f"\n\n{'-' * 32}\n\n" + metadata["comment"]

            if not base["cover"]:
                base["cover"] = metadata["cover"]
            if not base["edition_title"]:
                base["edition_title"] = metadata["edition_title"]
            if not base["year"]:
                base["year"] = metadata["year"]
            if not base["group_year"] or (
                str(metadata["group_year"]).isdecimal() and int(metadata["group_year"]) < int(base["group_year"])
            ):
                base["group_year"] = metadata["group_year"]
            if not base["date"] and metadata["date"]:
                base["date"] = metadata["date"]
                if metadata["year"]:
                    base["year"] = metadata["year"]
                if str(metadata["group_year"]).isdecimal():
                    base["group_year"] = metadata["group_year"]
            if not base["rls_type"] or base["rls_type"] == "Album":
                base["rls_type"] = metadata["rls_type"]
            if not base["upc"]:
                base["upc"] = metadata["upc"]

            from_preferred_source = False

        if sources[pref] and base is not None:
            # Process URLs from all metadata entries from this source
            for metadata in sources[pref]:
                if "url" in metadata:
                    link_source = get_source_from_link(metadata["url"])
                    if link_source and metadata["url"] not in base["urls"]:
                        base["urls"].append(metadata["url"])
                        url_sources.add(link_source)

    if base is None:
        raise ValueError("No metadata provided to combine")

    assert base is not None  # Type narrowing for pyright

    if not base["group_year"]:
        base["group_year"] = base["year"]

    if "url" in base:
        del base["url"]

    _drop_fragments(base["tracks"], claims)
    # The titles first, from the performers as the sources credited them, before the decision corrects their names and
    # joins split ones: a featuring clause names them that way, and deciding changes only their roles.
    _decide_titles(base["tracks"], base_source, claims)
    decisions, joined = _decide_credits(base["tracks"], claims, base_source, corrected=True)
    # After the decision: a base track no store paired is decided from its own marks.
    without_merge_only_keys(base)
    _print_joined(joined)
    _print_disputes(decisions)
    base["artists"], base["tracks"] = generate_artists(base["tracks"])
    base["genres"] = standardize_genres(_correct_genres(base["genres"]))
    base["label"] = determine_label_type(base["label"], base["artists"])
    return base


def _correction_key(name):
    return name.strip().casefold()


def _artist_corrections():
    return {_correction_key(name): to for name, to in cfg.metadata.corrections.artists.items()}


def _correct_genres(genres):
    """Each genre [metadata.corrections] genres names, replaced by its genres there (none drops it).

    Matched on each genre standardized on its own, as the review shows it: the stores' genres already are, the
    files' own may be an alias ("Electronica"). One at a time, so a combination does not first drop a genre it
    contains ("Rock" beside "Pop & Rock").
    """
    corrections = {_correction_key(name): to for name, to in cfg.metadata.corrections.genres.items()}
    if not corrections:
        return genres
    shown = [standardized for genre in genres for standardized in standardize_genres([genre])]
    return list(dict.fromkeys(to for genre in shown for to in corrections.get(_correction_key(genre), [genre])))


def sort_metadatas(metadatas):
    """Split the metadatas by source."""
    sources = defaultdict(list)
    for source, md in metadatas:
        sources[source].append(md)
    return sources


def _extract_remixers_from_title(title):
    # Mix types that don't indicate a remixer when alone
    common_mix_types = {
        "original",
        "extended",
        "radio",
        "club",
        "instrumental",
        "acoustic",
        "album",
        "vocal",
        "main",
        "dub",
        "edit",
    }

    # Match patterns like (Remixer Remix), (Remixer Club Remix), etc. Only "Remix" names a remixer: "Mix" and "Edit"
    # are far more often a version ("Extended Disco Mix", "Radio Edit") than a person (#1061).
    match = re.search(r"\(([^()]*?)\s+(?:Club|Radio|Vocal|Dub|Extended)?\s*Remix\)", title, re.IGNORECASE)
    if match:
        remixers = match.group(1).strip()
        # Split on commas, semicolons and slashes, which separate ("LP/single"), and on "&" and "+" only with a space
        # on each side: unspaced, they join a name ("Losers Middlesex A&E").
        remixer_list = [r.strip() for r in re.split(r"\s*[,;/]\s*|\s+[&+]\s+", remixers)]
        # Filter out common mix types and return remaining as remixers
        return [(r, "remixer") for r in remixer_list if r.lower() not in common_mix_types]
    return []


def _sorted_naturally(items):
    return sorted(items, key=lambda item: natural_key(item[0]))


def _pair_base_tracks(btracks, mtracks):
    """Order the local track dicts to line up with the scraped tracklist.

    Returns a list of length ``len(mtracks)`` where entry ``i`` is the local
    track belonging to ``mtracks[i]``, or ``None`` if no local track is left for
    it (the caller raises TrackCombineError for those), and how each was paired.
    Local tracks beyond the scraped tracklist are left unclaimed and untouched.

    Tracks numbered by their files' own TRACKNUMBER tags keep their numbers unless
    an ISRC or an exact title says otherwise. A track dict without the "numbered"
    key (another source's) counts as numbered.
    """
    numbered = all(track.get("numbered", True) for track in btracks)
    pairing, reasons = pair_by_content(_with_folded_titles(btracks), _with_folded_titles(mtracks), numbered=numbered)
    return [None if bi is None else btracks[bi] for bi in pairing], reasons


def _with_folded_titles(tracks):
    """The tracks with each title as its store folds it, for pairing: the files' titles hold their versions. A base
    track is also known by each title a store merged before gave it, so a track one store paired (by its ISRC, say) is
    found by the next store's title for it. In pairing's second title pass every title counts as written too ("Song"
    for "Song", "Remastered", by strip_useless_versions), as files salmon tagged before may hold it."""
    return [_for_pairing(track) for track in tracks]


def _for_pairing(track):
    named = [(track["title"], track.get("version")), *track.get("store_titles", [])]
    folded = [folded_title({"title": title, "version": version}) for title, version in named]
    written = [title_with_version(title, version) for title, version in named if title]
    return {**track, "title": folded[0], "titles": tuple(folded[1:]), "written_titles": tuple(written)}


def _numbers_usable(meta):
    """True when, on every disc of the source, the track numbers are exactly 1..n (n = the disc's track count),
    read as decimal integers. One disc that is not makes the whole source unusable for numbers (vinyl sides on
    Discogs come as `A1`, `S` or `5`)."""
    for disc in meta.values():
        nums = []
        for num in disc:
            text = str(num).strip()
            if not (text.isascii() and text.isdecimal()):
                return False
            nums.append(int(text))
        if sorted(nums) != list(range(1, len(nums) + 1)):
            return False
    return True


def _in_source_order(meta):
    """The source's discs in natural order, each with its (number, track) pairs: by number where a disc's numbers are
    all digits (a store may list them out of order), else as the source gave them, since vinyl positions ("S" and
    "5" on Discogs, #765) have no order of their own."""
    return [
        (disc, _sorted_naturally(tracks.items()) if all(str(n).isdecimal() for n in tracks) else list(tracks.items()))
        for disc, tracks in _sorted_naturally(meta.items())
    ]


def _new_track_numbers(mtracks, ordered, usable, update_track_numbers):
    """The new track number of each paired local track (None where there is no local track), in the order of
    `mtracks`. Used by both the merge and the check that it loses no track, so the two never disagree.

    A usable source: its number when `update_track_numbers`, else the file's own. An unusable source: a file
    numbered by its own TRACKNUMBER tag keeps it, and any other file gets its place (1..n) among the paired
    tracks of its disc, in the source's order."""
    seen: dict[int | str, int] = {}
    result = []
    for mtrack, btrack in zip(mtracks, ordered, strict=True):
        if btrack is None:
            result.append(None)
            continue
        disc = number_or_text(btrack["disc#"])
        seen[disc] = place = seen.get(disc, 0) + 1
        if usable:
            new_num = mtrack["track#"] if (update_track_numbers and mtrack["track#"]) else btrack["track#"]
        elif btrack.get("numbered", True):
            new_num = btrack["track#"]
        else:
            new_num = str(place)
        result.append(new_num)
    return result


def _slot(disc, num):
    return (number_or_text(disc), number_or_text(num))


def _refuse_renumbering_that_loses_a_track(btracks, mtracks, ordered, usable, update_track_numbers):
    """Raise TrackCombineError, before anything is merged, if two local tracks would end up in one slot, so one
    would be overwritten and lost: a paired track taking the number of a track the source has no track for, or
    two paired tracks taking the same number on one disc (a track keeps its own disc when it is renumbered).
    Pairing by content can pair a local track anywhere in the folder, not only at its own rank."""
    new_numbers = _new_track_numbers(mtracks, ordered, usable, update_track_numbers)
    paired = {id(btrack) for btrack in ordered if btrack is not None}
    taken = {_slot(btrack["disc#"], btrack["track#"]) for btrack in btracks if id(btrack) not in paired}
    for btrack, new_num in zip(ordered, new_numbers, strict=True):
        if btrack is None:
            continue  # The merge raises for a scraped track without a local one.
        slot = _slot(btrack["disc#"], new_num)
        if slot in taken:
            raise TrackCombineError(f"Disc {slot[0]} track {slot[1]} would hold two local tracks, losing one.")
        taken.add(slot)


def _print_pairing(mtracks, ordered, reasons):
    """Show how local files were matched, so a bad pairing is visible before tagging."""
    if not any(r and r != "position" for r in reasons):
        return  # Paired by position only, as before: nothing worth reporting.
    click.secho("\nMatched local tracks to the metadata source:", fg="yellow", bold=True)
    for mtrack, btrack, why in zip(mtracks, ordered, reasons, strict=False):
        if btrack is None:
            click.secho(f"  {mtrack['track#']:>3}. {folded_title(mtrack)} -> (no local track)", fg="red")
            continue
        moved = str(btrack["track#"]) != str(mtrack["track#"])
        click.secho(
            f"  {mtrack['track#']:>3}. {folded_title(mtrack)} <- {folded_title(btrack)} "
            f"(was track {btrack['track#']}, matched on {why})",
            fg="red" if why == "position" else ("yellow" if moved else "green"),
        )


def combine_tracks(base, meta, update_track_numbers, claims=None, sources=None):  # noqa: C901
    """Combine the metadata for the tracks of two different sources.

    With `claims` (a dict) and `sources` (the base's and the store's names), every source's claims about each base
    track's artists are added to `claims`, by id(track), for `_decide_credits` once the last store is merged.
    """
    base_source, store = sources or (None, None)
    # Track dictionaries reflect discovery order, so sort discs and tracks naturally before pairing.
    btracks_sorted = [
        [track for _, track in _sorted_naturally(disc.items())] for _, disc in _sorted_naturally(base.items())
    ]
    btracks_flat = list(chain.from_iterable(btracks_sorted))

    # Pair on content instead of on sorted rank, so unnumbered local files don't
    # have to already be in the source's real tracklist order.
    scraped_discs = _in_source_order(meta)
    mtracks_flat = [track for _, tracks in scraped_discs for _, track in tracks]
    ordered_btracks, pair_reasons = _pair_base_tracks(btracks_flat, mtracks_flat)
    usable = _numbers_usable(meta)
    new_numbers = iter(_new_track_numbers(mtracks_flat, ordered_btracks, usable, update_track_numbers))
    _refuse_renumbering_that_loses_a_track(btracks_flat, mtracks_flat, ordered_btracks, usable, update_track_numbers)
    _print_pairing(mtracks_flat, ordered_btracks, pair_reasons)

    # If the source already provides remixer credits, skip title-based inference
    # to avoid adding spurious artists from mix names like "Attitude Dub Mix".
    meta_has_remixer = any(
        role == "remixer" for tracks in meta.values() for track in tracks.values() for _, role in track["artists"]
    )

    # Before the merge: combine_metadatas goes on past a TrackCombineError, keeping whatever was merged.
    scraped = [(disc, num) for disc, tracks in scraped_discs for num, _ in tracks]
    for (disc, num), btrack in zip(scraped, ordered_btracks, strict=True):
        if btrack is None:
            raise TrackCombineError(f"Disc {disc} track {num} does not exist.")

    # Track numbers are applied after the merge loop; renumbering in place can
    # overwrite a base track that hasn't been visited yet once pairing reorders.
    renumbered = []

    # One entry per scraped track, in the order this loop visits them.
    paired_btracks = iter([btrack for btrack in ordered_btracks if btrack is not None])
    for _disc, tracks in scraped_discs:
        for _num, track in tracks:
            btrack = next(paired_btracks)

            if _untitled(btrack["title"]):
                # The base names no title (a ripper's "Track 01", or a store's): the first store that names it does, and
                # the stores after pair by that name. Gathered, not decided: _decide_titles writes it as that store's.
                btrack["title"], btrack["version"] = track["title"], track.get("version")
                btrack["placeholder_from"] = store
            # Each store's title and version beside the base's, for _decide_titles once the last store is merged.
            btrack.setdefault("store_titles", []).append((track["title"], track.get("version")))

            base_artists = {(comparable(a[0]), a[1]) for a in btrack["artists"]}
            base_keys = {(same_artist_key(a[0]), a[1]) for a in btrack["artists"]}
            btrack["artists"] = list(btrack["artists"])
            variations = {(v["name"], v["role"]): v["anv"] for v in track.get("artist_variations", [])}
            own = None if claims is None else claims.setdefault(id(btrack), _claims_of(btrack, base_source))
            if own is not None:
                own += _store_claims(track["artists"], variations, store, track.get("assumed_artists", ()))
                # Discogs' names and the names it credits them as, for _decide_credits to read as one artist. Gathered,
                # not decided: the base's own (Discogs as the base) are on it already.
                btrack.setdefault("artist_variations", []).extend(track.get("artist_variations", []))
            for a in track["artists"]:
                if (comparable(a[0]), a[1]) in base_artists:
                    continue
                # The tags may spell the artist the way the release credits them (Discogs ANV), with or without "The"
                anv = variations.get(a)
                if anv and (same_artist_key(anv), a[1]) in base_keys:
                    continue
                btrack["artists"].append(a)

            if _reads_title_remixers(track, store, meta_has_remixer):
                remixers = _extract_remixers_from_title(folded_title(track))
                for remixer in remixers:
                    if (comparable(remixer[0]), remixer[1]) not in base_artists:
                        btrack["artists"].append(remixer)
                if own is not None:
                    own += _store_claims(remixers, {}, store, guessed=remixers)

            btrack["credited_remixers"] = list(
                dict.fromkeys([*btrack.get("credited_remixers", []), *track.get("credited_remixers", [])])
            )

            if track["explicit"]:
                btrack["explicit"] = True
            if not btrack["format"]:
                btrack["format"] = track["format"]
            if not btrack["isrc"]:
                btrack["isrc"] = track["isrc"]
            if not btrack["replay_gain"]:
                btrack["replay_gain"] = track["replay_gain"]
            # A source whose numbering is not 1..n on each disc has no totals to give either.
            if usable and not btrack["tracktotal"]:
                btrack["tracktotal"] = track["tracktotal"]
            if usable and not btrack["disctotal"]:
                btrack["disctotal"] = track["disctotal"]
            new_num = next(new_numbers)
            renumbered.append((btrack, btrack["disc#"], btrack["track#"], new_num))

    # Clear every old slot before filling the new ones, so two tracks swapping
    # positions doesn't leave one of them deleted.
    for _, disc_no, old_num, _ in renumbered:
        base[disc_no].pop(old_num, None)
    for btrack, disc_no, _, new_num in renumbered:
        btrack["track#"] = new_num
        base[disc_no][new_num] = btrack
    return base


def _reads_title_remixers(track, store, meta_has_remixer):
    """Whether a store's track title is read for remixers: not once remixers are credited, to avoid adding spurious
    artists from mix names ("Attitude Dub Mix"). A store that states its credits says so of each track; any other's
    one credited remixer (e.g. from Discogs) says it of the whole release."""
    if store in METASOURCES and METASOURCES[store].Scraper.states_credits:
        return not any(role == "remixer" for _, role in track["artists"])
    return not meta_has_remixer


def _assumed(source, credit, guesses):
    """Whether a source's credit counts as assumed: every credit of a store until its own step-3 PR reads them (its
    scraper states_credits), or one among `guesses`, the credits the source marked by the name and role it gave them (a
    stating store's assumed_artists). The files' plain ARTIST counts as stated until step 4 (ADR 0007)."""
    return (source != "files" and not METASOURCES[source].Scraper.states_credits) or credit in guesses


def _claims_of(track, source):
    """A track's claims from its own credits and marks: the base's, or a scrape's used alone, each credited as Discogs
    credits it there (its own `artist_variations`), as a store's claims are."""
    variations = {(v["name"], v["role"]): v["anv"] for v in track.get("artist_variations", ())}
    return _store_claims(track["artists"], variations, source, track.get("assumed_artists", ()))


def _store_claims(credits, variations, source, guessed):
    """A store's credits on a base track as claims, each under the name the store gives it and the name it credits it
    as (Discogs' ANV). Which names are one artist is decided later, by _artist_classes."""
    return [
        Claim(source, name, variations.get((name, role)) or name, role, assumed=_assumed(source, (name, role), guessed))
        for name, role in credits
    ]


# A ripper's placeholder: "Track 01", "Audio Track 1", and its French, Spanish, Italian, German, Portuguese words.
_RE_PLACEHOLDER_TITLE = re.compile(r"\s*(?:audio\s*)?(?:track|piste|pista|traccia|titel|faixa)\s*\d+\s*", re.IGNORECASE)


def _untitled(title):
    """Whether a base track's title names nothing: none, or a ripper's placeholder ("Track 01")."""
    return not title or bool(_RE_PLACEHOLDER_TITLE.fullmatch(title))


def _decide_titles(tracks, base_source, claims):
    """Write each track's one title, from its own and each store's beside it (`store_titles`), in place. A featuring
    clause may name a performer under any name a source gave it, Discogs' credited name (ANV) included."""
    for disc in tracks.values():
        for track in disc.values():
            own = claims.get(id(track)) or _claims_of(track, base_source)
            # Only an artist still credited as a performer on the track: a fragment the merge dropped performs nothing.
            keys = {same_artist_key(name) for name, role in track["artists"] if role in {"main", "guest"}}
            performers = {
                comparable(name)
                for claim in own
                if claim.role in {"main", "guest"}
                and {same_artist_key(claim.name), same_artist_key(claim.credited_as)} & keys
                for name in (claim.name, claim.credited_as)
            }
            track["title"], _why = _written_title(track, base_source == "files", _credited(track) | performers)


def _credited(track):
    """Who a featuring clause may name as already credited, by comparable: the track's performers, not its producer
    or composer, whose credit says nothing of a guest."""
    return {comparable(name) for name, role in track.get("artists") or () if role in {"main", "guest"}}


def written_files_title(title, track):
    """A files title as the merge writes it for `track` (`_files_title`): "Song (feat. B)" is "Song" where the track
    credits B. The retagger knows each file by it."""
    return _files_title(title, _credited(track))


def _written_title(track, files, credited):
    """The track's title and its reason: which text it is, then what changed it. A placeholder the merge named from a
    store is that store's title, the files' title is written as it is (`_files_title`), a store's with its version
    (`_store_title`), each with the accents a store restores. What changed it is read from what is written: the title
    written without that step differs. The reason is for the tests, and for saying why a title is as it is when that is
    wanted."""
    title, stores = track["title"], track.get("store_titles", [])
    if not title:
        return title, "no title"
    if "placeholder_from" in track:
        own, why = False, f"{track['placeholder_from'] or 'a store'}'s title, for a placeholder"
    elif files:
        own, why = True, "the files' title"
    else:
        own, why = False, "the store's title and version"

    version = track.get("version")  # a files track holds one only once a placeholder took a store's title

    def write(text, kept, names):
        return _files_title(text, names) if own else _store_title(text, kept, names)

    accented, accented_version = _accented(title, version, stores)
    written = write(accented, accented_version, credited)
    if written != write(title, version, credited):
        why += ", with a store's accents"
    if written != write(accented, accented_version, set()):
        why += ", without a credited featuring clause"
    return written, why


def _accented(title, version, stores):
    """The title and version with the accents a store restores (`_with_store_accents`), the two read together as the
    store folds them, so a version's accents come back too ("Tiesto Remix" beside a store's "Tiësto Remix")."""
    if version:
        folded = f"{title} ({version})"
        accented = _with_store_accents(folded, stores)
        # Restoring keeps each letter's place in the composed text it gives, so it splits back where it was joined.
        composed = unicodedata.normalize("NFC", title)
        if len(accented) == len(unicodedata.normalize("NFC", folded)):
            return accented[: len(composed)], accented[len(composed) + 2 : -1]
    return _with_store_accents(title, stores), version


def _files_title(title, credited):
    """The files' title as it is, but for a trailing featuring clause under strip_useless_versions that names only
    artists the track credits (`credited`, by comparable), as the credit says it already. What the release says is not
    trimmed otherwise, and no store's version is added."""
    clause = featuring_clause(title)
    if cfg.upload.formatting.strip_useless_versions and clause and _names_only(clause[1], credited):
        return clause[0]
    return title


def _store_title(title, version, credited):
    """A store's title written with its version by strip_useless_versions (title_with_version), a featuring clause in
    its title (as `_files_title`, "ft." too) or its version ("ft. Mary J Blige") going only as the files' does: when
    the track credits them."""
    guests = featuring_clause(f"({bare})") if (bare := unbracketed(version)) else None
    return title_with_version(
        _files_title(title, credited),
        version,
        keep_featuring=not (guests and _names_only(guests[1], credited)),
        keep_title_featuring=True,
    )


# What separates two names in a featuring clause, besides a comma.
_SEPARATORS = r"[&+/;|＆、]"


def _names_only(names, credited):
    """Whether `names` ("A & B", a featuring clause's) are only artists in `credited` (by comparable), however they're
    joined: "Tyler, The Creator" is one. A joining word ("and", "with", "x") joins only between two credited names,
    with no separator beside it but a comma before it ("A, B, and C"), so the "X" of "Alice & X" and of
    "Alice & X & Bob" is a name. A name's own marks beside it are no separator ("M.I.A. and Bob", "Wham! x Bob")."""
    parts = re.split(r"(\W+)", names)  # words, with what separates them between
    found = [
        (word, parts[at - 1] if at else "", parts[at + 1] if at + 1 < len(parts) else "")
        for at in range(0, len(parts), 2)
        if (word := comparable(parts[at]))
    ]
    words = [word for word, _, _ in found]
    pieces, start = [], 0
    while start < len(words):
        # The longest run of words from here that is a credited name, "" for it ("Tyler, The Creator" is three).
        end = next((end for end in range(len(words), start, -1) if "".join(words[start:end]) in credited), None)
        word, before, after = found[start]
        joins = (
            word in {"and", "with", "x"}
            and not re.search(_SEPARATORS, before)
            and not re.search(f",|{_SEPARATORS}", after)
        )
        pieces.append("" if end else "+" if joins else word)
        start = end or start + 1
    return bool(pieces) and pieces[0] == pieces[-1] == "" and set(pieces) <= {"", "+"}


def _bare_head(title):
    """The title without the bracketed versions it ends in: "Song (Live) [2011]" is "Song"."""
    return re.sub(r"(?:\s*[\(\[][^()\[\]]*[\)\]])+\s*$", "", title) or title


def _with_store_accents(title, stores):
    """The title with the accents the first store to give them restores, letter by letter: the whole title first, as
    the store folds it or as it gives it ("Song (Tiesto Remix)" and "Song", "Tiësto Remix"), then each without the
    versions it ends in ("Ny Batteri (Live)" and "Ný batterí (Remastered)")."""
    for store_title, store_version in stores:
        if not store_title:
            continue
        # A version as given or out of the brackets it may come in ("(Rehearsal)"), as the title holds it.
        folded = [f"{store_title} ({given})" for given in (unbracketed(store_version), store_version) if given]
        for theirs in dict.fromkeys((*folded, store_title)):
            if (accented := restore_accents(title, theirs)) != title:
                return accented
        head = _bare_head(title)
        if (accented := restore_accents(head, _bare_head(store_title))) != head:
            return accented + title[len(head) :]
    return title


def _drop_fragments(tracks, claims):
    """Drop each paired track's fragments (#1112), once, after the last store, so the guard sees every name Discogs
    credits on the track."""
    for track in (track for disc in tracks.values() for track in disc.values() if id(track) in claims):
        guesses = _guesses(track["artists"], claims[id(track)], track.get("artist_variations", ()))
        track["artists"] = check_for_artist_fragments(track["artists"], track.get("artist_variations", ()), guesses)


def _decide_credits(tracks, claims, base_source, corrected=False):
    """Decide each track's credits from its claims (ADR 0007), in place. Returns the reasons and the names read as one
    artist beyond what `comparable` joins (_print_joined).

    Each claim first takes its artist's spelling (_artist_classes), so two spellings of one artist are one name to
    decide on. With `corrected`, the configured artist corrections apply to that spelling, once, so a corrected artist
    keeps every spelling it had, and a corrected part joins under its new name, as it did when the repair ran after the
    corrections. A claim whose artist the merge dropped in that role (a fragment of another name) is left out. Every
    kept credit has a claim: the merge claims each credit it adds to a track. The track's badly split names are joined
    back over every source's claims (`_join_split_names`), so the decision weighs the whole name (#1109). A credited
    remixer takes its artist's spelling too, so the REMIXER tag still finds it.
    """
    own = {
        (disc, num): claims.get(id(track)) or _claims_of(track, base_source)
        for disc, disc_tracks in tracks.items()
        for num, track in disc_tracks.items()
    }
    corrections = _artist_corrections() if corrected else {}
    shown = _artist_classes(tracks, own)
    spelled = {
        key: {name: corrections.get(_correction_key(as_)) or as_ for name, as_ in spelling.items()}
        for key, spelling in shown.items()
    }
    to_replace = construct_replacement_list(
        [
            (spelled[disc, num][name], role)
            for disc, disc_tracks in tracks.items()
            for num, track in disc_tracks.items()
            for name, role in track["artists"]
        ]
    )
    by_track, joined = {}, {}
    for disc, disc_tracks in tracks.items():
        for num, track in disc_tracks.items():
            spelling = spelled[disc, num]
            kept = {(spelling[name], role) for name, role in track["artists"]}
            stays = [claim for claim in own[disc, num] if (spelling[claim.name], claim.role) in kept]
            respelled = [msgspec.structs.replace(claim, name=spelling[claim.name]) for claim in stays]
            by_track[disc, num] = _join_split_names(respelled, to_replace)
            _note_joins(joined, stays, shown[disc, num])
            if "credited_remixers" in track:
                # A remixer split and joined back is credited under the whole name, so REMIXER finds it.
                whole = {
                    part.name: claim.name
                    for part, claim in zip(respelled, by_track[disc, num], strict=True)
                    if part.role == "remixer"
                }
                track["credited_remixers"] = list(
                    dict.fromkeys(
                        whole.get(written, written)
                        for written in (
                            spelling.get(name) or corrections.get(_correction_key(name), name)
                            for name in track["credited_remixers"]
                        )
                    )
                )
    decided = resolve_credits(by_track)
    for (disc, num), credits in decided.items():
        tracks[disc][num]["artists"] = [credit for credit, _ in credits]
    return [decision for credits in decided.values() for decision in credits], joined


def _artist_classes(tracks, own):
    """Which names on each track are one artist, and how that artist is spelled (#1131): each track's
    {name: spelling} for every name its credits and claims give.

    Names are one artist across the release when they share a `same_artist_key`, and on a track when Discogs credits
    one as the other there, whatever the role (its ANV: per track, as "Taylor" on one track is not every Taylor's). An
    artist takes the spelling of its first credit the merge kept on that track, as the release first spells that
    credit's key and generate_artists spells that (`artist_spellings`).
    """
    spellings = artist_spellings(tracks)
    first = {}  # same_artist_key: the first credit the merge kept under it, in track order
    for _, disc in _sorted_naturally(tracks.items()):
        for _, track in _sorted_naturally(disc.items()):
            for name, _ in track["artists"]:
                first.setdefault(same_artist_key(name), name)
    spelled = {}
    for disc_no, disc in tracks.items():
        for num, track in disc.items():
            joins = _credited_joins(track.get("artist_variations", ()))
            spelling_of = {}
            for name, _ in track["artists"]:
                spelling_of.setdefault(_joined(joins, same_artist_key(name)), first[same_artist_key(name)])
            spelling = spelled[disc_no, num] = {}
            for name in [*(name for name, _ in track["artists"]), *(claim.name for claim in own[disc_no, num])]:
                spelled_as = spelling_of.get(_joined(joins, same_artist_key(name)), name)
                spelling[name] = spellings.get(normalize_accents(spelled_as.lower()), spelled_as)
    return spelled


def _note_joins(joined, claims, spelling):
    """Add to `joined` ({spelling: {name: [sources]}}) each name `claims` give that is read as another spelling beyond
    case, accents and punctuation, "&" read as "and": names `comparable` alone joins are not shown, but for that. The
    name Discogs credits an artist as (its ANV) is why two names are one, not a name a source gave, so not shown."""
    for claim in claims:
        spelled_as = spelling[claim.name]
        if _shown(claim.name) != _shown(spelled_as):
            others = joined.setdefault(spelled_as, {})
            # One entry per spelling as shown: "WATT" and "Watt" are one.
            name = next((other for other in others if _shown(other) == _shown(claim.name)), claim.name)
            if claim.source not in (sources := others.setdefault(name, [])):
                sources.append(claim.source)


def _shown(name):
    return comparable(re.sub(r"\s*&\s*", " and ", name))


def _credited_joins(variations):
    """Each key to the one it is credited as on a track: Discogs' name to its ANV, through any chain."""
    joins = {}
    for variation in variations:
        name = _joined(joins, same_artist_key(variation["name"]))
        anv = _joined(joins, same_artist_key(variation["anv"]))
        if name != anv:
            joins[name] = anv
    return joins


def _joined(joins, key):
    """The key `key` is read as on a track, following `joins` (each key to the one it is credited as)."""
    while key in joins:
        key = joins[key]
    return key


def _join_split_names(claims, to_replace):
    """A track's claims with its badly split names joined back, as generate_artists' repair joins a track's credits
    (fix_artists_list, with `to_replace` from the release's names), over the claims of every source: in a role where
    every part of a name is claimed, each claim of a part becomes a claim of the whole name, keeping its source and
    whether it is assumed. Run before the decision, so that a part one source also claims in another role does not
    lose a role the whole name keeps (#1109).

    A claim the decision overrules (`_overruled`) joins nothing: it follows the claim that beat it, into the whole name
    when that one is joined, and loses to it again. One that agrees with a claim the decision keeps (the files' own
    guest beside their main) follows that one."""
    overruled = _overruled(claims)
    kept = dict.fromkeys((claim.name, claim.role) for at, claim in enumerate(claims) if at not in overruled)
    joined = {}
    fix_artists_list(list(kept), to_replace, joined)  # a list of its own: the repair changes the one it is given
    renamed = []
    for at, claim in enumerate(claims):
        key = (claim.name, claim.role if (claim.name, claim.role) in kept else overruled[at])
        renamed.append(msgspec.structs.replace(claim, name=joined[key]) if key in joined else claim)
    return renamed


def _overruled(claims):
    """The claims the decision overrules, by their place in `claims`, each with the performing role it loses to: a
    guess, and another source's claim that loses to the first. A source's stated claim beside its own claim of the
    role kept is not overruled: the files may credit one artist as main and as guest (#1109)."""
    won = {
        name: (role, reason.from_sources)
        for (name, role), reason in resolve_credits({None: claims})[None]
        if role in {"main", "guest"}
    }
    return {
        at: won[claim.name][0]
        for at, claim in enumerate(claims)
        if claim.role in {"main", "guest"}
        and won[claim.name][0] != claim.role
        and (claim.assumed or claim.source not in won[claim.name][1])
    }


def decide_alone(metadata, source):
    """Decide a scrape used alone as a merge of that one source, in place (no disputes to print, but the names it
    reads as one artist): each track's titles, from the names as the store credits them, as in combine_metadatas;
    then each track's credits from its claims, cleaned as the merge's are (`clean_metadata`), and the release's from
    the tracks."""
    from salmon.tagger.metadata import clean_metadata  # metadata imports this module

    _decide_titles(metadata["tracks"], source, {})
    _, joined = _decide_credits(metadata["tracks"], {}, source)
    _print_joined(joined)
    clean_metadata(metadata)
    metadata["artists"], metadata["tracks"] = generate_artists(metadata["tracks"])


def _print_joined(joined):
    """Show the names read as one artist that differ beyond case, accents and punctuation ("&" read as "and"): a wrong
    join would drop an artist from the release, so it is shown, as a dispute is."""
    if not joined:
        return
    click.secho("\nRead as one artist:", fg="yellow", bold=True)
    for spelling, others in joined.items():
        names = ", ".join(
            f"{name} ({', '.join('the files' if source == 'files' else source for source in sources)})"
            for name, sources in others.items()
        )
        click.secho(f"  {spelling}: {names}", fg="yellow")


def _print_disputes(decisions):
    """Show the credits the sources disagreed on, as the merge kept them: one line per set of names the same sources
    dispute, not one per track."""
    disputes = {}
    for (name, role), reason in decisions:
        if reason.disputed_by:
            disputes.setdefault((reason.from_sources[0], role), {}).setdefault(name, {}).update(
                dict.fromkeys(reason.disputed_by)
            )
    for (source, role), names in disputes.items():
        owner, others = ("the files'", "stores") if source == "files" else (f"{source}'s", "other stores")
        other = "guest" if role == "main" else "main"
        click.secho(f"\nKept {owner} {role} where {others} say {other}:", fg="yellow", bold=True)
        lines = {}
        for name, sources in names.items():
            lines.setdefault(tuple(sorted(sources)), []).append(name)
        for sources, line_names in lines.items():
            at = ", ".join("the files" if source == "files" else source for source in sources)
            click.secho(f"  {', '.join(line_names)} ({other} at {at})", fg="yellow")


def check_for_artist_fragments(artists, variations=(), guesses: Collection[tuple[str, str]] = ()):
    """Drop the credits that may be a fragment of another credit's name in the same role ("Ultra" of "Ultra Nate"); a
    name in one role is no fragment of a name in another (#1112). Two spellings of one artist are no fragment of each
    other, the decision joins them: one `same_artist_key` ("Herbaliser", "The Herbaliser"), or a name and the name
    Discogs credits it as on the track, through any chain (`variations`: "Roger Taylor" as "Taylor"). `guesses` are the
    credits no source states (`_guesses`): a guess never drops a credit that one states (#1112)."""
    joins = _credited_joins(variations)
    names = [(_comparable_words(a), _joined(joins, same_artist_key(a)), i, (a, i) in guesses) for a, i in artists]
    for a, i in artists.copy():
        words, key = _comparable_words(a), _joined(joins, same_artist_key(a))
        if len("".join(words)) <= 1:
            continue
        guessed = (a, i) in guesses
        for other, other_key, other_role, other_guessed in names:
            drops = other_role == i and (guessed or not other_guessed) and key != other_key
            if drops and words != other and _is_word_run(words, other) and (a, i) in artists:
                artists.remove((a, i))
    return artists


def _guesses(artists, claims, variations=()):
    """The track's credits that no source states (ADR 0007): every claim of each is assumed. A claim is of a credit in
    its role under any spelling the merge reads as one artist, as the fragment check reads them (`same_artist_key`,
    the name a source credits it as, Discogs' credited names on the track through any chain): the track keeps the
    first source's spelling, and a store stating "Beyoncé" states the base's "Beyonce"."""
    joins = _credited_joins(variations)
    keys = {
        (_joined(joins, same_artist_key(spelling)), claim.role, claim.assumed)
        for claim in claims
        for spelling in (claim.name, claim.credited_as)
    }
    return {
        (name, role)
        for name, role in artists
        if (key := _joined(joins, same_artist_key(name)), role, True) in keys and (key, role, False) not in keys
    }


def _comparable_words(name):
    """The words of a name with case, accents and punctuation dropped (`X-Men` is `xmen`)."""
    return [word for word in (comparable(w) for w in name.split()) if word]


def _is_word_run(words, other):
    """True when `words` is a contiguous run of whole words inside `other`."""
    return any(other[i : i + len(words)] == words for i in range(len(other) - len(words) + 1))
