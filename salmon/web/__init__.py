import asyncio
from os.path import dirname, join

import aiohttp
import aiohttp_jinja2
import asyncclick as click
import jinja2
from aiohttp_jinja2 import render_template

from salmon import cfg
from salmon.common import commandgroup
from salmon.errors import WebServerIsAlreadyRunning
from salmon.web import spectrals

web_cfg = cfg.upload.web_interface


@commandgroup.command()
async def web() -> None:
    """Start the salmon web server."""
    click.secho(f"Running webserver on http://{web_cfg.host}:{web_cfg.port}", fg="cyan")
    runner = await create_app_async()
    try:
        # Keep the server running
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()


async def create_app_async() -> aiohttp.web.AppRunner:
    """Create and start the aiohttp web application.

    Returns:
        The AppRunner instance for the web server.

    Raises:
        WebServerIsAlreadyRunning: If the port is already in use.
    """
    app = aiohttp.web.Application()
    add_routes(app)
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(join(dirname(__file__), "templates")))
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", web_cfg.port)
    try:
        await site.start()
    except OSError as err:
        raise WebServerIsAlreadyRunning from err
    return runner


def add_routes(app: aiohttp.web.Application) -> None:
    """Add routes to the web application.

    Args:
        app: The aiohttp web application.
    """
    app.router.add_static("/static", join(dirname(__file__), "static"), follow_symlinks=True)
    app.router.add_route("GET", "/", handle_index)
    app.router.add_route("GET", "/spectrals", spectrals.handle_spectrals)
    app["static_root_url"] = web_cfg.static_root_url


def handle_index(request: aiohttp.web.Request, **kwargs) -> aiohttp.web.Response:
    """Handle the index page request.

    Args:
        request: The aiohttp request object.
        **kwargs: Additional keyword arguments.

    Returns:
        The rendered index page response.
    """
    return render_template("index.html", request, {})
