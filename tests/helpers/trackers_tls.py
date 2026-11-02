"""Helpers of tests/test_trackers_tls.py that other test modules use too."""

import asyncio
import ssl
from dataclasses import dataclass
from pathlib import Path


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
