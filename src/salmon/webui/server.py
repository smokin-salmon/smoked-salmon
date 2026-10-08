"""The ``salmon web`` server: login, the request rules, and the committed front-end build (ADR 0004).

Ported from styx-techno's first ``salmon web`` (5f1f55a6) and chodeus's token auth (995928d1, 18d685fe, ab23ae24),
from FastAPI to aiohttp, which salmon already uses: no new dependency.
"""

import contextlib
import signal
from pathlib import Path

import anyio
import asyncclick as click
import msgspec
from aiohttp import web
from aiohttp.typedefs import Handler

from salmon import cfg
from salmon.webui.auth import COOKIE_NAME, SESSION_MAX_AGE, Auth, resolve_token
from salmon.webui.security import DEV_ORIGINS, host_allowed, is_loopback, origin_allowed

STATIC_DIR = Path(__file__).parent / "static"
# The routes a browser reaches before logging in.
PUBLIC_API = frozenset({("POST", "/api/login"), ("GET", "/api/health"), ("HEAD", "/api/health")})
SAFE_METHODS = frozenset({"GET", "HEAD"})

AUTH = web.AppKey("auth", Auth)
BIND_HOST = web.AppKey("bind_host", str)
ALLOWED_HOSTS = web.AppKey("allowed_hosts", list[str])
DEV = web.AppKey("dev", bool)


class LoginRequest(msgspec.Struct):
    token: str


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


async def front_end(request: web.Request) -> web.StreamResponse:
    """A file of the build, else ``index.html``: the app routes the other paths itself."""
    root = STATIC_DIR.resolve()
    path = (root / request.match_info["tail"]).resolve()
    if path.is_relative_to(root) and path.is_file():
        response = web.FileResponse(path)
        if path.parent == root / "assets":
            # Vite names these after their content.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
    if request.match_info["tail"].startswith("assets/"):
        raise web.HTTPNotFound()
    response = web.FileResponse(root / "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


def create_app(token: str, bind_host: str, allowed_hosts: list[str], dev: bool = False) -> web.Application:
    app = web.Application(middlewares=[dev_cors, request_rules] if dev else [request_rules])
    app[AUTH] = Auth(token)
    app[BIND_HOST] = bind_host
    app[ALLOWED_HOSTS] = list(allowed_hosts)
    app[DEV] = dev
    app.on_response_prepare.append(security_headers)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/auth", auth_status)
    app.router.add_get("/api/health", health)
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
