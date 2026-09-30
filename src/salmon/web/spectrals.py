import datetime
from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

_active_spectrals: dict[int, str] = {}
_active_frequency_plots: frozenset[int] = frozenset()


async def handle_spectrals(request: "web.Request") -> "web.Response":
    from aiohttp import web
    from aiohttp_jinja2 import render_template

    if not _active_spectrals:
        raise web.HTTPNotFound()
    context = {
        "spectrals": _active_spectrals,
        "frequency_plots": _active_frequency_plots,
        "now": datetime.datetime.now(),
    }
    return render_template("spectrals.html", request, context)


def _sanitize_filename(filename: str) -> str:
    """Encode filename to UTF-8, replacing undecodable characters."""
    return filename.encode("utf-8", "replace").decode("utf-8", "replace")


def set_active_spectrals(spectrals: dict[int, str], frequency_plots: Collection[int] = ()) -> None:
    """Replace active spectrals with the given mapping.

    Args:
        spectrals: Mapping of spectral ID to filename.
        frequency_plots: The spectral IDs that also have an averaged-spectrum plot, "NN Spectrum.png".
    """
    global _active_spectrals, _active_frequency_plots
    _active_spectrals = dict(sorted((k, _sanitize_filename(v)) for k, v in spectrals.items()))
    _active_frequency_plots = frozenset(frequency_plots)


def get_active_spectrals() -> dict[int, str]:
    """Return the currently active spectrals.

    Returns:
        Mapping of spectral ID to filename.
    """
    return _active_spectrals
