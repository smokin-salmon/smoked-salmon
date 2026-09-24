import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp

from salmon import proxy

mimetypes.init()

# aiohttp's default: 5 minutes for a whole upload, 30 s to connect. It counts the wait for a
# free pooled connection too, so a batch queues its uploads before they reach aiohttp, not in
# the pool. A total is kept because the read timeout only starts once the body is sent: without
# it, an upload the host stops reading would hang forever.
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=30)


class BaseImageUploader:
    """Base class for image uploaders.

    Subclasses should implement the async upload_file method, and send their requests
    through the session from `_http_session()`.
    """

    # The host's key in [proxy.services], whose proxy `_http_session()` goes through. None for a
    # host that sends nothing through it.
    proxy_service: str | None = None

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @asynccontextmanager
    async def connections(self, limit: int) -> AsyncIterator[None]:
        """Send the uploads made inside this block over one pool of at most `limit` connections.

        The connections are reused from one upload to the next instead of opened for each image.
        """
        async with aiohttp.ClientSession(
            connector=proxy.connector(self.proxy_service, limit=limit), timeout=UPLOAD_TIMEOUT
        ) as session:
            self._session = session
            try:
                yield
            finally:
                self._session = None

    @asynccontextmanager
    async def _http_session(self) -> AsyncIterator[aiohttp.ClientSession]:
        """Get the session to upload through: the pool of `connections()`, else one for this upload."""
        if self._session is not None:
            yield self._session
        else:
            async with aiohttp.ClientSession(
                timeout=UPLOAD_TIMEOUT, **proxy.session_kwargs(self.proxy_service)
            ) as session:
                yield session

    async def upload_file(self, filename: str) -> tuple[str, str | None]:
        """Upload an image file and return the URL.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url). deletion_url may be None.

        Raises:
            ValueError: If the file is not an image.
            NotImplementedError: If not overridden by subclass.
        """
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type or mime_type.split("/")[0] != "image":
            raise ValueError(f"Unknown image file type {mime_type}")
        raise NotImplementedError("Subclasses must implement upload_file")
