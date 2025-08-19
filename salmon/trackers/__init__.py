from urllib import parse

import click

from salmon import cfg
from salmon.trackers import ops, red

# hard coded as it needs to reflect the imports anyway.
tracker_classes = {"RED": red.RedApi, "OPS": ops.OpsApi}
tracker_url_code_map = {"redacted.sh": "RED", "orpheus.network": "OPS"}

# tracker_list is used to offer the user choices. Generated if not specified in the config.
tracker_cfg = cfg.tracker
tracker_list = []
if tracker_cfg.red:
    tracker_list.append("RED")
if tracker_cfg.ops:
    tracker_list.append("OPS")


def get_class(site_code):
    "Returns the api class from the tracker string."
    return tracker_classes[site_code]


def choose_tracker(choices):
    """Allows the user to choose a tracker from choices."""
    while True:
        # Loop until we have chosen a tracker or aborted.
        tracker_input = click.prompt(
            click.style(f"Your choices are {' , '.join(choices)} or [a]bort.", fg="magenta"),
            default=choices[0],
        )
        tracker_input = tracker_input.strip().upper()
        if tracker_input in choices:
            click.secho(f"Using tracker: {tracker_input}", fg="green")
            return tracker_input
        # this part allows input of the first letter of the tracker.
        elif tracker_input in [choice[0] for choice in choices]:
            for choice in choices:
                if tracker_input == choice[0]:
                    click.secho(f"Using tracker: {choice}", fg="green")
                    return choice
        elif tracker_input.lower().startswith("n"):
            return None


def choose_tracker_first_time(question="Which tracker would you like to upload to?"):
    """Specific logic for the first time a tracker choice is offered.
    Uses default if there is one and uses the only tracker if there is only one."""
    choices = tracker_list
    if len(choices) == 1:
        click.secho(f"Using tracker: {choices[0]}")
        return choices[0]
    if tracker_cfg.default_tracker:
        click.secho(f"Using tracker: {tracker_cfg.default_tracker}", fg="green")
        return tracker_cfg.default_tracker
    click.secho(question, fg="magenta")
    tracker = choose_tracker(choices)
    return tracker


def validate_tracker(ctx, param, value):
    """Only allow trackers in the config tracker dict.
    If it isn't there. Prompt to choose.
    """
    try:
        if value is None:
            return choose_tracker_first_time()
        if value.upper() in tracker_list:
            click.secho(f"Using tracker: {value.upper()}", fg="green")
            return value.upper()
        else:
            click.secho(f"{value} is not a tracker in your config.", fg="red")
            return choose_tracker(tracker_list)
    except AttributeError:
        raise click.BadParameter(
            "This flag requires a tracker. Possible sources are: " + ", ".join(tracker_list)
        ) from None


def validate_request(gazelle_site, request):
    """Check the request id is a url or number. and return the number.
    Should it check more? Currently not checking it is the right tracker.
    """
    try:
        if request is None:
            return None
        if request.strip().isdigit():
            pass
        elif request.strip().lower().startswith(gazelle_site.base_url + "/requests.php"):
            request = parse.parse_qs(parse.urlparse(request).query)["id"][0]
        click.secho(
            f"Attempting to fill {gazelle_site.base_url}/requests.php?action=view&id={request}",
            fg="green",
        )
        return request
    except (KeyError, AttributeError):
        raise click.BadParameter("This flag requires a request, either as a url or ID") from None
