"""A local proxy for tests: SOCKS4/4A, SOCKS5 or HTTP CONNECT, logging the tunnels it opens.

It only ever connects to this machine: a request for 127.0.0.1, localhost or a name under .invalid
goes to 127.0.0.1, and any other destination is refused. Tests can name a site under .invalid to
check that the proxy, not the client, resolves it: such a name never resolves locally.
"""

import asyncio
import base64
import contextlib
import ipaddress
import socket
import struct


def free_port() -> int:
    """A local port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeProxy:
    """A proxy speaking one protocol, with optional credentials, that can refuse or stall.

    Args:
        kind: "socks4", "socks5" or "http".
        auth: The (user, password) it requires, if any.
        refuse: Answer every tunnel request with a failure (SOCKS: connection refused, HTTP: 502).
        stall: Accept the connection, then never answer.
    """

    def __init__(
        self, kind: str, *, auth: tuple[str, str] | None = None, refuse: bool = False, stall: bool = False
    ) -> None:
        self.kind = kind
        self.auth = auth
        self.refuse = refuse
        self.stall = stall
        # Each destination asked for, as the client named it: (host, port).
        self.targets: list[tuple[str, int]] = []
        # Whether each SOCKS request named its destination by name rather than by address.
        self.by_name: list[bool] = []
        # Client connections accepted, failed logins, and tunnels open now and at most at once.
        self.connections = 0
        self.failed_logins = 0
        self.open = 0
        self.peak = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, port: int = 0) -> str:
        """Start listening; return the proxy's URL, with its credentials if it has some."""
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.url()

    def url(self, auth: tuple[str, str] | None = None) -> str:
        user, password = auth or self.auth or (None, None)
        credentials = f"{user}:{password}@" if user is not None else ""
        return f"{self.kind}://{credentials}127.0.0.1:{self.port}"

    async def stop(self) -> None:
        self.server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.server.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        self.connections += 1
        try:
            if self.stall:
                await reader.read()
                return
            handler = {"socks4": self._socks4, "socks5": self._socks5, "http": self._http}[self.kind]
            await handler(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _socks5(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        method = 2 if self.auth else 0
        if method not in methods:
            writer.write(b"\x05\xff")
            return
        writer.write(bytes([5, method]))
        if self.auth:
            _version, size = await reader.readexactly(2)
            user = (await reader.readexactly(size)).decode()
            (size,) = await reader.readexactly(1)
            password = (await reader.readexactly(size)).decode()
            if (user, password) != self.auth:
                self.failed_logins += 1
                writer.write(b"\x01\x01")
                return
            writer.write(b"\x01\x00")
        _version, _command, _reserved, address_type = await reader.readexactly(4)
        if address_type == 3:
            (size,) = await reader.readexactly(1)
            host = (await reader.readexactly(size)).decode()
        else:
            host = str(ipaddress.ip_address(await reader.readexactly(4 if address_type == 1 else 16)))
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        self.by_name.append(address_type == 3)
        failure = b"\x05\x05\x00\x01" + bytes(6)
        success = b"\x05\x00\x00\x01" + bytes(6)
        await self._tunnel(reader, writer, host, port, success, failure)

    async def _socks4(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _version, _command, port, address = struct.unpack("!BBH4s", await reader.readexactly(8))
        await reader.readuntil(b"\x00")  # The user id
        # SOCKS4A: an address of 0.0.0.x, x > 0, means a hostname follows.
        by_name = address[:3] == bytes(3) and address[3] != 0
        host = (await reader.readuntil(b"\x00"))[:-1].decode() if by_name else str(ipaddress.ip_address(address))
        self.by_name.append(by_name)
        await self._tunnel(reader, writer, host, port, b"\x00\x5a" + bytes(6), b"\x00\x5b" + bytes(6))

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        request_line, *header_lines = head.split("\r\n")
        _method, target, _version = request_line.split()
        headers = dict(line.split(": ", 1) for line in header_lines if line)
        if self.auth:
            expected = "Basic " + base64.b64encode(":".join(self.auth).encode()).decode()
            if headers.get("Proxy-Authorization") != expected:
                self.failed_logins += 1
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                return
        host, _, port = target.rpartition(":")
        success = b"HTTP/1.1 200 Connection established\r\n\r\n"
        await self._tunnel(reader, writer, host, int(port), success, b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        success: bytes,
        failure: bytes,
    ) -> None:
        self.targets.append((host, port))
        local = host in ("127.0.0.1", "localhost") or host.endswith(".invalid")
        if self.refuse or not local:
            writer.write(failure)
            return
        try:
            site_reader, site_writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            writer.write(failure)
            return
        writer.write(success)
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            await asyncio.gather(_pipe(reader, site_writer), _pipe(site_reader, writer))
        finally:
            self.open -= 1
            site_writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy until either side hangs up, then hang up the other."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()
