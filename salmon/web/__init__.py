import asyncio
from os.path import dirname, join

import aiohttp
import aiohttp_jinja2
import click
import jinja2
from aiohttp_jinja2 import render_template

from salmon import cfg
from salmon.common import commandgroup
from salmon.errors import WebServerIsAlreadyRunning
from salmon.web import spectrals

web_cfg = cfg.upload.web_interface


@commandgroup.command()
def web():
    """Start the salmon web server"""
    click.secho(f"Running webserver on http://{web_cfg.host}:{web_cfg.port}", fg="cyan")
    asyncio.run(_serve_web())


def create_app():
    app = aiohttp.web.Application()
    add_routes(app)
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(join(dirname(__file__), "templates")))
    return app


async def create_app_async():
    app = create_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", web_cfg.port)
    try:
        await site.start()
    except OSError as err:
        raise WebServerIsAlreadyRunning from err
    return runner


async def _serve_web():
    runner = await create_app_async()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def add_routes(app):
    app.router.add_static("/static", join(dirname(__file__), "static"), follow_symlinks=True)
    app.router.add_route("GET", "/", handle_index)
    app.router.add_route("GET", "/spectrals", spectrals.handle_spectrals)
    app["static_root_url"] = web_cfg.static_root_url


def handle_index(request, **kwargs):
    return render_template("index.html", request, {})
