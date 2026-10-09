"""The ``salmon web`` server: login, the request rules, jobs, and the committed front-end build (ADR 0004).

Ported from styx-techno's first ``salmon web`` (5f1f55a6) and chodeus's token auth (995928d1, 18d685fe, ab23ae24),
and the fork's jobs router, from FastAPI to aiohttp, which salmon already uses: no new dependency.
"""

import asyncio
import contextlib
import os
import signal
import weakref
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import msgspec
from aiohttp import WSCloseCode, web
from aiohttp.typedefs import Handler

from salmon import cfg
from salmon.trackers import account
from salmon.webui import output
from salmon.webui.auth import COOKIE_NAME, SESSION_MAX_AGE, Auth, resolve_token
from salmon.webui.egress import Redactor, config_secrets
from salmon.webui.jobs import Job, JobError, JobManager
from salmon.webui.security import DEV_ORIGINS, host_allowed, is_loopback, origin_allowed

STATIC_DIR = Path(__file__).parent / "static"
# The routes a browser reaches before logging in.
PUBLIC_API = frozenset({("POST", "/api/login"), ("GET", "/api/health"), ("HEAD", "/api/health")})
SAFE_METHODS = frozenset({"GET", "HEAD"})

AUTH = web.AppKey("auth", Auth)
BIND_HOST = web.AppKey("bind_host", str)
ALLOWED_HOSTS = web.AppKey("allowed_hosts", list[str])
DEV = web.AppKey("dev", bool)
BUILD_FILES = web.AppKey("build_files", dict[str, Path])
JOBS = web.AppKey("jobs", JobManager)
SOCKETS = web.AppKey("sockets", weakref.WeakSet[web.WebSocketResponse])


class LoginRequest(msgspec.Struct):
    token: str


class StartJobRequest(msgspec.Struct, forbid_unknown_fields=True):
    kind: str
    params: dict[str, Any] = msgspec.field(default_factory=dict)
    dry_run: bool = False
    # A job's -yyy: its questions are answered with their defaults where the CLI would.
    assume_defaults: bool = False


class AnswerRequest(msgspec.Struct, forbid_unknown_fields=True):
    question_id: str
    value: str | bool | None = None


def _error(status: int, detail: str) -> web.Response:
    return web.json_response({"detail": detail}, status=status)


@web.middleware
async def dev_cors(request: web.Request, handler: Handler) -> web.StreamResponse:
    """With --dev, let the Vite dev server's pages call the API with their cookie."""
    origin = request.headers.get("Origin")
    if origin not in DEV_ORIGINS:
        return await handler(request)
    if request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers:
        response = web.Response(status=204)
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, POST, PUT, PATCH, DELETE"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Vary"] = "Origin"
    return response


@web.middleware
async def request_rules(request: web.Request, handler: Handler) -> web.StreamResponse:
    """The Host check on every request; JSON and same origin on every change; the login on the API."""
    app = request.app
    host = request.headers.get("Host")
    if not host_allowed(host, app[BIND_HOST], app[ALLOWED_HOSTS]):
        return _error(403, "This host name is not allowed. Add it to allowed_hosts in the [web] config section.")
    if request.method not in SAFE_METHODS:
        # A cross-site form or a simple fetch cannot send JSON; a preflighted one is refused by the browser.
        if request.content_type != "application/json":
            return _error(415, "Requests other than GET and HEAD must be sent as application/json.")
        if not origin_allowed(request.headers.get("Origin"), host, app[DEV]):
            return _error(403, "This request comes from another site.")
    if (
        _is_api(request.path)
        and (request.method, request.path) not in PUBLIC_API
        and not app[AUTH].authenticated(request.headers.get("Authorization"), request.cookies.get(COOKIE_NAME))
    ):
        return _error(401, "Authentication required.")
    return await handler(request)


async def security_headers(_request: web.Request, response: web.StreamResponse) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _secure(request: web.Request) -> bool:
    # Behind a TLS reverse proxy the request arrives as http. Trusting the header here can only add Secure to the
    # cookie, which keeps it off plain http: a client that forges it only breaks its own login.
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


async def login(request: web.Request) -> web.Response:
    try:
        body = msgspec.json.decode(await request.read(), type=LoginRequest)
    except msgspec.MsgspecError:
        return _error(400, 'Send {"token": "..."}.')
    auth = request.app[AUTH]
    if not auth.token_matches(body.token):
        return _error(401, "Invalid token.")
    response = web.json_response({"authenticated": True})
    response.set_cookie(
        COOKIE_NAME,
        auth.open_session(),
        max_age=SESSION_MAX_AGE,
        path="/",
        httponly=True,
        samesite="Strict",
        secure=_secure(request),
    )
    return response


async def logout(request: web.Request) -> web.Response:
    request.app[AUTH].close_session(request.cookies.get(COOKIE_NAME))
    response = web.json_response({"authenticated": False})
    response.del_cookie(COOKIE_NAME, path="/", httponly=True, samesite="Strict", secure=_secure(request))
    return response


async def auth_status(_request: web.Request) -> web.Response:
    # The middleware already refused a request that is not logged in.
    return web.json_response({"authenticated": True})


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def api_not_found(_request: web.Request) -> web.Response:
    return _error(404, "Not found.")


# --- Jobs ------------------------------------------------------------------------------


def _job(request: web.Request) -> Job:
    job = request.app[JOBS].jobs.get(request.match_info["job_id"])
    if job is None:
        raise web.HTTPNotFound(text='{"detail": "No such job."}', content_type="application/json")
    return job


async def list_jobs(request: web.Request) -> web.Response:
    """Every job, the newest first, without their logs."""
    return web.json_response({"jobs": [job.summary() for job in reversed(request.app[JOBS].jobs.values())]})


async def start_job(request: web.Request) -> web.Response:
    try:
        body = msgspec.json.decode(await request.read(), type=StartJobRequest)
    except msgspec.MsgspecError:
        return _error(400, 'Send {"kind": "...", "params": {...}}, with "dry_run" and "assume_defaults" if wanted.')
    try:
        job = request.app[JOBS].start(
            body.kind, body.params, dry_run=body.dry_run, assume_defaults=body.assume_defaults
        )
    except JobError as e:
        return _error(e.status, e.detail)
    return web.json_response(job.summary(), status=201)


async def get_job(request: web.Request) -> web.Response:
    return web.json_response(_job(request).detail())


async def answer_job(request: web.Request) -> web.Response:
    job = _job(request)
    try:
        body = msgspec.json.decode(await request.read(), type=AnswerRequest)
    except msgspec.MsgspecError:
        return _error(400, 'Send {"question_id": "...", "value": ...}.')
    if not request.app[JOBS].answer(job.id, body.question_id, body.value):
        return _error(409, "This job has no such question open: it was answered already, or the job has ended.")
    return web.json_response({"answered": body.question_id})


async def cancel_job(request: web.Request) -> web.Response:
    job = _job(request)
    if not request.app[JOBS].cancel(job.id):
        return _error(409, "This job has ended already.")
    return web.json_response({"cancelled": job.id})


async def job_spectral(request: web.Request) -> web.StreamResponse:
    """One of the spectral images a job shows, from its spectrals folder only."""
    job = _job(request)
    name = request.match_info["name"]
    if job.spectrals is None or name not in job.spectrals[1]:
        return _error(404, "No such spectral image.")
    folder = job.spectrals[0]
    path = os.path.join(folder, name)
    if os.path.islink(path) or not os.path.isfile(path) or os.path.dirname(os.path.realpath(path)) != folder:
        return _error(404, "No such spectral image.")
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def job_events(request: web.Request) -> web.StreamResponse:
    """Every job event, as it happens, over a websocket. Logged in as the API is; from this site only."""
    if not origin_allowed(request.headers.get("Origin"), request.headers.get("Host"), request.app[DEV]):
        return _error(403, "This connection comes from another site.")
    manager = request.app[JOBS]
    subscriber = manager.subscribe()
    if subscriber is None:
        return _error(503, "Too many connections: close another salmon web tab.")
    socket = web.WebSocketResponse(heartbeat=30)
    try:
        await socket.prepare(request)
        request.app[SOCKETS].add(socket)

        async def send() -> None:
            while (event := await subscriber.events.get()) is not None:
                await socket.send_json(event)
            # Too far behind: the browser connects again and reloads the jobs.
            await socket.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"Reconnect")

        sending = asyncio.create_task(send())
        try:
            # The browser sends nothing; this ends when either side closes.
            async for _message in socket:
                pass
        finally:
            sending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sending
    finally:
        manager.unsubscribe(subscriber)
    return socket


async def _jobs_running(app: web.Application) -> AsyncIterator[None]:
    """While the server runs: jobs, their output captured, and every tracker request on this loop."""
    with output.capturing():
        async with account.requests_on_this_loop():
            app[JOBS].open()
            try:
                yield
            finally:
                await app[JOBS].stop()


async def _close_sockets(app: web.Application) -> None:
    for socket in list(app[SOCKETS]):
        await socket.close(code=WSCloseCode.GOING_AWAY, message=b"salmon web is stopping")


def build_files(root: Path) -> dict[str, Path]:
    """The build's files by URL path. Requests are looked up here: no path is made from what a request asks for."""
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}


async def front_end(request: web.Request) -> web.StreamResponse:
    """A file of the build, else ``index.html``: the app routes the other paths itself."""
    files = request.app[BUILD_FILES]
    name = request.match_info["tail"]
    path = files.get(name)
    if path is not None and name != "index.html":
        response = web.FileResponse(path)
        if name.startswith("assets/"):
            # Vite names these after their content.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
    if name.startswith("assets/"):
        raise web.HTTPNotFound()
    response = web.FileResponse(files["index.html"])
    response.headers["Cache-Control"] = "no-cache"
    return response


def create_app(
    token: str, bind_host: str, allowed_hosts: list[str], dev: bool = False, max_jobs: int | None = None
) -> web.Application:
    app = web.Application(middlewares=[dev_cors, request_rules] if dev else [request_rules])
    app[AUTH] = Auth(token)
    app[BIND_HOST] = bind_host
    app[ALLOWED_HOSTS] = list(allowed_hosts)
    app[DEV] = dev
    app[BUILD_FILES] = build_files(STATIC_DIR)
    app[JOBS] = JobManager(max_jobs or cfg.web.max_jobs, Redactor([*config_secrets(cfg), token]))
    app[SOCKETS] = weakref.WeakSet()
    app.cleanup_ctx.append(_jobs_running)
    app.on_shutdown.append(_close_sockets)
    app.on_response_prepare.append(security_headers)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/auth", auth_status)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/jobs", list_jobs)
    app.router.add_post("/api/jobs", start_job)
    app.router.add_get("/api/jobs/{job_id}", get_job)
    app.router.add_post("/api/jobs/{job_id}/answer", answer_job)
    app.router.add_post("/api/jobs/{job_id}/cancel", cancel_job)
    app.router.add_get("/api/jobs/{job_id}/spectrals/{name}", job_spectral)
    app.router.add_get("/api/ws", job_events)
    app.router.add_route("*", "/api/{tail:.*}", api_not_found)
    app.router.add_get("/{tail:.*}", front_end)
    return app


def _url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


async def serve(host: str, port: int, dev: bool = False) -> None:
    """Run the server until Ctrl-C or SIGTERM.

    Raises:
        click.ClickException: No token for a non-loopback bind (see ``resolve_token``), or the port is taken.
    """
    token, generated = resolve_token(host, cfg.web.token)
    runner = web.AppRunner(create_app(token, host, cfg.web.allowed_hosts, dev=dev))
    await runner.setup()
    try:
        with contextlib.ExitStack() as stack:
            # Before the server starts, so a signal sent as soon as it says it listens still stops it cleanly. As
            # PID 1 (docker stop), salmon would otherwise ignore SIGTERM.
            try:
                signals = stack.enter_context(anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM))
            except NotImplementedError:  # Windows: Ctrl-C ends the run as anywhere else in salmon.
                signals = None
            try:
                await web.TCPSite(runner, host, port).start()
            except OSError as e:
                raise click.ClickException(f"salmon web cannot listen on {host}:{port}: {e.strerror or e}") from e
            _announce(host, port, token, generated)
            if signals is None:
                await anyio.sleep_forever()
            else:
                async for _ in signals:
                    break
        click.echo("salmon web: stopped.")
    finally:
        await runner.cleanup()


def _announce(host: str, port: int, token: str, generated: bool) -> None:
    if generated:
        click.secho(f"salmon web: log in at {_url(host, port)}#token={token}", fg="cyan", bold=True)
        click.echo("A new token is made at each start; set SALMON_WEB_TOKEN or [web] token to keep one.")
    else:
        click.secho(f"salmon web: listening on {_url(host, port)}", fg="cyan", bold=True)
        click.echo("Log in with the token from SALMON_WEB_TOKEN or [web] token.")
    if not is_loopback(host):
        click.secho(
            "salmon web serves plain HTTP: the token and the login cookie cross the network in clear. "
            "Unless you trust this network, reach it through a TLS reverse proxy, an SSH tunnel or a VPN.",
            fg="yellow",
        )
        click.echo(
            "Browsers must reach it by a loopback name, its bind address or a name listed in [web] allowed_hosts "
            "(this machine's LAN name or address, for instance); any other name is refused."
        )
