"""MusicBrainz requests are bounded, and their failures are ScrapeErrors (#786).

musicbrainzngs, not aiohttp: the requests go to a local threaded server through its own set_hostname globals.
"""

import inspect
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest
from musicbrainzngs import caa
from musicbrainzngs import musicbrainz as mb_module

from salmon.errors import ScrapeError
from salmon.search import musicbrainz as musicbrainz_search
from salmon.sources import musicbrainz
from salmon.tagger.sources import musicbrainz as musicbrainz_tagger

RELEASE_ID = "0a1b2c3d-0000-0000-0000-000000000000"
RELEASE_URL = f"https://musicbrainz.org/release/{RELEASE_ID}"
RELEASE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">'
    f'<release id="{RELEASE_ID}"><title>Album</title></release></metadata>'
)


class _Server:
    """A local server answering every request with `status` and `body`, after waiting `delay` seconds."""

    def __init__(self) -> None:
        self.status = 200
        self.body = RELEASE_XML
        self.delay = 0.0
        self.requests: list[str] = []
        self.released = threading.Event()  # Set at teardown, so no handler outlives the test
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.requests.append(self.path)
                if server.delay and server.released.wait(server.delay):
                    return  # The client gave up long ago: nothing to answer
                payload = server.body.encode()
                self.send_response(server.status)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.released.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch) -> Iterator[_Server]:
    """MusicBrainz and the Cover Art Archive pointed at a local server, with no rate limit and no real sleeps."""
    local = _Server()
    for module in (mb_module, caa):
        monkeypatch.setattr(module, "hostname", local.host)
        monkeypatch.setattr(module, "https", False)
    monkeypatch.setattr(mb_module, "do_rate_limit", False)
    yield local
    local.close()


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Every time.sleep during the test, recorded instead of slept: here, musicbrainzngs's between retries."""
    slept: list[float] = []
    monkeypatch.setattr(mb_module.time, "sleep", slept.append)
    return slept


def _fetch_release() -> dict:
    return anyio.run(musicbrainz_tagger.Scraper().fetch_data, RELEASE_URL)


def test_safe_read_keeps_the_signature_the_wrapper_calls() -> None:
    # The bound wraps a private function of musicbrainzngs: an upgrade that changes it must fail here, not quietly.
    parameters = list(inspect.signature(musicbrainz._safe_read).parameters)

    assert parameters[:3] == ["opener", "req", "body"]
    assert "max_retries" in parameters
    assert mb_module._safe_read is musicbrainz._bounded_safe_read


def test_a_release_is_fetched_through_the_bounded_read(server) -> None:
    release = _fetch_release()

    assert release["title"] == "Album"
    assert len(server.requests) == 1


def test_a_failing_server_gets_two_requests_and_one_sleep(server, sleeps) -> None:
    # 503 every time: musicbrainzngs made 8 requests and slept 56 s, then raised NetworkError past the scrape.
    server.status = 503

    with pytest.raises(ScrapeError) as excinfo:
        _fetch_release()

    assert str(excinfo.value) == "MusicBrainz: Request failed (NetworkError)."
    assert len(server.requests) == 2
    assert sleeps == [2.0]


def test_a_server_that_does_not_answer_times_out(server, sleeps, monkeypatch) -> None:
    # No timeout at all: a server that stopped answering held the scrape, and STORE_LOCK under salmon web.
    monkeypatch.setattr(musicbrainz, "REQUEST_TIMEOUT", 0.2, raising=False)
    server.delay = 3

    started = time.monotonic()
    with pytest.raises(ScrapeError):
        _fetch_release()

    assert time.monotonic() - started < 2
    assert len(server.requests) == 2


def test_a_release_not_on_musicbrainz_is_said_so(server) -> None:
    server.status = 404

    with pytest.raises(ScrapeError) as excinfo:
        _fetch_release()

    assert str(excinfo.value) == "MusicBrainz: Not found."
    assert len(server.requests) == 1


def test_a_failed_search_is_a_scrape_error(server, sleeps) -> None:
    server.status = 503

    with pytest.raises(ScrapeError) as excinfo:
        anyio.run(musicbrainz_search.Searcher().search_releases, "artist album", 5)

    assert str(excinfo.value) == "MusicBrainz: Request failed (NetworkError)."
    assert len(server.requests) == 2


def test_a_failed_cover_art_archive_request_is_no_cover_and_says_so(server, sleeps, capsys) -> None:
    # The cover lookup caught a 404 only: a Cover Art Archive that failed failed the whole scrape.
    server.status = 503
    release = {"id": RELEASE_ID, "cover-art-archive": {"front": "true"}}

    cover = musicbrainz_tagger.Scraper().parse_cover_url(release)

    out = capsys.readouterr().out
    assert cover is None
    assert "Cover Art Archive request failed (NetworkError): no cover." in out
    assert len(server.requests) == 2
