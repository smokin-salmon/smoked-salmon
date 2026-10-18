"""What salmon prints of a torrent client URL: its scheme, host and port, never its path, query or fragment."""

import pytest
import qbittorrentapi
import urllib3.util.connection

from salmon.common.redaction import redact_client_url, url_secrets
from salmon.uploader import torrent_client as tc
from salmon.uploader.torrent_client import (
    DelugeClient,
    QBittorrentClient,
    RuTorrentClient,
    TorrentClientGenerator,
    TransmissionClient,
)


@pytest.fixture
def no_login(monkeypatch):
    """Replace every client class's login with a stub so no network happens."""
    for cls in (QBittorrentClient, TransmissionClient, DelugeClient, RuTorrentClient):
        monkeypatch.setattr(cls, "login", lambda self: f"session-{type(self).__name__}")


@pytest.fixture
def click_messages(monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(tc.click, "secho", lambda message, **kwargs: messages.append(str(message)))
    return messages


def test_parse_libtc_url_does_not_log_credentials(no_login, click_messages) -> None:
    TorrentClientGenerator.parse_libtc_url("qbittorrent+http://admin:hunter2@127.0.0.1:8080")

    joined = "\n".join(click_messages)
    assert "hunter2" not in joined
    assert "admin:" not in joined
    assert "127.0.0.1:8080" in joined


# A proxy's key can take any shape, not only a token's.
PROXY_KEYS = ["PROXYKEY0123UNIQUE", "PROXYKEYONLYLETTERS", "9876543210987654", "correct-horse-battery-staple"]


@pytest.mark.parametrize("key", PROXY_KEYS)
def test_parse_libtc_url_does_not_log_a_proxy_key_in_the_path(no_login, click_messages, key) -> None:
    client = TorrentClientGenerator.parse_libtc_url(f"qbittorrent+http://proxy.local:7476/proxy/{key}")

    joined = "\n".join(click_messages)
    assert key not in joined
    assert "proxy.local:7476" in joined
    assert client.url == f"http://proxy.local:7476/proxy/{key}"


@pytest.mark.parametrize("rest", ["/proxy?key=PROXYKEY0123UNIQUE", "/proxy#PROXYKEY0123UNIQUE"])
def test_parse_libtc_url_does_not_log_a_key_in_the_query_or_fragment(no_login, click_messages, rest) -> None:
    TorrentClientGenerator.parse_libtc_url(f"qbittorrent+http://proxy.local:7476{rest}")

    joined = "\n".join(click_messages)
    assert "PROXYKEY0123UNIQUE" not in joined
    assert "proxy.local:7476" in joined


@pytest.mark.parametrize(
    ("key", "echoed"),
    [
        *((key, key) for key in PROXY_KEYS),
        # The HTTP client re-escapes the path before its error repeats it.
        ("PROXY%2fKEY", "PROXY%2FKEY"),
        ("PROXY%41%2fKEY", "PROXYA%2FKEY"),
        # A bad escape makes requests escape every "%".
        ("KEY%ZZONLYLETTERS", "KEY%25ZZONLYLETTERS"),
        ("KEYéONLYLETTERS", "KEY%C3%A9ONLYLETTERS"),
    ],
)
def test_a_login_error_repeating_the_url_hides_its_proxy_key(monkeypatch, click_messages, key, echoed) -> None:
    class FakeQbt:
        def __init__(self, host, username, password):
            raise qbittorrentapi.APIConnectionError(f"Max retries exceeded with url: /proxy/{echoed}/api/v2/auth/login")

    monkeypatch.setattr(tc.qbittorrentapi, "Client", FakeQbt)
    client = QBittorrentClient(url=f"http://proxy.local:7476/proxy/{key}")

    assert client.client is None
    assert any("Max retries exceeded" in m for m in click_messages)
    assert echoed not in "\n".join(click_messages)


@pytest.mark.parametrize("key", ["PROXYKEY0123UNIQUE", "PROXYKEYONLYLETTERS", "PROXY%2fKEY", "PROXY%ZZKEY"])
def test_a_real_qbittorrent_login_error_hides_the_proxy_key(monkeypatch, click_messages, key) -> None:
    def refuse(*_args, **_kwargs):
        raise ConnectionRefusedError(61, "Connection refused")

    # Only the socket is stubbed: the client library and its HTTP stack word the error, and nothing is sent.
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(urllib3.util.connection, "create_connection", refuse)
    client = TorrentClientGenerator.parse_libtc_url(f"qbittorrent+http://proxy.local:7476/proxy/{key}")

    joined = "\n".join(click_messages)
    assert client.client is None
    assert "Max retries exceeded" in joined
    assert "KEY" not in joined.replace("[REDACTED]", "")


def test_a_client_url_that_does_not_parse_is_never_quoted(no_login) -> None:
    with pytest.raises(ValueError) as refused:
        TorrentClientGenerator.parse_libtc_url("qbittorrent+http://admin:PASS／UNIQUE@proxy.local:7476")

    assert "PASS" not in str(refused.value)
    assert refused.value.__suppress_context__


@pytest.mark.parametrize(
    ("url", "path", "words"),
    [
        (
            "rutorrent+http://rutorrent.example:9380/plugins/rpc/rpc.php",
            "/plugins/rpc/rpc.php",
            {"plugins", "rpc", "rpc.php"},
        ),
        ("rutorrent+http://rutorrent.example/RPC2", "/RPC2", {"RPC2"}),
        ("qbittorrent+http://proxy.local:7476/proxy/KEY", "/proxy/KEY", {"proxy"}),
    ],
)
def test_url_secrets_leave_a_client_endpoints_own_words_alone(url, path, words) -> None:
    secrets = url_secrets(url)

    assert path in secrets
    assert words.isdisjoint(secrets)


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("qbittorrent+http://admin:hunter2@127.0.0.1:8080", "qbittorrent+http://[REDACTED]@127.0.0.1:8080"),
        ("qbittorrent+http://proxy.local:7476/proxy/KEY", "qbittorrent+http://proxy.local:7476/[REDACTED]"),
        (
            "qbittorrent+http://proxy.local:7476/?key=KEY#KEY",
            "qbittorrent+http://proxy.local:7476?[REDACTED]#[REDACTED]",
        ),
        ("qbittorrent+http://[::1]:7476/", "qbittorrent+http://[::1]:7476"),
        ("transmission+http://127.0.0.1:9091", "transmission+http://127.0.0.1:9091"),
        ("qbittorrent+http://[proxy.local/proxy/KEY", "[REDACTED]"),
    ],
)
def test_a_client_url_shows_only_its_scheme_host_and_port(url, shown) -> None:
    assert redact_client_url(url) == shown


def test_url_secrets_take_no_root_path() -> None:
    # "/" would mask every slash an error holds.
    assert url_secrets("qbittorrent+http://127.0.0.1:8080/") == []
