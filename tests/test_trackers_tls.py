"""A TLS certificate that does not verify is reported as such, and not retried (#581).

Each fake tracker is a local TLS server counting the connections it accepts, before their handshake,
with a certificate made by openssl for the test.
"""

import asyncio
import shutil
import socket
import ssl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from fake_proxy import FakeProxy
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import salmon.run
import salmon.trackers
from salmon import cfg
from salmon.commands import checkconf
from salmon.config.validations import ProxyCfg, ProxyServicesCfg
from salmon.errors import RequestError, TLSCertificateError, UnknownOutcomeError
from salmon.trackers.base import BaseGazelleApi, RetryableError
from salmon.uploader.dupe_checker import fetch_existing_group_candidates_in_background

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so the tests count attempts, not seconds."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Certificate:
    cert: Path
    key: Path


@dataclass(frozen=True)
class Certificates:
    # Valid for 127.0.0.1, but signed by nothing the system trusts.
    self_signed: Certificate
    # Valid for 127.0.0.1 in 2020 only.
    expired: Certificate


_CA_CONFIG = """[ca]
default_ca = test
[test]
database = index.txt
serial = serial
new_certs_dir = .
default_md = sha256
policy = any_name
copy_extensions = copy
unique_subject = no
[any_name]
commonName = supplied
"""


def _openssl(cwd: Path, *args: str) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _new_key_args(name: str) -> list[str]:
    return [
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-nodes",
        "-keyout",
        f"{name}.key",
        "-subj",
        "/CN=127.0.0.1",
        "-addext",
        "subjectAltName=IP:127.0.0.1",
    ]


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Certificates:
    d = tmp_path_factory.mktemp("certs")
    _openssl(d, "req", "-x509", *_new_key_args("self_signed"), "-out", "self_signed.pem", "-days", "30")
    # openssl req cannot set dates in the past before OpenSSL 3.4; openssl ca can.
    (d / "ca.cnf").write_text(_CA_CONFIG)
    (d / "index.txt").write_text("")
    (d / "serial").write_text("01\n")
    _openssl(d, "req", "-new", *_new_key_args("expired"), "-out", "expired.csr")
    _openssl(
        d,
        "ca",
        "-batch",
        "-config",
        "ca.cnf",
        "-selfsign",
        "-keyfile",
        "expired.key",
        "-in",
        "expired.csr",
        "-out",
        "expired.pem",
        "-startdate",
        "20200101000000Z",
        "-enddate",
        "20200102000000Z",
        "-notext",
    )
    return Certificates(
        self_signed=Certificate(d / "self_signed.pem", d / "self_signed.key"),
        expired=Certificate(d / "expired.pem", d / "expired.key"),
    )


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        trust: ssl.SSLContext | None = None,
        connector: type[aiohttp.TCPConnector] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        super().__init__()
        self._trust = trust
        self._connector = connector
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True

    def _new_session(self, connections: int) -> aiohttp.ClientSession:
        if self._trust is None and self._connector is None:
            return super()._new_session(connections)
        connector = (self._connector or aiohttp.TCPConnector)(limit=connections, ssl=self._trust or True)
        return aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar())


class Server:
    """A local server counting the connections it accepts."""

    def __init__(self) -> None:
        self.accepted = 0
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"https://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def serve_tls(self, certificate: Certificate) -> "Server":
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certificate.cert, certificate.key)

        def connection() -> asyncio.Protocol:
            # Called on accepting the connection, before the handshake.
            self.accepted += 1
            return asyncio.Protocol()

        self._server = await asyncio.get_running_loop().create_server(connection, "127.0.0.1", 0, ssl=context)
        return self

    async def serve_tls_alert(self, alert: int) -> "Server":
        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self.accepted += 1
            await reader.read(1024)  # The ClientHello
            # A fatal TLS alert, as a TLS terminator sends when it gives up on a handshake.
            writer.write(bytes([0x15, 0x03, 0x03, 0x00, 0x02, 0x02, alert]))
            await writer.drain()
            writer.close()

        self._server = await asyncio.start_server(answer, "127.0.0.1", 0)
        return self

    async def close(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


async def _request(api: FakeApi, method: str = "GET") -> None:
    data = {"file": "x"} if method == "POST" else None
    await api._request(method, api.base_url + "/ajax.php", params={"action": "browse"}, data=data, timeout_secs=5)


async def _certificate_failure(certificate: Certificate, method: str, trust: bool) -> tuple[TLSCertificateError, int]:
    server = await Server().serve_tls(certificate)
    api = FakeApi(server.url, trust=ssl.create_default_context(cafile=certificate.cert) if trust else None)
    try:
        with pytest.raises(TLSCertificateError) as caught:
            await _request(api, method)
        return caught.value, server.accepted
    finally:
        await api.close()
        await server.close()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_untrusted_certificate_connects_once(certificates: Certificates, method: str) -> None:
    err, accepted = anyio.run(_certificate_failure, certificates.self_signed, method, False)
    assert accepted == 1
    assert str(err) == "TLS certificate verification failed for 127.0.0.1: self-signed certificate"
    assert (err.host, err.reason, err.tracker) == ("127.0.0.1", "self-signed certificate", "RED")
    # Every caller handles a RequestError, and a POST that failed in the handshake was not acted on.
    assert isinstance(err, RequestError)
    assert not isinstance(err, RetryableError | UnknownOutcomeError)


def test_expired_certificate_connects_once(certificates: Certificates) -> None:
    # Trusted, so what fails is the date, as with a stale CA bundle that still has an expired root.
    err, accepted = anyio.run(_certificate_failure, certificates.expired, "GET", True)
    assert accepted == 1
    assert str(err) == "TLS certificate verification failed for 127.0.0.1: certificate has expired"


async def _certificate_failure_through_a_proxy(certificate: Certificate) -> tuple[TLSCertificateError, int, int]:
    server = await Server().serve_tls(certificate)
    proxy = FakeProxy("socks5")
    proxy_url = await proxy.start()
    api = FakeApi(server.url)
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            # Only RED's requests go through it; any other service would hit a port nothing listens on.
            others = dict.fromkeys(ProxyServicesCfg.__struct_fields__, "socks5://127.0.0.1:9")
            monkeypatch.setattr(cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(**{**others, "red": proxy_url})))
            with pytest.raises(TLSCertificateError) as caught:
                await _request(api)
        return caught.value, proxy.connections, server.accepted
    finally:
        await api.close()
        await proxy.stop()
        await server.close()


def test_certificate_failure_through_a_proxy_connects_once(certificates: Certificates) -> None:
    err, through_proxy, accepted = anyio.run(_certificate_failure_through_a_proxy, certificates.self_signed)
    assert (through_proxy, accepted) == (1, 1)
    assert str(err) == "TLS certificate verification failed for 127.0.0.1: self-signed certificate"


async def _dupe_check_on_a_fresh_client(certificate: Certificate) -> int:
    server = await Server().serve_tls(certificate)
    api = FakeApi(server.url)
    # As at the start of an upload: the gathered searches wait on one authentication.
    api._authenticated = False
    try:
        # Raised as it is, not in an exception group: salmon's main catches it.
        with pytest.raises(TLSCertificateError):
            async with fetch_existing_group_candidates_in_background(
                api, ["artist album", "artist album 2024", "artist album deluxe"], "album"
            ) as fetch:
                assert fetch is not None
                await fetch.result()
        return server.accepted
    finally:
        await api.close()
        await server.close()


def test_dupe_check_at_the_start_of_an_upload_connects_once(certificates: Certificates) -> None:
    assert anyio.run(_dupe_check_on_a_fresh_client, certificates.self_signed) == 1


class _CertificateFailureOnSecondConnection(aiohttp.TCPConnector):
    """Fails the second connection on its certificate, as aiohttp would."""

    connections = 0

    async def _wrap_create_connection(self, *args: Any, **kwargs: Any) -> Any:
        type(self).connections += 1
        if type(self).connections == 2:
            failure = ssl.SSLCertVerificationError(1, "certificate verify failed: certificate has expired")
            raise aiohttp.ClientConnectorCertificateError(kwargs["req"].connection_key, failure)
        return await super()._wrap_create_connection(*args, **kwargs)


async def _certificate_failure_on_the_redirect_of_a_post() -> list[str]:
    hits = []

    async def upload(request: web.Request) -> web.Response:
        hits.append(request.path)
        # Closed, so the redirect goes out on a new connection.
        return web.Response(status=303, headers={"Location": "/torrents.php?id=1", "Connection": "close"})

    async def torrents(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.Response(text="torrent page")

    app = web.Application()
    app.router.add_post("/upload.php", upload)
    app.router.add_get("/torrents.php", torrents)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    _CertificateFailureOnSecondConnection.connections = 0
    api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}", connector=_CertificateFailureOnSecondConnection)
    try:
        # The tracker acted on the upload when it redirected it: its outcome is unknown, as before.
        with pytest.raises(UnknownOutcomeError):
            await api._request("POST", api.base_url + "/upload.php", data={"file": "x"})
        return hits
    finally:
        await api.close()
        await runner.cleanup()


def test_certificate_failure_after_a_post_was_redirected_is_an_unknown_outcome() -> None:
    assert anyio.run(_certificate_failure_on_the_redirect_of_a_post) == ["/upload.php"]


async def _refused() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    api = FakeApi(f"https://127.0.0.1:{port}")
    try:
        with pytest.raises(RetryableError):
            await _request(api)
        return BaseGazelleApi._send.statistics["attempt_number"]  # type: ignore[attr-defined]
    finally:
        await api.close()


def test_refused_connection_is_still_retried() -> None:
    assert anyio.run(_refused) == 5


class _ConnectTimesOut(aiohttp.TCPConnector):
    async def _wrap_create_connection(self, *args: Any, **kwargs: Any) -> Any:
        # What sock_connect running out raises in there: aiohttp makes it a ConnectionTimeoutError.
        raise TimeoutError


async def _connect_timeout() -> int:
    api = FakeApi("https://127.0.0.1:9", connector=_ConnectTimesOut)
    try:
        with pytest.raises(RetryableError, match="Connection timeout"):
            await _request(api)
        return BaseGazelleApi._send.statistics["attempt_number"]  # type: ignore[attr-defined]
    finally:
        await api.close()


def test_connect_timeout_is_still_retried() -> None:
    assert anyio.run(_connect_timeout) == 5


async def _tls_alert(alert: int) -> int:
    server = await Server().serve_tls_alert(alert)
    api = FakeApi(server.url)
    try:
        with pytest.raises(RetryableError):
            await _request(api)
        return server.accepted
    finally:
        await api.close()
        await server.close()


def test_handshake_failing_on_an_internal_error_alert_is_still_retried() -> None:
    # Not a certificate verdict: a TLS terminator under load answers this, and may not on the next attempt.
    assert anyio.run(_tls_alert, 80) == 5


async def _checkconf(certificate: Certificate, monkeypatch: pytest.MonkeyPatch) -> int:
    server = await Server().serve_tls(certificate)
    made: list[FakeApi] = []

    def tracker_class(_tracker: str):
        def make() -> FakeApi:
            made.append(FakeApi(server.url, api_key="an-api-key"))
            return made[-1]

        return make

    monkeypatch.setattr(salmon.trackers, "get_class", tracker_class)
    try:
        assert checkconf.callback is not None
        await checkconf.callback(tracker="RED", metadata=False, seedbox=False, reset=False)
        return server.accepted
    finally:
        for api in made:
            await api.close()
        await server.close()


def test_checkconf_reports_a_certificate_failure_not_an_authentication_failure(
    certificates: Certificates, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    stale_bundle = tmp_path / "old-ca-bundle.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(stale_bundle))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    accepted = anyio.run(_checkconf, certificates.self_signed, monkeypatch)

    out = capsys.readouterr().out
    assert "✖ TLS certificate verification failed for 127.0.0.1: self-signed certificate" in out
    assert "your session cookie and API key are not the cause" in out
    assert f"SSL_CERT_FILE: {stale_bundle}" in out
    assert "SSL_CERT_DIR: not set" in out
    assert f"CA file: none, nothing at {stale_bundle}" in out
    assert "CA directory: " in out
    assert "system clock" in out
    assert "✖ Error testing RED (TLS certificate)" in out
    assert "authentication failed" not in out.lower()
    assert "cookie check failed" not in out.lower()
    # The API key check would go to the same host: it is not sent.
    assert accepted == 1


def test_main_points_at_checkconf_when_a_command_ends_on_a_certificate_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def command(**_kwargs: object) -> None:
        raise TLSCertificateError("tracker.example", "certificate has expired", "OPS")

    monkeypatch.setattr(salmon.run, "cleanup_tmp_dir", lambda: None)
    monkeypatch.setattr(salmon.run, "show_release_notification", lambda: None)
    monkeypatch.setattr(salmon.run, "commandgroup", command)

    salmon.run.main()

    out = capsys.readouterr().out
    assert "There was an error: TLS certificate verification failed for tracker.example: certificate has expired" in out
    assert "salmon checkconf -t OPS" in out
