import asyncio
from urllib import parse

import click
import rich

from salmon import cfg
from salmon.common import format_size
from salmon.errors import RequestError

loop = asyncio.get_event_loop()


def check_requests(gazelle_site, searchstrs):
    """
    Search for requests on site and offer a choice to fill one.
    """
    results = get_request_results(gazelle_site, searchstrs)
    print_request_results(gazelle_site, results, " / ".join(searchstrs))
    # Should add an option to still prompt if there are no results.
    if results or cfg.upload.requests.always_ask_for_request_fill:
        request_id = _prompt_for_request_id(gazelle_site, results)
        if request_id:
            confirmation = _confirm_request_id(gazelle_site, request_id)
            if confirmation is True:
                return request_id
    return None


def get_request_results(gazelle_site, searchstrs):
    "Get the request results from gazelle site"
    results = []
    for searchstr in searchstrs:
        for req in loop.run_until_complete(
            gazelle_site.request("requests", search=searchstr)  # ,order='bounty')
        )["results"]:
            if req not in results:
                results.append(req)
    return results


def print_request_results(gazelle_site, results, searchstr):
    """Print all the request search results.
    Could use a table in the future."""
    if not results:
        click.secho(
            f"\nNo requests were found on {gazelle_site.site_string}",
            fg="green",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
    else:
        click.secho(
            f"\nRequests were found on {gazelle_site.site_string}: ",
            fg="green",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for r_index, r in enumerate(results):
            try:
                url = gazelle_site.request_url(r["requestId"])
                # User doesn't get to pick a zero index
                click.echo(f" {r_index + 1:02d} >> {url} | ", nl=False)
                if len(r["artists"][0]) > 3:
                    r["artist"] = "Various Artists"
                else:
                    r["artist"] = ""
                    for a in r["artists"][0]:
                        r["artist"] += a["name"] + " "
                click.secho(f"{r['artist']}", fg="cyan", nl=False)
                click.secho(f" - {r['title']} ", fg="cyan", nl=False)
                click.secho(f"({r['year']}) [{r['releaseType']}] ", fg="yellow")
                click.secho(f"Requirements: {' or '.join(r['bitrateList'])} / ", nl=False)
                click.secho(f"{' or '.join(r['formatList'])} / ", nl=False)
                click.secho(f"{' or '.join(r['mediaList'])} / ")

            except (KeyError, TypeError):
                continue


def _print_request_details(gazelle_site, req):
    """Print request details."""
    click.secho("\nSelected Request:")
    click.secho(gazelle_site.request_url(req["requestId"]))
    click.secho(f" {req['artist']}", fg="cyan", nl=False)
    click.secho(f" - {req['title']} ", fg="cyan", nl=False)
    click.secho(f"({req['year']})", fg="yellow")
    click.secho(f" - {req['requestorName']} ", fg="cyan", nl=False)

    if "totalBounty" in req:
        bounty = req["totalBounty"]
    elif "bounty" in req:
        bounty = req["bounty"]

    bounty = format_size(bounty)
    click.secho(bounty, fg="cyan")

    click.secho(f"Allowed Bitrate: {' | '.join(req['bitrateList'])}")
    click.secho(f"Allowed Formats: {' | '.join(req['formatList'])}")
    if "CD" in req["mediaList"]:
        req["mediaList"].remove("CD")
        req["mediaList"].append(str("CD " + req["logCue"]))
    click.secho(f"Allowed   Media: {' | '.join(req['mediaList'])}")
    click.secho(
        "Description:",
        fg="cyan",
    )
    description = req["bbDescription"].splitlines(True)

    # Should probably be refactored out and a setting.
    line_limit = 5
    num_lines = len(description)
    if num_lines > line_limit:
        description = "".join(description[:line_limit]) + f"...{num_lines - line_limit} more lines..."
    else:
        description = "".join(description)
    rich.print(description)


def _prompt_for_request_id(gazelle_site, results):
    """Have the user choose a group ID"""
    while True:
        request_id = click.prompt(
            click.style("\nFill a request? Choose from results, paste a url, or do[n]t.", fg="magenta"),
            default="N",
        )
        if request_id.strip().isdigit():
            request_id = int(request_id) - 1  # User doesn't type zero index
            if request_id < 1:
                request_id = 0  # If the user types 0 give them the first choice.
            if request_id < len(results):
                request_id = results[request_id]["requestId"]
                return int(request_id)
            else:
                request_id = int(request_id) + 1
                click.echo(f"Interpreting {request_id} as a request id")
                return request_id

        elif request_id.strip().lower().startswith(gazelle_site.base_url + "/requests.php"):
            request_id = parse.parse_qs(parse.urlparse(request_id).query)["id"][0]
            return request_id
        elif request_id.lower().startswith("n") or not request_id.strip():
            click.echo("Not filling a request")
            return None


def _confirm_request_id(gazelle_site, request_id):
    """Have the user decide whether or not they want to fill request"""
    try:
        req = loop.run_until_complete(gazelle_site.request("request", id=request_id))
        req["artist"] = ""
        if len(req["musicInfo"]["artists"]) > 3:
            req["artist"] = "Various Artists"
        else:
            for a in req["musicInfo"]["artists"]:
                req["artist"] += a["name"] + " "
    except RequestError:
        click.secho(f"{request_id} does not exist.", fg="red")
        raise click.Abort from None
    _print_request_details(gazelle_site, req)
    if cfg.upload.yes_all:
        return True

    while True:
        resp = click.prompt(
            click.style("\nAre you sure you would you like to fill this request [Y]es, [n]o", fg="magenta"),
            default="Y",
        )[0].lower()
        if resp == "y":
            return True
        elif resp == "n":
            click.secho("Not filling this request", fg="red")
            return False
