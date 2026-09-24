from dataclasses import dataclass


@dataclass(frozen=True)
class HostRules:
    """Per-image-host rules: where its images may display, and whether it may host spectrals."""

    # Tracker codes whose pages can display this host's images. None means every tracker can,
    # so the host is not restricted to being a cover host for particular trackers.
    displays_on: tuple[str, ...] | None = None
    # Why this host may never be used as specs_uploader. None means spectrals are allowed,
    # subject to the displays_on restriction (if any) applying to every [image] setting.
    spectrals_refused: str | None = None


HOST_RULES: dict[str, HostRules] = {
    "red": HostRules(
        displays_on=("red", "ops"),
        spectrals_refused="RED's rules forbid spectrals on its image host",
    ),
    "ra": HostRules(spectrals_refused="Ra's owner asks not to use it for spectrals"),
}


def tracker_only_hosts() -> dict[str, tuple[str, ...]]:
    """Hosts valid only as a cover host for certain trackers, mapped to those tracker codes."""
    return {host: rules.displays_on for host, rules in HOST_RULES.items() if rules.displays_on is not None}


def spectrals_refusal(host: str) -> str | None:
    """Why `host` may not be used as specs_uploader, or None if it is allowed."""
    rules = HOST_RULES.get(host)
    if rules is None:
        return None
    if rules.displays_on is not None:
        codes = "/".join(code.upper() for code in rules.displays_on)
        display_reason = f"its images only display on {codes}"
        if rules.spectrals_refused is not None:
            return f"{rules.spectrals_refused}, and {display_reason}"
        return display_reason
    return rules.spectrals_refused
