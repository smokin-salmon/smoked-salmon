"""Per-image-host rules, as data.

A leaf module: no salmon imports, so it can be imported both from salmon.config.validations
(which is still initialising when Cfg gets built) and from salmon.images (which needs cfg to
be ready before it imports anything else), without either side pulling in the other's
dependencies.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HostRules:
    """Per-image-host rules: where its images may display, and whether it may host spectrals."""

    # Tracker codes whose pages can display this host's images. None means every tracker can.
    # Otherwise the host can only be set under [image.<tracker>] for those trackers, never in
    # the shared [image] settings.
    displays_on: tuple[str, ...] | None = None
    # Why this host may never be used as specs_uploader, for any tracker. None means spectrals
    # are allowed wherever displays_on allows the host.
    spectrals_refused: str | None = None


HOST_RULES: dict[str, HostRules] = {
    "red": HostRules(
        displays_on=("red", "ops"),
        spectrals_refused="RED's rules forbid spectrals on its image host",
    ),
    "ra": HostRules(spectrals_refused="Ra's owner asks not to use it for spectrals"),
}


def tracker_only_hosts() -> dict[str, tuple[str, ...]]:
    """Hosts that can only be set under [image.<tracker>] for certain trackers, mapped to those tracker codes."""
    return {host: rules.displays_on for host, rules in HOST_RULES.items() if rules.displays_on is not None}


def _display_refusal(rules: HostRules, tracker: str | None) -> str | None:
    """Why a host with these rules may not be used for `tracker`'s images, or for every tracker's if None."""
    if rules.displays_on is None or (tracker is not None and tracker.lower() in rules.displays_on):
        return None
    codes = "/".join(code.upper() for code in rules.displays_on)
    return f"its images only display on {codes}"


def spectrals_refusal(host: str, tracker: str | None = None) -> str | None:
    """Why `host` may not be used as specs_uploader, or None if it is allowed.

    Args:
        host: The image host.
        tracker: The site code (e.g. "RED") whose [image.<tracker>] specs_uploader it would be, or None for the
            shared [image] specs_uploader, whose spectrals every tracker shows.
    """
    rules = HOST_RULES.get(host)
    if rules is None:
        return None
    reasons = [reason for reason in (rules.spectrals_refused, _display_refusal(rules, tracker)) if reason is not None]
    return ", and ".join(reasons) or None


def cover_refusal(host: str, tracker: str) -> str | None:
    """Why `host` may not be used as a cover host for `tracker` (a site code, e.g. "RED"), or None if allowed."""
    rules = HOST_RULES.get(host)
    return None if rules is None else _display_refusal(rules, tracker)
