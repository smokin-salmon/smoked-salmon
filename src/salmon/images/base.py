import functools
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from salmon import dryrun, proxy

mimetypes.init()

_UploadFile = Callable[[Any, str], Awaitable[tuple[str, str | None]]]

# aiohttp's default: 5 minutes for a whole upload, 30 s to connect. It counts the wait for a
# free pooled connection too, so a batch queues its uploads before they reach aiohttp, not in
# the pool. A total is kept because the read timeout only starts once the body is sent: without
# it, an upload the host stops reading would hang forever.
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=30)


def describe_upload_timeout(e: TimeoutError) -> str:
    """Word a timed-out upload's error message.

    aiohttp.ServerTimeoutError (raised for a connection that never gets accepted, and its
    subclasses) is a connection-level timeout, told apart from the plain TimeoutError raised
    once the overall upload timeout expires: each is worded with the number it actually hit, so
    neither prints a duration the failure did not wait out.

    Args:
        e: The timeout that was caught.

    Returns:
        The message to raise ImageUploadFailed with.
    """
    if isinstance(e, aiohttp.ServerTimeoutError):
        return f"Connection to the host timed out after {UPLOAD_TIMEOUT.sock_connect}s"
    return f"Upload timed out after {UPLOAD_TIMEOUT.total}s"


def _refused_in_dry_run(upload_file: _UploadFile) -> _UploadFile:
    """Wrap an image host's upload_file so it uploads nothing during a dry run."""

    @functools.wraps(upload_file)
    async def guarded(self: "BaseImageUploader", filename: str) -> tuple[str, str | None]:
        dryrun.refuse(f"upload {filename} to {self.host}")
        return await upload_file(self, filename)

    return guarded


class BaseImageUploader:
    """Base class for image uploaders.

    Subclasses should implement the async upload_file method, and send their requests
    through the session from `_http_session()`. Each one's upload_file refuses to run during
    a dry run, whatever it sends its requests through.
    """

    # The host's key in [proxy.services], whose proxy `_http_session()` goes through. None for a
    # host that sends nothing through it.
    proxy_service: str | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Every upload goes through upload_file, some not through _http_session (imgbox's library, RED's client).
        upload_file = cls.__dict__.get("upload_file")
        if upload_file is not None:
            # setattr: an assignment is checked against upload_file's own signature, which the wrapper keeps
            # but its type does not spell out.
            setattr(cls, "upload_file", _refused_in_dry_run(upload_file))  # noqa: B010

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def host(self) -> str:
        """The host's name, as HOSTS and the config know it: its module's."""
        return type(self).__module__.rsplit(".", 1)[-1]

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
