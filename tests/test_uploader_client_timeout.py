"""A torrent client that accepts a connection and never answers can't hang an upload.

ruTorrent's are real sockets on localhost: xmlrpc.client has no timeout of its own, so it's the connection that is
bounded. The silent server closes on its own after a few seconds, so a client that waits for it can't hang the suite.
"""

import socket
import threading
import time
from collections.abc import Iterator
from xmlrpc.server import SimpleXMLRPCServer

import pytest

from salmon.uploader import torrent_client as tc
from salmon.uploader.torrent_client import RuTorrentClient, TransmissionClient

STAYS_SILENT_FOR = 4.0


@pytest.fixture
def silent_port() -> Iterator[int]:
    """A port on localhost that accepts connections and answers nothing, for STAYS_SILENT_FOR seconds."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        accepted: list[socket.socket] = []

        def accept() -> None:
            listener.settimeout(STAYS_SILENT_FOR)
            while True:
                try:
                    accepted.append(listener.accept()[0])
                except OSError:  # Nothing more to accept: stop being silent, so that a client waiting returns.
                    for connection in accepted:
                        connection.close()
                    listener.close()  # xmlrpc.client sends again once on a dropped connection.
                    return

        threading.Thread(target=accept, daemon=True).start()
        yield listener.getsockname()[1]


@pytest.fixture
def short_timeout(monkeypatch) -> None:
    monkeypatch.setattr(tc, "REQUEST_TIMEOUT", 0.2, raising=False)


@pytest.fixture
def messages(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(tc.click, "secho", lambda message, **_kwargs: said.append(str(message)))
    return said


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_silent_rutorrent_fails_the_login_in_time(silent_port, short_timeout, messages, scheme: str) -> None:
    started = time.monotonic()

    client = RuTorrentClient(url=f"{scheme}://127.0.0.1:{silent_port}/RPC2")

    assert time.monotonic() - started < STAYS_SILENT_FOR / 2
    assert client.client is None
    assert any("Connect to ruTorrent failed" in message for message in messages)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_rutorrent_that_stops_answering_fails_the_add_in_time(
    monkeypatch, silent_port, short_timeout, messages, scheme: str
) -> None:
    url = f"{scheme}://127.0.0.1:{silent_port}/RPC2"
    monkeypatch.setattr(
        RuTorrentClient, "login", lambda self: tc.xmlrpc.client.Server(url, transport=tc._rutorrent_transport(url))
    )
    client = RuTorrentClient(url=url)
    started = time.monotonic()

    added = client.add_to_downloader("/downloads", b"torrent", False, None)

    assert added is False
    assert time.monotonic() - started < STAYS_SILENT_FOR / 2
    assert any("Failed to add torrent" in message for message in messages)


def _answering_rutorrent() -> tuple[SimpleXMLRPCServer, list[tuple]]:
    server = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)
    loaded: list[tuple] = []
    server.register_function(lambda: "0.9.8", "system.client_version")
    server.register_function(lambda *args: loaded.append(args) or 0, "load.raw_start_verbose")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, loaded


@pytest.mark.parametrize(("url", "safe"), [("https://host/RPC2", True), ("http://host/RPC2", False)])
def test_an_https_rutorrent_is_reached_over_tls(url: str, safe: bool) -> None:
    assert isinstance(tc._rutorrent_transport(url), tc.xmlrpc.client.SafeTransport) is safe


def test_as_before_a_rutorrent_that_answers_is_used(messages) -> None:
    server, loaded = _answering_rutorrent()
    try:
        client = RuTorrentClient(url=f"http://127.0.0.1:{server.server_address[1]}/RPC2")

        added = client.add_to_downloader("/downloads", b"torrent", False, "label")
    finally:
        server.shutdown()
        server.server_close()

    assert added is True
    assert loaded[0][0] == ""
    assert loaded[0][2:] == ("print=d.hash=", "d.directory.set=/downloads", "d.custom1.set=label")


def test_transmission_is_given_the_same_timeout(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(tc.transmission_rpc, "Client", lambda **kwargs: seen.update(kwargs) or "session")

    TransmissionClient(host="localhost", port=9091)

    assert seen["timeout"] == tc.REQUEST_TIMEOUT == 60
