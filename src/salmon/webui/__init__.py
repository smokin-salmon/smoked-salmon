"""``salmon web``: salmon in the browser (ADR 0004).

aiohttp.web is imported when the server starts, never when salmon starts (#368): the command is all this module holds.
"""

import asyncclick as click

from salmon import cfg
from salmon.common import commandgroup


@commandgroup.command()
@click.option("--host", default=None, help="Address to listen on [default: [web] host, else 127.0.0.1]")
@click.option(
    "--port", type=click.IntRange(1, 65535), default=None, help="Port to listen on [default: [web] port, else 55155]"
)
@click.option("--dev", is_flag=True, help="Accept requests from the Vite dev server (http://localhost:5173)")
async def web(host: str | None, port: int | None, dev: bool) -> None:
    """Start the web interface"""
    from salmon.webui.server import serve

    await serve(host or cfg.web.host, port or cfg.web.port, dev=dev)
