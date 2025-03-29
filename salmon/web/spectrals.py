import datetime
import sqlite3
from itertools import chain

import aiohttp
from aiohttp_jinja2 import render_template

from salmon.database import DB_PATH


async def handle_spectrals(request, **kwargs):
    active_spectrals = get_active_spectrals()
    if active_spectrals:
        active_spectrals['now'] = datetime.datetime.now()
        return render_template("spectrals.html", request, active_spectrals)
    return aiohttp.web.HTTPNotFound()


def set_active_spectrals(spectrals):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("DELETE FROM spectrals")
        cursor.execute(
            "INSERT INTO spectrals (id, filename) VALUES "
            + ", ".join("(?, ?)" for _ in range(len(spectrals))),
            tuple(chain.from_iterable(list(spectrals.items()))),
        )
        conn.commit()


def get_active_spectrals():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, filename FROM spectrals ORDER BY ID ASC")
        return {"spectrals": {r["id"]: r["filename"] for r in cursor.fetchall()}}
