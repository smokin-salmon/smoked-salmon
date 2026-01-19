import asyncio
import re
from difflib import SequenceMatcher as SM
from urllib import parse

import click

from salmon import cfg
from salmon.common import RE_FEAT, make_searchstrs
from salmon.errors import AbortAndDeleteFolder, RequestError


def dupe_check_recent_torrents(gazelle_site, searchstrs):
    """Checks the site log for recent uploads similar to ours.
    It may be a little slow as it has to fetch multiple pages of the log
    It also has to do a string distance comparison for each result"""
    searchstr = searchstrs[0]
    recent_uploads = gazelle_site.get_uploads_from_log()
    # Each upload in this list is best guess at (id,artist,title) from log
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
        ratio = 0
        for comparison_string in possible_comparisons:
            new_ratio = SM(None, searchstr, comparison_string).ratio()
            ratio = max(ratio, new_ratio)
        # Default tolerance is 0.5
        if ratio > cfg.upload.log_dupe_tolerance:
            hits.append(upload)
    return hits


def print_recent_upload_results(gazelle_site, recent_uploads, searchstr):
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


def _prompt_for_recent_upload_results(gazelle_site, recent_uploads, searchstr, offer_deletion):
    """
    Prints recent uploads and prompts user to choose a group ID or take other actions.
    Combines the functionality of print_recent_upload_results and _prompt_for_group_id.
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
        prompt_text = (
            "\nWould you like to upload to an existing group?\n"
            f"{'Pick from recent uploads found, p' if recent_uploads else 'P'}aste a URL"
            f" or [N]ew group / [a]bort {'/ [d]elete music folder ' if offer_deletion else ''}"
        )

        group_id = click.prompt(
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
                    group_id = asyncio.run(gazelle_site.get_redirect_torrentgroupid(torrent_id))
                    return group_id
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
                group_id = asyncio.run(gazelle_site.get_redirect_torrentgroupid(torrent_id))
                return group_id
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


def check_existing_group(gazelle_site, searchstrs, offer_deletion=True):
    """
    Make a request to the API with a dupe-check searchstr,
    then have the user validate that the torrent does not match
    anything on site.
    """
    results = get_search_results(gazelle_site, searchstrs)
    if not results and cfg.upload.requests.check_recent_uploads:
        recent_uploads = dupe_check_recent_torrents(gazelle_site, searchstrs)
        group_id = _prompt_for_recent_upload_results(
            gazelle_site, recent_uploads, " / ".join(searchstrs), offer_deletion
        )
    else:
        print_search_results(gazelle_site, results, " / ".join(searchstrs))
        group_id = _prompt_for_group_id(gazelle_site, results, offer_deletion)
    if group_id:
        confirmation = _confirm_group_id(gazelle_site, group_id, results)
        if confirmation is True:
            return group_id
        return None
    return group_id


def get_search_results(gazelle_site, searchstrs):
    results = []
    tasks = [gazelle_site.request("browse", searchstr=searchstr) for searchstr in searchstrs]
    for releases in asyncio.run(asyncio.gather(*tasks)):
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


def print_search_results(gazelle_site, results, searchstr):
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


def _prompt_for_group_id(gazelle_site, results, offer_deletion):
    """Have the user choose a group ID"""
    while True:
        group_id = click.prompt(
            click.style(
                "\nWould you like to upload to an existing group?\n"
                f"Paste a URL{', pick from groups found ' if results is not None else ''}"
                f"or [N]ew group / [a]bort {'/ [d]elete music folder ' if offer_deletion else ''}",
                fg="magenta",
            ),
            default="",
        )
        if group_id.strip().isdigit():
            group_id = int(group_id) - 1  # User doesn't type zero index
            if group_id < 1:
                group_id = 0  # If the user types 0 give them the first choice.
            if group_id < len(results):
                group_id = results[group_id]["groupId"]
                return int(group_id)
            else:
                group_id = int(group_id) + 1
                click.echo(f"Interpreting {group_id} as a group Id")
                return group_id

        elif group_id.strip().lower().startswith(gazelle_site.base_url + "/torrents.php"):
            parsed_query = parse.parse_qs(parse.urlparse(group_id).query)
            if "id" in parsed_query:
                group_id = parsed_query["id"][0]
            elif "torrentid" in parsed_query:
                group_id = parsed_query["torrentid"][0]
                group_id = asyncio.run(gazelle_site.get_redirect_torrentgroupid(group_id))
                return group_id
            else:
                click.echo("Could not find group ID in URL.")
                continue
            return int(group_id)
        elif group_id.lower().startswith("a"):
            raise click.Abort
        elif group_id.lower().startswith("d") and offer_deletion:
            raise AbortAndDeleteFolder
        elif group_id.lower().startswith("n") or not group_id.strip():
            click.echo("Uploading to a new torrent group.")
            return None


def print_torrents(gazelle_site, group_id, rset=None, highlight_torrent_id=None):
    """Print the torrents that are a part of the torrent group."""
    # If rset is not provided, fetch it from the API
    if rset is None:
        try:
            rset = asyncio.run(gazelle_site.torrentgroup(group_id))
            # account for differences between search result and group result json
            rset["groupName"] = rset["group"]["name"]
            rset["artist"] = ""
            for a in rset["group"]["musicInfo"]["artists"]:
                rset["artist"] += a["name"] + " "
            rset["groupId"] = rset["group"]["id"]
            rset["groupYear"] = rset["group"]["year"]
        except RequestError:
            click.secho(f"{group_id} does not exist.", fg="red")
            raise click.Abort from None

    group_info = {}
    click.secho(f"\nSelected ID: {rset['groupId']} ", nl=False)
    click.secho(f"| {rset['artist']} - {rset['groupName']} ", fg="cyan", nl=False)
    click.secho(f"({rset['groupYear']})", fg="yellow")
    click.secho("Torrents in this group:", fg="yellow", bold=True)
    for t in rset["torrents"]:
        color = "yellow" if highlight_torrent_id and t["id"] == highlight_torrent_id else None
        if t["remastered"]:
            click.secho(
                f"> {t['remasterYear']} / {t['remasterCatalogueNumber']} / "
                f"{t['media']} / {t['format']} / {t['encoding']}",
                fg=color,
            )
        if not t["remastered"]:
            if not group_info:
                group_info = asyncio.run(gazelle_site.torrentgroup(group_id))["group"]
            click.secho(
                f"> OR / {group_info['recordLabel']} / "
                f"{group_info['catalogueNumber']} / {t['media']} / "
                f"{t['format']} / {t['encoding']}",
                fg=color,
            )


def _confirm_group_id(gazelle_site, group_id, results):
    """Have the user decide whether or not to upload to a torrent group."""
    rset = None
    for r in results:
        if group_id == r["groupId"]:
            rset = r
            break

    print_torrents(gazelle_site, group_id, rset)
    while True:
        resp = click.prompt(
            click.style(
                "\nAre you sure you would you like to upload this torrent to this group? [Y]es, "
                "[n]ew group, [a]bort, [d]elete music folder",
                fg="magenta",
            ),
            default="Y",
        )[0].lower()
        if resp == "a":
            raise click.Abort
        elif resp == "d":
            raise AbortAndDeleteFolder
        elif resp == "y":
            return True
        elif resp == "n":
            return False
