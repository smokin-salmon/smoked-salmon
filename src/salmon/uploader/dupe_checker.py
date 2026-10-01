import asyncio
import html
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any
from urllib import parse

import anyio
import asyncclick as click

from salmon import cfg
from salmon.common import RE_FEAT, make_searchstrs, normalize_accents, re_strip
from salmon.common.strings import artist_keys, comparable
from salmon.errors import AbortAndDeleteFolder, RequestError, RequestFailedError
from salmon.trackers.base import hold_request_messages
from salmon.uploader.upload import generate_catno

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


LOG_DUPE_WORD_OVERLAP_THRESHOLD = 0.5


def can_check_site_log(gazelle_site: "BaseGazelleApi") -> bool:
    """Whether the site log can be read for recent uploads, saying why not when it cannot.

    log.php is a site page, not an API endpoint, so an API key does not open it. Without a
    session cookie every request to it is bounced to login.php (#432).

    Args:
        gazelle_site: The tracker API instance.

    Returns:
        True if a session cookie is configured.
    """
    if gazelle_site.has_session_cookie:
        return True
    click.secho(
        f"Skipping the {gazelle_site.site_string} log check for recent uploads: it needs a session cookie "
        f"(tracker.{gazelle_site.site_code.lower()}.session), and none is set.",
        fg="yellow",
    )
    return False


async def dupe_check_recent_torrents(
    gazelle_site: "BaseGazelleApi", searchstrs: list[str], our_title: str | None = None
) -> list[tuple]:
    """Check site log for recent uploads similar to ours.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings to match against.
        our_title: Our release's title, used to require actual title overlap on top of the
            search string comparison (see _recent_upload_matches). None skips that extra check,
            for callers that cannot supply a title.

    Returns:
        List of matching upload tuples (id, artist, title).
    """
    recent_uploads = await gazelle_site.get_uploads_from_log()
    # Each upload in this list is best guess at (id,artist,title) from log
    our_title_words = _title_words(our_title) if our_title is not None else None
    hits = []
    seen = []
    for upload in recent_uploads:
        # We don't care about different torrents from the same release.
        torrent_str = upload[1] + upload[2]
        if torrent_str in seen:
            continue
        seen.append(torrent_str)
        artist = upload[1]
        title = upload[2]
        artist = [[artist, "main"]]
        possible_comparisons = generate_dupe_check_searchstrs(artist, title)
        if _recent_upload_matches(
            searchstrs, possible_comparisons, cfg.upload.log_dupe_tolerance, our_title_words, _title_words(title)
        ):
            hits.append(upload)
    return hits


def _title_words(title: str | None) -> set[str]:
    """Normalized words of a title, cleaned up the same way make_searchstrs cleans the album.

    Two releases can share every artist word and still be unrelated if their titles have nothing
    in common: comparing the full search strings alone lets a shared multi-word artist outweigh a
    completely different one-word title, since SequenceMatcher and the word-overlap ratio both
    look at artist and title text together. Comparing title words on their own closes that gap.

    Args:
        title: A release title, our own or a logged upload's.

    Returns:
        The set of normalized words in the title, empty if there is no title.
    """
    album = _sanitize_album_for_dupe_check(title)
    album = re.sub(r" ?(- )? (EP|Single)", "", album)
    album = re.sub(r"\(?[Ff]eat(\.|uring)? [^\)]+\)?", "", album)
    normalized = re_strip(album, filter_nonscrape=False)
    accented = normalize_accents(normalized)
    return set(accented.split()) if isinstance(accented, str) else set()


def _recent_upload_matches(
    searchstrs: list[str],
    possible_comparisons: list[str],
    tolerance: float,
    our_title_words: set[str] | None = None,
    candidate_title_words: set[str] | None = None,
) -> bool:
    """Return True when a logged upload is genuinely similar enough to be a likely dupe.

    Comparing against only the first search string, and on SequenceMatcher's ratio alone,
    overweights a shared artist prefix between two otherwise unrelated releases (#509). Every
    generated search string is checked, and a high ratio is also required to share enough words
    with the candidate to suggest actual title overlap, not just a shared artist.

    A shared artist can still carry a high ratio and word-overlap fraction on its own when the
    artist has more words than the title (#518): "A B C song" vs "A B C other" shares 3 of 4
    words. Requiring the titles themselves to share at least one word, checked separately from the
    artist, closes that gap while still matching same-title collab releases.

    Args:
        searchstrs: Search strings generated for our release.
        possible_comparisons: Search strings generated for the logged upload.
        tolerance: The configured similarity tolerance (cfg.upload.log_dupe_tolerance).
        our_title_words: Normalized words of our release's title, or None to skip the title check.
        candidate_title_words: Normalized words of the logged upload's title.

    Returns:
        True if any pair of strings is similar enough, shares enough words, and (when title words
        are given) shares at least one title word.
    """
    if our_title_words and candidate_title_words and not (our_title_words & candidate_title_words):
        return False
    for searchstr in searchstrs:
        for comparison_string in possible_comparisons:
            ratio = SequenceMatcher(None, searchstr, comparison_string).ratio()
            if ratio <= tolerance:
                continue
            if _word_overlap_ratio(searchstr, comparison_string) < LOG_DUPE_WORD_OVERLAP_THRESHOLD:
                continue
            return True
    return False


def _word_overlap_ratio(left: str, right: str) -> float:
    """Fraction of words shared between two normalized search strings, relative to the larger side.

    Args:
        left: A normalized search string.
        right: Another normalized search string.

    Returns:
        The overlap ratio, 0.0 if either side has no words.
    """
    left_words = set(left.split())
    right_words = set(right.split())
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / max(len(left_words), len(right_words))


def print_recent_upload_results(gazelle_site: "BaseGazelleApi", recent_uploads: list[tuple], searchstr: str) -> None:
    """Prints any recent uploads.
    Currently hard limited to 5.
    Realistically we are probably only interested in 1.
    These results can't be used for group selection because the log doesn't give us a group id"""
    if recent_uploads:
        click.secho(
            f"\nFound similar recent uploads in the {gazelle_site.site_string} log: ",
            fg="red",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for u in recent_uploads[:5]:
            click.secho(
                f"{u[1]} - {u[2]} | {gazelle_site.base_url}/torrents.php?torrentid={u[0]}",
                fg="cyan",
            )


async def _prompt_for_recent_upload_results(
    gazelle_site: "BaseGazelleApi",
    recent_uploads: list[tuple],
    searchstr: str,
    offer_deletion: bool,
) -> int | None:
    """Print recent uploads and prompt user to choose a group ID.

    Args:
        gazelle_site: The tracker API instance.
        recent_uploads: List of recent upload tuples.
        searchstr: Search string used.
        offer_deletion: Whether to offer folder deletion option.

    Returns:
        Group ID or None for new group.
    """
    # First, print the recent uploads if any
    if recent_uploads:
        click.secho(
            f"\nFound similar recent uploads in the {gazelle_site.site_string} log: ",
            fg="red",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for u_index, u in enumerate(recent_uploads[:5]):
            click.echo(f" {u_index + 1:02d} >> ", nl=False)  # torrent_id
            click.secho(f"{u[1]} - {u[2]} ", fg="cyan", nl=False)  # artist - title
            click.echo(f"| {gazelle_site.base_url}/torrents.php?torrentid={u[0]}")

    # Now prompt for user action
    while True:
        prompt_header = (
            "\nThese are similar recent uploads from the site log, not exact group matches.\n"
            "Pick one only if it is actually the same group.\n"
            if recent_uploads
            else "\nWould you like to upload to an existing group?\n"
        )
        prompt_text = (
            prompt_header + f"{'Pick from recent uploads found, p' if recent_uploads else 'P'}aste a URL"
            f" or [N]ew group / [a]bort {'/ [d]elete music folder ' if offer_deletion else ''}"
        )

        group_id = await click.prompt(
            click.style(prompt_text, fg="magenta"),
            default="",
        )

        # Handle numeric input (selecting from recent uploads or direct group ID)
        if group_id.strip().isdigit():
            group_id_num = int(group_id)

            if group_id_num == 0:
                group_id_num = 1  # If the user types 0 give them the first choice.

            # If user picks from recent uploads list
            if recent_uploads and 1 <= group_id_num <= len(recent_uploads):
                torrent_id = recent_uploads[group_id_num - 1][0]
                # Need to convert torrent ID to group ID
                try:
                    result_group_id = await gazelle_site.get_redirect_torrentgroupid(torrent_id)
                    if result_group_id is not None:
                        return result_group_id
                    click.echo("Could not get group ID from torrent ID.")
                    continue
                except Exception:
                    click.echo("Could not get group ID from torrent ID.")
                    continue
            else:
                # Direct group ID input
                click.echo(f"Interpreting {group_id_num} as a group ID")
                return group_id_num

        # Handle URL input
        elif group_id.strip().lower().startswith(gazelle_site.base_url + "/torrents.php"):
            parsed_query = parse.parse_qs(parse.urlparse(group_id).query)
            if "id" in parsed_query:
                group_id = parsed_query["id"][0]
                return int(group_id)
            elif "torrentid" in parsed_query:
                torrent_id = parsed_query["torrentid"][0]
                result_group_id = await gazelle_site.get_redirect_torrentgroupid(torrent_id)
                if result_group_id is not None:
                    return result_group_id
                click.echo("Could not get group ID from torrent ID.")
                continue
            else:
                click.echo("Could not find group ID in URL.")
                continue

        # Handle action commands
        elif group_id.lower().startswith("a"):
            raise click.Abort
        elif group_id.lower().startswith("d") and offer_deletion:
            raise AbortAndDeleteFolder
        elif group_id.lower().startswith("n") or not group_id.strip():
            click.echo("Uploading to a new torrent group.")
            return None


async def fetch_existing_group_candidates(
    gazelle_site: "BaseGazelleApi",
    searchstrs: list[str],
    our_title: str | None = None,
) -> tuple[list[dict], list[tuple] | None]:
    """Search the tracker for an existing group, without printing or prompting anything.

    This is the part of check_existing_group that talks to the tracker, so it can run in the
    background: see fetch_existing_group_candidates_in_background.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking.
        our_title: Our release's title, passed through to dupe_check_recent_torrents.

    Returns:
        Tuple of (search results, recent uploads from the site log, or None if it was not read).
    """
    results = await get_search_results(gazelle_site, searchstrs)
    recent_uploads = None
    # The test resolve_existing_group makes, with has_session_cookie for can_check_site_log: the notice
    # that one prints when the log cannot be read is for resolve_existing_group to show.
    if not results and cfg.upload.requests.check_recent_uploads and gazelle_site.has_session_cookie:
        recent_uploads = await dupe_check_recent_torrents(gazelle_site, searchstrs, our_title)
    return results, recent_uploads


async def resolve_existing_group(
    gazelle_site: "BaseGazelleApi",
    searchstrs: list[str],
    results: list[dict],
    recent_uploads: list[tuple] | None,
    offer_deletion: bool = True,
    release: dict[str, Any] | None = None,
) -> int | None:
    """Show the candidates fetch_existing_group_candidates found, and prompt the user for a group.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking.
        results: Search results from fetch_existing_group_candidates.
        recent_uploads: Recent uploads from fetch_existing_group_candidates, or None.
        offer_deletion: Whether to offer folder deletion option.
        release: Our release (artists, title, year, source, format, encoding, edition), for the answers
            the prompts offer: the one result it matches, and abort when the group already holds it.

    Returns:
        Group ID or None for new group.
    """
    if not results and cfg.upload.requests.check_recent_uploads and can_check_site_log(gazelle_site):
        group_id = await _prompt_for_recent_upload_results(
            gazelle_site, recent_uploads or [], " / ".join(searchstrs), offer_deletion
        )
    else:
        print_search_results(gazelle_site, results, " / ".join(searchstrs))
        group_id = await _prompt_for_group_id(
            gazelle_site, results, offer_deletion, default=suggest_group(results, release)
        )
    if group_id:
        confirmation = await _confirm_group_id(gazelle_site, group_id, results, offer_deletion, release)
        if confirmation is True:
            return group_id
        return None
    return group_id


async def check_existing_group(
    gazelle_site: "BaseGazelleApi",
    searchstrs: list[str],
    offer_deletion: bool = True,
    our_title: str | None = None,
    release: dict[str, Any] | None = None,
) -> int | None:
    """Check for existing group and prompt user for selection.

    fetch_existing_group_candidates, then resolve_existing_group.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking.
        offer_deletion: Whether to offer folder deletion option.
        our_title: Our release's title, passed through to fetch_existing_group_candidates.
        release: Our release, passed through to resolve_existing_group.

    Returns:
        Group ID or None for new group.
    """
    results, recent_uploads = await fetch_existing_group_candidates(gazelle_site, searchstrs, our_title)
    return await resolve_existing_group(gazelle_site, searchstrs, results, recent_uploads, offer_deletion, release)


class GroupCandidatesFetch:
    """fetch_existing_group_candidates, running in the background: see fetch_existing_group_candidates_in_background."""

    def __init__(self, gazelle_site: "BaseGazelleApi", searchstrs: list[str], our_title: str | None = None) -> None:
        self._gazelle_site = gazelle_site
        self._searchstrs = searchstrs
        self._our_title = our_title
        self._done = anyio.Event()
        self._candidates: tuple[list[dict], list[tuple] | None] | None = None
        self._error: Exception | None = None
        self._messages: list[tuple[str, dict[str, Any]]] = []

    async def _run(self) -> None:
        with hold_request_messages() as messages:
            self._messages = messages
            try:
                self._candidates = await fetch_existing_group_candidates(
                    self._gazelle_site, self._searchstrs, self._our_title
                )
            except Exception as e:
                # Kept for result() to raise: raised here, it would cancel whatever the user is doing.
                self._error = e
        self._done.set()

    async def result(self) -> tuple[list[dict], list[tuple] | None]:
        """Wait for the fetch, print what it held back, then return what it found or raise its error.

        Returns:
            As fetch_existing_group_candidates.
        """
        await self._done.wait()
        for message, styles in self._messages:
            click.secho(message, **styles)
        self._messages = []
        if self._error is not None:
            raise self._error
        assert self._candidates is not None
        return self._candidates


@asynccontextmanager
async def fetch_existing_group_candidates_in_background(
    gazelle_site: "BaseGazelleApi",
    searchstrs: list[str],
    our_title: str | None = None,
) -> AsyncIterator[GroupCandidatesFetch | None]:
    """Run fetch_existing_group_candidates in the background while the block runs.

    The fetch only reads from the tracker, through the site's own client, so it shares the rate
    limiter and connections of every other request. What its requests print is held back until the
    block calls result(), so none of it lands in the middle of a prompt the block is showing.

    Leaving the block, whichever way, cancels the fetch if it is still running: it never outlives
    the block, and an error it had is dropped with it if result() was never called.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking. If empty, nothing is fetched.
        our_title: Our release's title, passed through to fetch_existing_group_candidates.

    Yields:
        The running fetch, or None if there are no search strings.
    """
    if not searchstrs:
        yield None
        return
    fetch = GroupCandidatesFetch(gazelle_site, searchstrs, our_title)
    raised: BaseException | None = None
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(fetch._run)
            try:
                yield fetch
            finally:
                tg.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        # The fetch keeps its own error for result(), so the group only holds what the block raised.
        # Raise that as it is, as the block would have without a task group around it.
        if len(group.exceptions) != 1:
            raise
        raised = group.exceptions[0]
    if raised is not None:
        raise raised


async def get_search_results(gazelle_site: "BaseGazelleApi", searchstrs: list[str]) -> list[dict]:
    """Search for existing releases on tracker.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings to query.

    Returns:
        List of matching release dicts.
    """
    results: list[dict] = []
    tasks = [gazelle_site.api_call("browse", {"searchstr": searchstr}) for searchstr in searchstrs]
    for releases in await asyncio.gather(*tasks):
        for release in releases["results"]:
            if release not in results:
                results.append(release)
    return results


def generate_dupe_check_searchstrs(artists, album, catno=None):
    searchstrs = []
    album = _sanitize_album_for_dupe_check(album)
    searchstrs += make_searchstrs(artists, album, normalize=True)
    if album is not None and re.search(r"vol[^u]", album.lower()):
        extra_alb_search = re.sub(r"vol[^ ]+", "volume", album, flags=re.IGNORECASE)
        searchstrs += make_searchstrs(artists, extra_alb_search, normalize=True)
    if album is not None and "untitled" in album.lower():  # Filthy catno untitled rlses
        searchstrs += make_searchstrs(artists, catno or "", normalize=True)
    if album is not None and "/" in album:  # Filthy singles
        searchstrs += make_searchstrs(artists, album.split("/")[0], normalize=True)
    elif catno and album is not None and catno.lower() in album.lower():
        searchstrs += make_searchstrs(artists, "untitled", normalize=True)
    return filter_unnecessary_searchstrs(searchstrs)


def _sanitize_album_for_dupe_check(album):
    if not album:  # Handle None or empty string
        return ""
    album = RE_FEAT.sub("", album)
    album = re.sub(
        r"[\(\[][^\)\]]*(Edition|Version|Deluxe|Original|Reissue|Remaster|Vol|Mix|Edit)"
        r"[^\)\]]*[\)\]]",
        "",
        album,
        flags=re.IGNORECASE,
    )
    album = re.sub(r"[\(\[][^\)\]]*Remixes[^\)\]]*[\)\]]", "remixes", album, flags=re.IGNORECASE)
    album = re.sub(r"[\(\[][^\)\]]*Remix[^\)\]]*[\)\]]", "remix", album, flags=re.IGNORECASE)
    return album


def filter_unnecessary_searchstrs(searchstrs):
    past_strs = []
    new_strs = []
    for stri in sorted(searchstrs, key=len):
        word_set = set(stri.split())
        for prev_word_set in past_strs:
            if all(p in word_set for p in prev_word_set):
                break
        else:
            new_strs.append(stri)
            past_strs.append(word_set)
    return new_strs


def print_search_results(gazelle_site: "BaseGazelleApi", results: list[dict], searchstr: str) -> None:
    """Print all the site search results."""
    if not results:
        click.secho(
            f"\nNo groups found on {gazelle_site.site_string} matching this release.",
            fg="green",
            nl=False,
        )
    else:
        click.secho(
            f"\nResults matching this release were found on {gazelle_site.site_string}: ",
            fg="red",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for r_index, r in enumerate(results):
            try:
                url = f"{gazelle_site.base_url}/torrents.php?id={r['groupId']}"
                # User doesn't get to pick a zero index
                click.echo(f" {r_index + 1:02d} >> {r['groupId']} | ", nl=False)
                click.secho(f"{r['artist']} - {r['groupName']} ", fg="cyan", nl=False)
                click.secho(f"({r['groupYear']}) [{r['releaseType']}] ", fg="yellow", nl=False)
                click.echo(f"[Tags: {', '.join(r['tags'])}] | {url}")
            except (KeyError, TypeError):
                continue


def suggest_group(results: list[dict], release: dict[str, Any] | None) -> str:
    """The group prompt's default: the number of the one search result with our artist, title and year.

    No result, or several, leaves the default empty (a new group). Names are compared loosely (case,
    accents and punctuation aside); the year must be the same on both sides, and present.

    Args:
        results: The tracker's search results, in the order printed.
        release: Our release, with its artists, title and year.

    Returns:
        The result's number as printed (1-based), or "" for none.
    """
    if not results or not release:
        return ""
    title = comparable(release.get("title"))
    artists = artist_keys(release.get("artists"))
    year = str(release.get("year") or "").strip()
    if not title or not artists or not year:
        return ""
    matches = [
        str(number)
        for number, result in enumerate(results, 1)
        if result.get("groupId") is not None
        and comparable(html.unescape(str(result.get("groupName") or ""))) == title
        and comparable(html.unescape(str(result.get("artist") or ""))) in artists
        and str(result.get("groupYear") or "").strip() == year
    ]
    return matches[0] if len(matches) == 1 else ""


async def _prompt_for_group_id(
    gazelle_site: "BaseGazelleApi",
    results: list[dict],
    offer_deletion: bool,
    default: str = "",
) -> int | None:
    """Prompt user to choose a group ID.

    Args:
        gazelle_site: The tracker API instance.
        results: Search results to choose from.
        offer_deletion: Whether to offer folder deletion option.
        default: The answer an empty reply gives: a result's number, or "" for a new group.

    Returns:
        Group ID or None for new group.
    """
    while True:
        group_id = await click.prompt(
            click.style(
                "\nWould you like to upload to an existing group?\n"
                f"Paste a URL{', pick from groups found ' if results is not None else ''}"
                f"or [N]ew group / [a]bort {'/ [d]elete music folder ' if offer_deletion else ''}",
                fg="magenta",
            ),
            default=default,
        )
        if group_id.strip().isdigit():
            raw_input = int(group_id)
            list_index = max(0, raw_input - 1)  # 1-based → 0-based, clamp to 0
            if list_index < len(results):
                return int(results[list_index]["groupId"])
            else:
                click.echo(f"Interpreting {raw_input} as a group Id")
                return raw_input

        elif group_id.strip().lower().startswith(gazelle_site.base_url + "/torrents.php"):
            parsed_query = parse.parse_qs(parse.urlparse(group_id).query)
            if "id" in parsed_query:
                return int(parsed_query["id"][0])
            elif "torrentid" in parsed_query:
                torrent_id = parsed_query["torrentid"][0]
                result_group_id = await gazelle_site.get_redirect_torrentgroupid(torrent_id)
                if result_group_id is not None:
                    return result_group_id
                continue
            else:
                click.echo("Could not find group ID in URL.")
                continue
        elif group_id.lower().startswith("a"):
            raise click.Abort
        elif group_id.lower().startswith("d") and offer_deletion:
            raise AbortAndDeleteFolder
        elif group_id.lower().startswith("n") or not group_id.strip():
            click.echo("Uploading to a new torrent group.")
            return None


async def print_torrents(
    gazelle_site: "BaseGazelleApi",
    group_id: int,
    rset: dict | None = None,
    highlight_torrent_id: int | None = None,
) -> dict:
    """Print torrents in a torrent group.

    Args:
        gazelle_site: The tracker API instance.
        group_id: The group ID.
        rset: Optional pre-fetched group data.
        highlight_torrent_id: Torrent ID to highlight.

    Returns:
        The group data printed: rset, or what was fetched.
    """
    # If rset is not provided, fetch it from the API
    if rset is None:
        try:
            fetched_rset = await gazelle_site.torrentgroup(group_id)
            # account for differences between search result and group result json
            fetched_rset["groupName"] = fetched_rset["group"]["name"]
            fetched_rset["artist"] = ""
            for a in fetched_rset["group"]["musicInfo"]["artists"]:
                fetched_rset["artist"] += a["name"] + " "
            fetched_rset["groupId"] = fetched_rset["group"]["id"]
            fetched_rset["groupYear"] = fetched_rset["group"]["year"]
            rset = fetched_rset
        except RequestFailedError as err:
            click.secho(f"{group_id} does not exist on {gazelle_site.site_string} ({err}).", fg="red")
            raise click.Abort from None
        except RequestError as err:
            click.secho(f"Could not fetch group {group_id} from {gazelle_site.site_string}: {err}", fg="red")
            raise click.Abort from None

    # At this point rset is guaranteed to be non-None
    assert rset is not None

    click.secho(f"\nSelected ID: {rset['groupId']} ", nl=False)
    click.secho(f"| {rset['artist']} - {rset['groupName']} ", fg="cyan", nl=False)
    click.secho(f"({rset['groupYear']})", fg="yellow")
    click.secho("Torrents in this group:", fg="yellow", bold=True)
    # Pull group-level info once (optional fallback only)
    group_info = rset.get("group", {}) or {}

    for t in rset["torrents"]:
        color = "yellow" if highlight_torrent_id and t.get("id") == highlight_torrent_id else None
        click.secho(f"> {describe_torrent(t, group_info)}", fg=color)
    return rset


def describe_torrent(t: dict, group_info: dict) -> str:
    """Describe a torrent of a group in one line: edition, media, format and encoding.

    Args:
        t: The torrent, from a search result or a torrentgroup response.
        group_info: The group's own info, used for an original release's label and catalogue number.

    Returns:
        The description, e.g. "2020 / Label / CAT1 / WEB / FLAC / Lossless".
    """
    is_remaster = _is_remaster(t)

    group_label = (group_info.get("recordLabel") or "").strip()
    group_catno = (group_info.get("catalogueNumber") or "").strip()
    label = ((t.get("remasterRecordLabel") or "").strip() if is_remaster else "") or group_label
    catno = ((t.get("remasterCatalogueNumber") or "").strip() if is_remaster else "") or group_catno

    prefix_parts = []
    if is_remaster:
        if t.get("remasterYear"):
            prefix_parts.append(str(t["remasterYear"]))
        title = (t.get("remasterTitle") or "").strip()
        if title:
            prefix_parts.append(title)
    else:
        prefix_parts.append("OR")

    if label:
        prefix_parts.append(label)
    if catno:
        prefix_parts.append(catno)

    prefix = " / ".join(prefix_parts)
    if prefix:
        prefix += " / "

    return f"{prefix}{t['media']} / {t['format']} / {t['encoding']}"


def _is_remaster(torrent: dict) -> bool:
    """Robust across RED/OPS: `remastered` is not always sent, so any edition field counts."""
    return bool(torrent.get("remastered")) or any(
        (
            torrent.get("remasterYear"),
            (torrent.get("remasterTitle") or "").strip(),
            (torrent.get("remasterRecordLabel") or "").strip(),
            (torrent.get("remasterCatalogueNumber") or "").strip(),
        )
    )


def _edition_catno(torrent: dict, group: dict) -> str:
    """Catalogue number of the torrent's edition; only an original release falls back to the group's."""
    if _is_remaster(torrent):
        return (torrent.get("remasterCatalogueNumber") or "").strip()
    return ((group.get("group") or {}).get("catalogueNumber") or "").strip()


def matching_torrents(group: dict, release: dict) -> list[dict]:
    """Find the group's torrents in the release's edition with its media, format and encoding.

    The edition is the year, catalogue number and edition title. Any of them missing on either side
    still matches.

    Args:
        group: The group, as the tracker's torrentgroup API returns it.
        release: The release metadata, with its source, format and encoding.

    Returns:
        The matching torrents, in the group's order.
    """
    wanted = (release.get("source"), release.get("format"), release.get("encoding"))
    if not all(wanted):
        return []
    year = str(release.get("year") or "")
    catno = comparable(generate_catno(release))
    edition_title = comparable(release.get("edition_title"))
    group_year = (group.get("group") or {}).get("year")
    matches = []
    for torrent in group.get("torrents") or []:
        if (torrent.get("media"), torrent.get("format"), torrent.get("encoding")) != wanted:
            continue
        # Only an original release takes the group's year; a remaster with no year of its own matches any.
        edition_year = str((torrent.get("remasterYear") if _is_remaster(torrent) else group_year) or "")
        if year and edition_year and edition_year != year:
            continue
        held_catno = comparable(_edition_catno(torrent, group))
        if catno and held_catno and held_catno != catno:
            continue
        held_title = comparable(torrent.get("remasterTitle"))
        if edition_title and held_title and held_title != edition_title:
            continue
        matches.append(torrent)
    return matches


async def choose_source_flac(group: dict, release: dict) -> dict | None:
    """Choose the FLAC in the release's edition of an existing group that the transcodes are made from.

    With several matching FLACs the user picks one; --yes-all stops instead of guessing.

    Args:
        group: The group, as the tracker's torrentgroup API returns it.
        release: The reviewed release metadata.

    Returns:
        The chosen torrent, or None to stop.
    """
    group_id = (group.get("group") or {}).get("id")
    flacs = matching_torrents(group, release)
    wanted = f"{release.get('source')} FLAC {release.get('encoding')}"
    if not flacs:
        click.secho(
            f"\nGroup {group_id} has no {wanted} in this release's edition (year, catalogue number, edition title) "
            "to transcode from.",
            fg="red",
            bold=True,
        )
        return None
    if len(flacs) == 1:
        return flacs[0]

    click.secho(f"\nGroup {group_id} has several {wanted} torrents in this edition:", fg="yellow", bold=True)
    group_info = group.get("group") or {}
    for i, t in enumerate(flacs, 1):
        click.echo(f"{i:02d} >> {describe_torrent(t, group_info)}")
    if cfg.upload.yes_all:
        click.secho(
            "Not picking the FLAC the transcodes are made from with --yes-all. Run without it to choose.",
            fg="red",
            bold=True,
        )
        return None
    while True:
        choice = await click.prompt(
            click.style(f"\nWhich one are these transcodes made from? [1-{len(flacs)}] or [a]bort", fg="magenta"),
            default="",
        )
        choice = choice.strip().lower()
        if choice.startswith("a"):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(flacs):
            return flacs[int(choice) - 1]
        click.secho(f"Enter a number from 1 to {len(flacs)}, or a to abort.", fg="red")


def held_formats(group: dict, release: dict, source_flac: dict, formats: dict[str, tuple[str, str]]) -> set[str]:
    """Find the formats that the release's edition in the group already holds, the source FLAC aside.

    Args:
        group: The group, as the tracker's torrentgroup API returns it.
        release: The reviewed release metadata.
        source_flac: The FLAC the transcodes are made from, which never counts as held.
        formats: The format and encoding of each candidate, by name.

    Returns:
        The names of the candidates the edition already has.
    """
    held = set()
    for name, (fmt, encoding) in formats.items():
        in_edition = matching_torrents(group, {**release, "format": fmt, "encoding": encoding})
        if any(t.get("id") != source_flac.get("id") for t in in_edition):
            held.add(name)
    return held


def _held_in_group(rset: dict, release: dict[str, Any] | None) -> list[dict]:
    """The torrents of a printed group that already hold our release's edition, media, format and encoding.

    rset is a search result or a fetched group: a search result has the group's year as groupYear.
    """
    if not release:
        return []
    group = rset if "group" in rset else {**rset, "group": {"year": rset.get("groupYear")}}
    return matching_torrents(group, release)


async def _confirm_group_id(
    gazelle_site: "BaseGazelleApi",
    group_id: int,
    results: list[dict],
    offer_deletion: bool = True,
    release: dict[str, Any] | None = None,
) -> bool:
    """Confirm upload to a torrent group.

    When the group's edition already holds our media, format and encoding, that torrent is named and
    the default answer is abort. It is not refused: a trump is a legitimate upload.

    Args:
        gazelle_site: The tracker API instance.
        group_id: The group ID.
        results: Search results.
        offer_deletion: Whether to offer folder deletion option.
        release: Our release, with its source, format, encoding and edition.

    Returns:
        True if confirmed, False otherwise.
    """
    rset = None
    for r in results:
        if group_id == r["groupId"]:
            rset = r
            break

    # The match reads the group just printed: it sends no request of its own.
    rset = await print_torrents(gazelle_site, group_id, rset)
    held = _held_in_group(rset, release)
    for torrent in held:
        click.secho(
            f"\nDUPE RISK: this edition already has {describe_torrent(torrent, rset.get('group') or {})}; "
            "the site removes exact duplicates unless this upload trumps it.",
            fg="red",
            bold=True,
        )
    while True:
        resp = (
            await click.prompt(
                click.style(
                    "\nAre you sure you would you like to upload this torrent to this group? [Y]es, "
                    f"[n]ew group, [a]bort{', [d]elete music folder' if offer_deletion else ''}",
                    fg="magenta",
                ),
                default="a" if held else "Y",
            )
        )[0].lower()
        if resp == "a":
            raise click.Abort
        elif resp == "d" and offer_deletion:
            raise AbortAndDeleteFolder
        elif resp == "y":
            return True
        elif resp == "n":
            return False
