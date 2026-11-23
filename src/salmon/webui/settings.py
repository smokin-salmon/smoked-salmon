"""salmon web's Settings page: each tracker's session cookie and API key, in config.toml (#1034, ADR 0008).

The GET says whether each is set and, for a long one, its last characters: never a stored value. The POST changes
them in the file (``salmon.config.edit``) for the trackers salmon web started with. Neither sends anything to a
tracker; the page runs the connection check as a job of its own once a save is done. A job reads the file when it
starts, so a save reaches the jobs started after it.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec
from aiohttp import web

import salmon.trackers
from salmon.config.edit import (
    ConfigEditError,
    Credentials,
    CredentialsUpdate,
    read_credentials,
    update_credentials,
    writable,
)
from salmon.trackers.credentials import secrets_of
from salmon.webui.egress import Redactor

# A value shorter than this shows no ending: four characters would be too much of it.
MIN_SHOWN_LENGTH = 16
SHOWN_CHARS = 4
# The trackers that take no API key: no field for one.
NO_API_KEY = frozenset({"DIC"})


@dataclass(frozen=True)
class _Settings:
    config_path: Path
    redactor: Redactor


SETTINGS = web.AppKey("settings", _Settings)


def _error(status: int, detail: str) -> web.Response:
    return web.json_response({"detail": detail}, status=status)


def _shown(value: str | None) -> dict[str, Any]:
    if not value:
        return {"set": False, "ends_with": None}
    return {"set": True, "ends_with": value[-SHOWN_CHARS:] if len(value) >= MIN_SHOWN_LENGTH else None}


def _answer(settings: _Settings) -> dict[str, Any]:
    """What the page shows, read from the file as it is now."""
    path = settings.config_path
    error = None
    try:
        found = read_credentials(path)
    except ConfigEditError as e:
        found, error = {}, e.format_message()
    trackers = []
    for code in salmon.trackers.tracker_list:
        creds = found.get(code)
        trackers.append(
            {
                "code": code,
                "session": _shown(creds.session if creds else None),
                "api_key": None if code in NO_API_KEY else _shown(creds.api_key if creds else None),
            }
        )
    return {"path": str(path), "writable": writable(path), "error": error, "trackers": trackers}


async def trackers(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    return web.json_response(settings.redactor.value(await asyncio.to_thread(_answer, settings)))


def _refusal(code: str, update: CredentialsUpdate) -> str | None:
    if code in NO_API_KEY and update.api_key is not msgspec.UNSET:
        return f"{code} takes no API key: salmon uses its session cookie."
    if isinstance(update.session, str) and not update.session.strip():
        return "The session cookie cannot be empty."
    return None


async def save(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    code = request.match_info["code"].upper()
    if code not in salmon.trackers.tracker_list:
        return _error(404, "Not a tracker salmon web started with: adding a tracker needs a restart.")
    try:
        update = msgspec.json.decode(await request.read(), type=CredentialsUpdate)
    except msgspec.MsgspecError:
        return _error(400, 'Send {"session": "...", "api_key": "..." or null}, either one or both.')
    if refusal := _refusal(code, update):
        return _error(422, refusal)
    session = update.session if isinstance(update.session, str) else ""
    api_key = update.api_key if isinstance(update.api_key, str) else None
    # Masked before anything could repeat them: an error about the change, the next job's log.
    settings.redactor.add(secrets_of({code: Credentials(session, api_key)}))
    try:
        await asyncio.to_thread(update_credentials, settings.config_path, code, update)
    except ConfigEditError as e:
        return _error(422, settings.redactor.text(e.format_message()))
    return web.json_response(settings.redactor.value(await asyncio.to_thread(_answer, settings)))


def add_routes(app: web.Application, *, config_path: Path, redactor: Redactor) -> None:
    """Serve the Settings page's routes, editing the config at ``config_path``."""
    app[SETTINGS] = _Settings(config_path, redactor)
    app.router.add_get("/api/settings/trackers", trackers)
    app.router.add_post("/api/settings/trackers/{code}", save)
