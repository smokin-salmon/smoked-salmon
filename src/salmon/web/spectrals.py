import datetime

from aiohttp import web
from aiohttp_jinja2 import render_template

_active_spectrals: dict[int, str] = {}


async def handle_spectrals(request: web.Request) -> web.Response:
    if not _active_spectrals:
        raise web.HTTPNotFound()
    context = {"spectrals": _active_spectrals, "now": datetime.datetime.now()}
    return render_template("spectrals.html", request, context)


def _sanitize_filename(filename: str) -> str:
    """Encode filename to UTF-8, replacing undecodable characters."""
    return filename.encode("utf-8", "replace").decode("utf-8", "replace")


def set_active_spectrals(spectrals: dict[int, str]) -> None:
    """Replace active spectrals with the given mapping.

    Args:
        spectrals: Mapping of spectral ID to filename.
    """
    global _active_spectrals
    _active_spectrals = dict(sorted((k, _sanitize_filename(v)) for k, v in spectrals.items()))


def get_active_spectrals() -> dict[int, str]:
    """Return the currently active spectrals.

    Returns:
        Mapping of spectral ID to filename.
    """
    return _active_spectrals
