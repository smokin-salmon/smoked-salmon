import datetime
import sqlite3
from itertools import chain
from typing import Any

from aiohttp import web
from aiohttp_jinja2 import render_template

from salmon.database import DB_PATH


async def handle_spectrals(request: web.Request, **kwargs) -> web.Response:
    active_spectrals: dict[str, Any] = get_active_spectrals()
    if active_spectrals.get("spectrals"):
        active_spectrals["now"] = datetime.datetime.now()
        return render_template("spectrals.html", request, active_spectrals)
    raise web.HTTPNotFound()


def set_active_spectrals(spectrals):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("DELETE FROM spectrals")
        cursor.execute(
            "INSERT INTO spectrals (id, filename) VALUES " + ", ".join("(?, ?)" for _ in range(len(spectrals))),
            tuple(chain.from_iterable(list(spectrals.items()))),
        )
        conn.commit()


def get_active_spectrals():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, filename FROM spectrals ORDER BY ID ASC")
        return {"spectrals": {r["id"]: r["filename"] for r in cursor.fetchall()}}
