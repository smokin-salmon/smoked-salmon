"""``salmon web``: the login, the request rules and the front-end build (#629, ADR 0004 section 3).

Every test talks to an aiohttp test server on 127.0.0.1; nothing here reaches a tracker.
"""

import re
import signal
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

import anyio
import asyncclick as click
import msgspec
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from salmon import cfg
from salmon.config import _parse_config
from salmon.webui import auth, server
from salmon.webui.auth import COOKIE_NAME, ENV_VAR, resolve_token
from salmon.webui.security import host_allowed, origin_allowed, split_host

TOKEN = "correct-horse-battery-staple-0123456789"
JSON = {"Content-Type": "application/json"}
_DEFAULT_CONFIG = Path(__file__).parent.parent / "src" / "salmon" / "data" / "config.default.toml"


def _with_client(
    test: Callable[[TestClient], Awaitable[None]],
    bind_host: str = "127.0.0.1",
    allowed_hosts: tuple[str, ...] = (),
    dev: bool = False,
) -> None:
    async def main() -> None:
        app = server.create_app(TOKEN, bind_host, list(allowed_hosts), dev=dev)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            await test(client)

    anyio.run(main)


async def _login(client: TestClient, token: str = TOKEN, headers: dict[str, str] | None = None):
    return await client.post("/api/login", json={"token": token}, headers=headers)


# --- The auth matrix ---------------------------------------------------------


def test_api_refuses_a_request_without_a_token() -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/auth")
        assert response.status == 401
        assert await response.json() == {"detail": "Authentication required."}

    _with_client(test)


@pytest.mark.parametrize(
    "authorization",
    [f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", f"Basic {TOKEN}", TOKEN, "Bearer ", "Bearer éé"],
)
def test_api_refuses_a_wrong_bearer_token(authorization: str) -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/auth", headers={"Authorization": authorization})
        assert response.status == 401

    _with_client(test)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
def test_api_takes_the_token_as_bearer(scheme: str) -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/auth", headers={"Authorization": f"{scheme} {TOKEN}"})
        assert response.status == 200
        assert await response.json() == {"authenticated": True}

    _with_client(test)


def test_login_with_a_wrong_token_sets_no_cookie() -> None:
    async def test(client: TestClient) -> None:
        response = await _login(client, "wrong" * 10)
        assert response.status == 401
        assert "Set-Cookie" not in response.headers
        assert (await client.get("/api/auth")).status == 401

    _with_client(test)


def test_login_sets_a_strict_http_only_cookie_that_logs_in() -> None:
    async def test(client: TestClient) -> None:
        response = await _login(client)
        assert response.status == 200
        cookie = response.headers["Set-Cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        # Plain HTTP: a Secure cookie would never come back.
        assert "Secure" not in cookie
        # The cookie is a session, not the token.
        assert TOKEN not in cookie

        assert (await client.get("/api/auth")).status == 200

    _with_client(test)


@pytest.mark.parametrize("proto", ["https", "HTTPS"])
def test_login_cookie_is_secure_behind_a_tls_proxy(proto: str) -> None:
    async def test(client: TestClient) -> None:
        response = await _login(client, headers={"X-Forwarded-Proto": proto})
        assert response.status == 200
        assert "Secure" in response.headers["Set-Cookie"]

    _with_client(test)


def test_logout_ends_the_session() -> None:
    async def test(client: TestClient) -> None:
        session_id = (await _login(client)).cookies[COOKIE_NAME].value

        response = await client.post("/api/logout", json={})
        assert response.status == 200
        assert (await client.get("/api/auth")).status == 401
        # The old cookie, sent again, is no longer a session.
        client.session.cookie_jar.clear()
        response = await client.get("/api/auth", headers={"Cookie": f"{COOKIE_NAME}={session_id}"})
        assert response.status == 401

    _with_client(test)


def test_logout_needs_a_login() -> None:
    async def test(client: TestClient) -> None:
        assert (await client.post("/api/logout", json={})).status == 401

    _with_client(test)


def test_a_made_up_session_cookie_is_refused() -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/auth", headers={"Cookie": f"{COOKIE_NAME}={TOKEN}"})
        assert response.status == 401

    _with_client(test)


def test_health_and_login_need_no_token() -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/health")
        assert response.status == 200
        assert await response.json() == {"status": "ok"}
        assert (await _login(client, "x")).status == 401  # Reached the handler, which refused the token.

    _with_client(test)


def test_unknown_api_paths_need_a_token_too() -> None:
    async def test(client: TestClient) -> None:
        assert (await client.get("/api/nothing")).status == 401
        assert (await client.get("/api")).status == 401
        response = await client.get("/api/nothing", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status == 404
        assert await response.json() == {"detail": "Not found."}

    _with_client(test)


def test_login_refuses_a_body_without_a_token() -> None:
    async def test(client: TestClient) -> None:
        for body in (b"{}", b"[]", b"not json", b'{"token": 1}'):
            response = await client.post("/api/login", data=body, headers=JSON)
            assert response.status == 400, body

    _with_client(test)


def test_sessions_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "MAX_SESSIONS", 3)
    gate = auth.Auth(TOKEN)
    first = gate.open_session()
    later = [gate.open_session() for _ in range(3)]
    assert not gate.session_valid(first)
    assert all(gate.session_valid(session) for session in later)


def test_sessions_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    gate = auth.Auth(TOKEN)
    session = gate.open_session()
    now[0] += auth.SESSION_MAX_AGE - 1
    assert gate.session_valid(session)
    now[0] += 2
    assert not gate.session_valid(session)


# --- JSON and Origin on every request but GET and HEAD -------------------------


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"]
)
def test_a_post_that_is_not_json_is_refused(content_type: str | None) -> None:
    async def test(client: TestClient) -> None:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if content_type:
            headers["Content-Type"] = content_type
        # Even with the token: the rule is not about who sends it.
        for path in ("/api/logout", "/api/login"):
            response = await client.post(path, data=b'{"token": "' + TOKEN.encode() + b'"}', headers=headers)
            assert response.status == 415, path

    _with_client(test)


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:1", "http://localhost:5173"])
def test_a_post_from_another_origin_is_refused(origin: str) -> None:
    async def test(client: TestClient) -> None:
        response = await _login(client, headers={"Origin": origin})
        assert response.status == 403
        assert "Set-Cookie" not in response.headers
        assert "Access-Control-Allow-Origin" not in response.headers

    _with_client(test)


def test_a_post_from_the_servers_own_origin_or_without_origin_passes() -> None:
    async def test(client: TestClient) -> None:
        own = f"http://127.0.0.1:{client.port}"
        assert (await _login(client, headers={"Origin": own})).status == 200
        assert (await _login(client)).status == 200

    _with_client(test)


def test_dev_accepts_the_vite_dev_server_with_cors() -> None:
    async def test(client: TestClient) -> None:
        origin = "http://localhost:5173"
        response = await client.options(
            "/api/login", headers={"Origin": origin, "Access-Control-Request-Method": "POST"}
        )
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == origin
        assert response.headers["Access-Control-Allow-Credentials"] == "true"

        response = await _login(client, headers={"Origin": origin})
        assert response.status == 200
        assert response.headers["Access-Control-Allow-Origin"] == origin

        response = await _login(client, headers={"Origin": "https://evil.example"})
        assert response.status == 403
        assert "Access-Control-Allow-Origin" not in response.headers

    _with_client(test, dev=True)


def test_without_dev_there_is_no_cors() -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert response.status == 200
        assert "Access-Control-Allow-Origin" not in response.headers
        response = await client.options(
            "/api/login", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}
        )
        assert response.status != 204
        assert "Access-Control-Allow-Origin" not in response.headers

    _with_client(test)


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("http://127.0.0.1:55155", "127.0.0.1:55155", True),
        ("http://LOCALHOST:55155", "localhost:55155", True),
        ("http://[::1]:55155", "[::1]:55155", True),
        # Behind a TLS reverse proxy that passes Host through.
        ("https://salmon.example", "salmon.example", True),
        ("https://salmon.example", "salmon.example:443", True),
        ("https://salmon.example:8443", "salmon.example:8443", True),
        ("http://salmon.example", "salmon.example:80", True),
        ("https://salmon.example", "salmon.example:80", False),
        ("http://127.0.0.1:55155", "127.0.0.1:55156", False),
        ("http://127.0.0.1", "127.0.0.1:55155", False),
        ("http://evil.example:55155", "127.0.0.1:55155", False),
        ("http://127.0.0.1.evil.example:55155", "127.0.0.1:55155", False),
        ("null", "127.0.0.1:55155", False),
        ("file://", "127.0.0.1:55155", False),
        ("http://127.0.0.1:99999", "127.0.0.1:55155", False),
        ("http://127.0.0.1:55155", None, False),
    ],
)
def test_origin_rule(origin: str, host: str | None, allowed: bool) -> None:
    assert origin_allowed(origin, host) is allowed


# --- The Host check (DNS rebinding) ------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:55155", "127.0.0.1.evil.example", "", "[::1"])
def test_an_unknown_host_is_refused_before_anything_else(host: str) -> None:
    async def test(client: TestClient) -> None:
        for response in (
            await client.get("/", headers={"Host": host}),
            await client.get("/api/health", headers={"Host": host}),
            await client.get("/api/auth", headers={"Host": host, "Authorization": f"Bearer {TOKEN}"}),
            await _login(client, headers={"Host": host}),
        ):
            assert response.status == 403
            assert "allowed_hosts" in (await response.json())["detail"]

    _with_client(test)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:55155", "127.0.0.1", "[::1]:80"])
def test_loopback_hosts_pass(host: str) -> None:
    async def test(client: TestClient) -> None:
        assert (await client.get("/api/health", headers={"Host": host})).status == 200

    _with_client(test)


def test_the_bind_address_and_allowed_hosts_pass() -> None:
    async def test(client: TestClient) -> None:
        for host in ("192.168.1.20:55155", "seedbox.lan", "Seedbox.LAN:8080", "other.lan"):
            assert (await client.get("/api/health", headers={"Host": host})).status == 200, host
        assert (await client.get("/api/health", headers={"Host": "elsewhere.lan"})).status == 403

    _with_client(test, bind_host="192.168.1.20", allowed_hosts=("seedbox.lan", "other.lan:55155"))


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "::"])
def test_a_wildcard_bind_is_no_host_name(bind_host: str) -> None:
    assert not host_allowed("0.0.0.0:55155", bind_host, [])
    assert not host_allowed("[::]:55155", bind_host, [])
    assert host_allowed("localhost:55155", bind_host, [])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.org", ("example.org", None)),
        ("Example.org:80", ("example.org", 80)),
        ("[::1]:55155", ("::1", 55155)),
        ("[::1]", ("::1", None)),
    ],
)
def test_split_host(value: str, expected: tuple[str, int | None]) -> None:
    assert split_host(value) == expected


@pytest.mark.parametrize("value", ["", ":80", "[::1", "[::1]x", "host:", "host:http", "host:0", "host:70000"])
def test_split_host_refuses_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        split_host(value)


# --- The front-end build -----------------------------------------------------

STATIC = server.STATIC_DIR


def test_committed_build_is_served_at_the_root_and_for_unknown_paths() -> None:
    index = (STATIC / "index.html").read_bytes()

    async def test(client: TestClient) -> None:
        for path in ("/", "/index.html", "/jobs", "/some/app/route", "/favicon.ico"):
            response = await client.get(path)
            assert response.status == 200, path
            assert await response.read() == index, path
            # It names the current assets: never from a cache without asking.
            assert response.headers["Cache-Control"] == "no-cache"
            assert response.headers["Content-Type"].startswith("text/html")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"

    _with_client(test)


@pytest.mark.parametrize("path", ["/../server.py", "/%2e%2e/server.py", "/..%2fserver.py", "/assets/..%2f..%2fauth.py"])
def test_no_file_outside_the_build_is_served(path: str) -> None:
    index = (STATIC / "index.html").read_bytes()

    async def test(client: TestClient) -> None:
        # A raw request: the client would clean the path up before sending it.
        port = client.port
        assert port is not None
        stream = await anyio.connect_tcp("127.0.0.1", port)
        async with stream:
            await stream.send(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
            answer = b""
            with anyio.fail_after(5):
                try:
                    while chunk := await stream.receive():
                        answer += chunk
                except anyio.EndOfStream:
                    pass
        head, _, body = answer.partition(b"\r\n\r\n")
        assert b"import" not in body
        assert head.startswith(b"HTTP/1.1 200") or head.startswith(b"HTTP/1.1 404"), head
        if head.startswith(b"HTTP/1.1 200"):
            assert body == index

    _with_client(test)


def test_committed_build_assets_are_served() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:src|href)="/([^"]+)"', index)
    # The script, the stylesheet and the icons.
    assert any(path.endswith(".js") for path in referenced)
    assert any(path.endswith(".css") for path in referenced)

    async def test(client: TestClient) -> None:
        for path in referenced:
            response = await client.get(f"/{path}")
            assert response.status == 200, path
            assert await response.read() == (STATIC / path).read_bytes(), path
            if path.startswith("assets/"):
                assert "immutable" in response.headers["Cache-Control"]
        assert (await client.get("/assets/missing.js")).status == 404

    _with_client(test)


# --- Starting the server ------------------------------------------------------


@pytest.fixture
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(cfg.web, "token", None)


@pytest.mark.usefixtures("no_token")
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "seedbox.lan"])
def test_a_non_loopback_bind_without_a_token_refuses_to_start(host: str) -> None:
    with pytest.raises(click.ClickException, match="will not listen on .* without a token"):
        resolve_token(host, None)


@pytest.mark.usefixtures("no_token")
def test_the_command_refuses_to_start_on_all_interfaces_without_a_token() -> None:
    from asyncclick.testing import CliRunner

    from salmon.webui import web as web_command

    result = anyio.run(partial(CliRunner().invoke, web_command, ["--host", "0.0.0.0", "--port", "55155"]))
    assert result.exit_code == 1
    assert "will not listen on 0.0.0.0 without a token" in result.output


@pytest.mark.usefixtures("no_token")
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_without_a_token_gets_a_new_one_each_start(host: str) -> None:
    first, generated = resolve_token(host, None)
    second, _ = resolve_token(host, None)
    assert generated
    assert len(first) >= 32
    assert first != second


def test_the_environment_beats_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "e" * 32)
    assert resolve_token("0.0.0.0", "c" * 32) == ("e" * 32, False)
    monkeypatch.delenv(ENV_VAR)
    assert resolve_token("0.0.0.0", "c" * 32) == ("c" * 32, False)


def test_a_short_environment_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "e" * 31)
    with pytest.raises(click.ClickException, match="at least 32"):
        resolve_token("127.0.0.1", None)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.usefixtures("no_token")
def test_a_loopback_start_prints_a_login_link_whose_token_works(capsys: pytest.CaptureFixture[str]) -> None:
    port = _free_port()

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(partial(server.serve, "127.0.0.1", port))
            out = ""
            with anyio.fail_after(5):
                while "#token=" not in out:
                    await anyio.sleep(0.02)
                    out += capsys.readouterr().out
            match = re.search(rf"http://127\.0\.0\.1:{port}/#token=(\S+)", out)
            assert match, out
            assert "in clear" not in out
            async with ClientSession() as session:
                url = f"http://127.0.0.1:{port}"
                async with session.get(f"{url}/api/auth") as response:
                    assert response.status == 401
                async with session.get(f"{url}/api/auth", headers={"Authorization": f"Bearer {match[1]}"}) as r:
                    assert r.status == 200
            tg.cancel_scope.cancel()

    anyio.run(main)


def test_a_non_loopback_start_warns_about_plain_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV_VAR, TOKEN)
    sites: list[tuple[str, int]] = []

    class FakeSite:
        def __init__(self, _runner: web.AppRunner, host: str, port: int) -> None:
            sites.append((host, port))

        async def start(self) -> None:
            pass

    monkeypatch.setattr(server.web, "TCPSite", FakeSite)

    async def main() -> None:
        with anyio.move_on_after(0.2):
            await server.serve("0.0.0.0", 55155)

    anyio.run(main)
    out = capsys.readouterr().out
    assert sites == [("0.0.0.0", 55155)]
    assert "in clear" in out
    assert "TLS reverse proxy, an SSH tunnel or a VPN" in out
    assert TOKEN not in out


STOP_SCRIPT = """
import sys

import salmon.webui
from salmon.common import commandgroup

commandgroup(["web", "--port", sys.argv[1]], prog_name="salmon")
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["ctrl-c", "sigterm"])
def test_ctrl_c_and_sigterm_stop_the_server_cleanly(signum: int) -> None:
    # A fresh interpreter with the test config (see conftest.py), SIGINT at its default as in a terminal.
    proc = subprocess.Popen(
        [sys.executable, "-c", STOP_SCRIPT, str(_free_port())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    try:
        stdout = proc.stdout
        assert stdout is not None
        first = stdout.readline()
        assert "log in at" in first, first + proc.communicate(timeout=10)[0]
        proc.send_signal(signum)
        rest, _ = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0, rest
    assert "salmon web: stopped." in rest
    assert "Traceback" not in rest


# --- The [web] config section -------------------------------------------------


def _parse(tmp_path: Path, section: str):
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir()
    torrents.mkdir()
    text = _DEFAULT_CONFIG.read_text(encoding="utf-8")
    text = text.replace("download_directory = '.music'", f"download_directory = '{music.as_posix()}'")
    text = text.replace("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{torrents.as_posix()}'")
    path = tmp_path / "config.toml"
    path.write_text(text + "\n" + section, encoding="utf-8")
    return _parse_config(path).web


def test_a_config_without_web_gets_the_defaults(tmp_path: Path) -> None:
    web_cfg = _parse(tmp_path, "")
    assert (web_cfg.host, web_cfg.port, web_cfg.token, web_cfg.allowed_hosts, web_cfg.max_jobs) == (
        "127.0.0.1",
        55155,
        None,
        [],
        2,
    )
    # The spectrals viewer keeps its own section.
    assert cfg.upload.web_interface.port == 55110


def test_the_documented_web_section_is_valid(tmp_path: Path) -> None:
    text = _DEFAULT_CONFIG.read_text(encoding="utf-8")
    section = text[text.index("# [web]") :]
    uncommented = "\n".join(line.removeprefix("# ") for line in section.splitlines() if not line.startswith("# #"))
    web_cfg = _parse(tmp_path, uncommented)
    assert web_cfg.port == 55155
    assert web_cfg.allowed_hosts == ["seedbox.lan", "192.168.1.20"]
    assert web_cfg.token is not None and len(web_cfg.token) >= 32


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('[web]\ntoken = "' + "x" * 31 + '"\n', "web.token must be at least 32 characters"),
        ("[web]\nmax_jobs = 0\n", "web.max_jobs must be at least 1"),
        ("[web]\nport = 0\n", "web.port must be between 1 and 65535"),
        ("[web]\nport = 65536\n", "web.port must be between 1 and 65535"),
    ],
)
def test_invalid_web_settings_are_refused(tmp_path: Path, section: str, message: str) -> None:
    with pytest.raises(msgspec.ValidationError, match=message):
        _parse(tmp_path, section)


def test_an_empty_token_is_no_token(tmp_path: Path) -> None:
    assert _parse(tmp_path, '[web]\ntoken = ""\n').token is None
