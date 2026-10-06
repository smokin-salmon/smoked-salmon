from os.path import dirname, join
from typing import TYPE_CHECKING

from salmon import cfg
from salmon.web import spectrals

# aiohttp.web and jinja2 take about 0.1 s to import, so they are imported when the server starts, which only
# happens while spectrals are reviewed, not when salmon starts.
if TYPE_CHECKING:
    from aiohttp import web

web_cfg = cfg.upload.web_interface


async def create_app_async(specs_path: str | None = None) -> "web.AppRunner":
    """Create and start the aiohttp web application.

    Args:
        specs_path: The folder of spectral images to serve under the static URL's ``/specs``.

    Returns:
        The AppRunner instance for the web server.

    Raises:
        OSError: If the port is already in use.
    """
    import aiohttp_jinja2
    import jinja2
    from aiohttp import web

    app = web.Application()
    add_routes(app, specs_path)
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(join(dirname(__file__), "templates")))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, web_cfg.host, web_cfg.port)
    await site.start()
    return runner


def add_routes(app: "web.Application", specs_path: str | None = None) -> None:
    """Add routes to the web application.

    Args:
        app: The aiohttp web application.
        specs_path: The folder of spectral images to serve under the static URL's ``/specs``.
    """
    # The spectrals are served from their own folder, at the URL the templates use. Linking them into the package's
    # static folder needed a privilege Windows does not give by default (WinError 1314), wrote into the installed
    # package, and two salmon runs at once shared the one link.
    if specs_path is not None:
        app.router.add_static("/static/specs", specs_path)
    app.router.add_static("/static", join(dirname(__file__), "static"))
    app.router.add_route("GET", "/", handle_index)
    app.router.add_route("GET", "/spectrals", spectrals.handle_spectrals)
    app["static_root_url"] = web_cfg.static_root_url


async def handle_index(request: "web.Request") -> "web.Response":
    """Handle the index page request.

    Args:
        request: The aiohttp request object.

    Returns:
        The rendered index page response.
    """
    from aiohttp_jinja2 import render_template

    return render_template("index.html", request, {})
