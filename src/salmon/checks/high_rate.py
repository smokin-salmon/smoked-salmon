"""16bit files above 48 kHz: what each tracker's rule means for them."""

from collections.abc import Mapping
from typing import Any

# Files named in a notice before "and N more".
_NAMED = 8


def sixteen_bit_above_48khz(audio_info: Mapping[str, dict[str, Any]]) -> dict[str, int]:
    """The 16bit files above 48 kHz, with their sample rate."""
    return {
        name: info["sample rate"]
        for name, info in audio_info.items()
        if info.get("precision") == 16 and (info.get("sample rate") or 0) > 48000
    }


def sixteen_bit_notice(tracker: str, rule: str, audio_info: Mapping[str, dict[str, Any]]) -> str | None:
    """What the tracker's rule means for these files, if they break it.

    Args:
        tracker: The site code.
        rule: Its TagRules.sixteen_bit_above_48khz: "refused", "trumpable", or "" for no rule.
    """
    files = sixteen_bit_above_48khz(audio_info)
    if not files or rule not in ("refused", "trumpable"):
        return None
    named = "; ".join(f"{name} ({rate / 1000:g} kHz)" for name, rate in list(files.items())[:_NAMED])
    if len(files) > _NAMED:
        named += f"; and {len(files) - _NAMED} more"
    found = f"{len(files)} 16bit file(s) above 48 kHz: {named}."
    return f"{found} {tracker} refuses them." if rule == "refused" else f"{found} {tracker} can trump them."
