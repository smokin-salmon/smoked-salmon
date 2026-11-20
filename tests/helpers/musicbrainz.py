"""A local MusicBrainz and Cover Art Archive answering from the recordings in tests/fixtures/stores/musicbrainz/."""

import contextlib
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from salmon.sources.musicbrainz import MusicBrainzBase

RECORDINGS = Path(__file__).parent.parent / "fixtures" / "stores" / "musicbrainz"


def slug(query: str) -> str:
    """A search's file name, as the recordings were saved."""
    return re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")


class FakeMusicBrainz:
    """A threaded server on 127.0.0.1: /ws/2/release/<mbid>, /ws/2/release?query=… and the CAA's /release/<mbid>.

    It answers from the recordings, or a 404 where there is none. `status` holds the statuses of the next requests,
    one each, before it answers 200 again; `body` replaces every answer's body, `headers` adds to every answer's
    headers, and `delay` holds every answer back that many seconds. Each request is kept as (path, query, headers).
    """

    def __init__(self) -> None:
        self.status: list[int] = []
        self.body: str | None = None
        self.headers: dict[str, str] = {}
        self.delay = 0.0
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.released = threading.Event()  # Set at close, so no handler outlives the test
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                query = dict(parse_qsl(parts.query))
                fake.requests.append((parts.path, query, dict(self.headers)))
                if fake.delay and fake.released.wait(fake.delay):
                    return  # The client gave up long ago: nothing to answer
                status, payload = fake.answer(parts.path, query)
                self.send_response(status)
                for name, value in {"Content-Type": "application/json", **fake.headers}.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def answer(self, path: str, query: dict[str, str]) -> tuple[int, bytes]:
        if self.status:
            return self.status.pop(0), (self.body or "{}").encode()
        if self.body is not None:
            return 200, self.body.encode()
        if path == "/ws/2/release":
            recorded = RECORDINGS / "search" / f"{slug(query.get('query', ''))}.json"
        elif path.startswith("/ws/2/release/"):
            recorded = RECORDINGS / "release" / f"{path.rsplit('/', 1)[1]}.json"
        else:
            recorded = RECORDINGS / "caa" / f"{path.rsplit('/', 1)[1]}.json"
        if not recorded.is_file():
            return 404, b'{"error": "Not Found"}'
        return 200, recorded.read_bytes()

    def close(self) -> None:
        self.released.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@contextlib.contextmanager
def serving_musicbrainz(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMusicBrainz]:
    """MusicBrainz and the Cover Art Archive pointed at a FakeMusicBrainz for the test."""
    fake = FakeMusicBrainz()
    monkeypatch.setattr(MusicBrainzBase, "api_url", f"{fake.url}/ws/2")
    monkeypatch.setattr(MusicBrainzBase, "caa_url", fake.url)
    try:
        yield fake
    finally:
        fake.close()
