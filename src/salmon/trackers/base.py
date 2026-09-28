import asyncio
import re
import socket
import sys
from collections.abc import AsyncIterator, Collection, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import aiohttp
import asyncclick as click
import msgspec
from aiohttp import FormData
from aiohttp.abc import AbstractStreamWriter
from aiohttp.http import StreamWriter
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup, Tag
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential, wait_random
from torf import TorfError, Torrent

from salmon import cfg, proxy
from salmon.common import UploadFiles
from salmon.common.redaction import redact_tracker_headers, redact_tracker_text
from salmon.constants import RELEASE_TYPES
from salmon.errors import (
    LoginError,
    RequestError,
    RequestFailedError,
    UnknownOutcomeError,
)

ARTIST_TYPES = [
    "main",
    "guest",
    "remixer",
    "composer",
    "conductor",
    "djcompiler",
    "producer",
    "arranger",
]

# What _request prints goes here instead while hold_request_messages() is active: a request
# sent in the background must not print over a prompt the user is answering meanwhile.
_held_request_messages: ContextVar[list[tuple[str, dict[str, Any]]] | None] = ContextVar(
    "held_request_messages", default=None
)


def _secho(message: str, **styles: Any) -> None:
    """Print a message about a request, or hold it back while hold_request_messages() is active."""
    held = _held_request_messages.get()
    if held is None:
        click.secho(message, **styles)
    else:
        held.append((message, styles))


@contextmanager
def hold_request_messages() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Hold back what requests sent from this context print, for the caller to show later.

    The context is the running task's, and the tasks it starts inherit it: requests other tasks
    send meanwhile still print as usual.

    Yields:
        The held messages, as (message, click.secho keyword arguments) pairs.
    """
    held: list[tuple[str, dict[str, Any]]] = []
    token = _held_request_messages.set(held)
    try:
        yield held
    finally:
        _held_request_messages.reset(token)


def _normalize_session_cookie(cookie: str) -> str:
    """Percent-encode a session cookie the way browsers send it.

    Some cookie editors show the session in decoded form (`abc/def+ghi:jkl==`). aiohttp
    quotes a value containing `/ + : =`, and PHP url-decodes cookies, so Gazelle would
    read the quotes and a space for every `+` and reject the session. An already
    encoded value is unchanged.

    Args:
        cookie: Session cookie value from config, decoded or encoded.

    Returns:
        The percent-encoded cookie value.
    """
    return quote(unquote(cookie.strip()), safe="")


def _add_form_field(form: FormData, key: str, value: Any) -> None:
    """Add a single value to FormData, coercing types as needed.

    aiohttp FormData only accepts str/bytes/IO types, so bool and int
    values are converted accordingly. False and None are skipped.

    Args:
        form: The FormData instance to add the field to.
        key: The field name.
        value: The field value.
    """
    if value is True:
        form.add_field(key, "on")
    elif value is False or value is None:
        return
    elif isinstance(value, int):
        form.add_field(key, str(value))
    else:
        form.add_field(key, value)


def _compose_form_data(files: UploadFiles, data: dict[str, Any]) -> FormData:
    """Compose FormData by converting UploadFiles and adding data fields.

    Args:
        files: The UploadFiles object containing file uploads.
        data: Dictionary of field names and values to add.

    Returns:
        A new FormData object with all files and fields added.
    """
    form = FormData()
    form.add_field(
        "file_input",
        files.torrent_data,
        filename="meowmeow.torrent",
        content_type="application/octet-stream",
    )
    for log_name, log_data in files.log_files:
        form.add_field(
            "logfiles[]",
            log_data,
            filename=log_name,
            content_type="application/octet-stream",
        )
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                _add_form_field(form, key, item)
        else:
            _add_form_field(form, key, value)
    return form


# Inputs a browser does not submit as text: a file input sends a file, and a button only
# its own name, when it is the one clicked.
_UNSUBMITTED_INPUT_TYPES = frozenset({"file", "submit", "button", "reset", "image"})


def _submitted_fields(form: Tag) -> list[tuple[str, str]]:
    """Get the fields a browser submits for a form as the page shows it, in page order.

    An unchecked checkbox or radio is not sent, a checked one without a value sends "on".
    A select sends its selected option, or its first one when none is, and an option
    without a value sends its text. Entities are decoded once, as the tracker expects the
    text itself. Disabled fields are sent too: RED's edit form enables them all before it
    submits, and a field sent with the value it shows is left as it is.

    Args:
        form: The form element.

    Returns:
        The (name, value) pairs, in page order.
    """
    fields: list[tuple[str, str]] = []
    for control in form.find_all(["input", "select", "textarea"]):
        if not isinstance(control, Tag) or not control.get("name"):
            continue
        name = str(control["name"])
        if control.name == "input":
            input_type = str(control.get("type", "text")).lower()
            if input_type in _UNSUBMITTED_INPUT_TYPES:
                continue
            if input_type not in ("checkbox", "radio"):
                fields.append((name, str(control.get("value", ""))))
            elif control.has_attr("checked"):
                fields.append((name, str(control.get("value", "on"))))
        elif control.name == "select":
            options = [option for option in control.find_all("option") if isinstance(option, Tag)]
            selected = [option for option in options if option.has_attr("selected")]
            if not control.has_attr("multiple"):
                # The last selected option wins; with none, the first one not disabled.
                selected = selected[-1:] or [option for option in options if not option.has_attr("disabled")][:1]
            for option in selected:
                value = option.get("value")
                fields.append((name, " ".join(option.get_text().split()) if value is None else str(value)))
        else:
            # A newline right after the opening tag is not part of the text.
            fields.append((name, control.get_text().removeprefix("\n")))
    return fields


def _torrent_edit_fields(page: str, torrent_id: int) -> list[tuple[str, str]]:
    """Read a torrent's edit form, as a browser would submit it.

    The page also holds forms that move the torrent to another group. The edit form is
    the one named "torrent" that posts action=takeedit, and it must be the only one.

    Args:
        page: The HTML of torrents.php?action=edit.
        torrent_id: The torrent the form must edit.

    Returns:
        The form's (name, value) pairs, in page order.

    Raises:
        RequestError: If the page does not hold exactly one edit form for this torrent,
            with a description and an auth field (a login or an error page, say).
    """
    soup = BeautifulSoup(page, "lxml")
    forms = [
        form
        for form in soup.find_all("form", attrs={"name": "torrent"})
        if isinstance(form, Tag) and form.find("input", attrs={"type": "hidden", "name": "action", "value": "takeedit"})
    ]
    if len(forms) != 1:
        raise RequestError(f"expected one edit form on the page, found {len(forms)}")
    fields = _submitted_fields(forms[0])
    descriptions = [name for name, _ in fields].count("release_desc")
    if descriptions != 1:
        raise RequestError(f"expected one description field in the edit form, found {descriptions}")
    if ("torrentid", str(torrent_id)) not in fields:
        raise RequestError(f"the edit form is not for torrent {torrent_id}")
    if not dict(fields).get("auth"):
        raise RequestError("the edit form has no auth field")
    return fields


# Gazelle's own redirects take up to two hops: torrents.php?torrentid= to its group and
# an upload POST to the group it created take one, and an upload that fills a request
# takes two (upload.php to requests.php?action=takefill to requests.php?action=view).
# The third is a margin: running out after a POST leaves its outcome unknown, so an
# upload that went through is only found by looking it up.
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset(
    {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
)


# A connection that was never made carried nothing to the tracker.
_NOT_SENT_ERRORS = (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError)

# The server errors an idempotent request is sent again on: the tracker, or a gateway in front
# of it, may answer the next attempt.
_TRANSIENT_5XX = frozenset(
    {
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)

# How long to wait, in seconds, before looking up an upload whose answer was lost, and before
# looking it up a second and last time if the tracker does not have it yet. When the upload timed
# out (30 s without an answer), the tracker is likely still handling it, and the torrent only
# shows up once it is done. The first wait lets an upload that was nearly done land, and costs
# little when the answer was lost after the tracker was done. The second covers a slow tracker,
# up to about 70 s after the upload was sent. The user waits 40 s at most, on top of the timeout
# they already sat through.
_LOST_UPLOAD_FIRST_WAIT = 10
_LOST_UPLOAD_SECOND_WAIT = 30

# A request body goes out in slices this big, each within the request's timeout. Once more than
# 64 KiB wait to go out, aiohttp holds the next slice until the connection takes most of them, so
# a slice times out only when the connection took next to nothing for that long.
_SEND_SLICE = 64 * 1024

# How much of a body not sent yet the kernel may hold (TCP_NOTSENT_LOWAT), and the option's number
# where the platform has it: Python's socket module does not always export it.
_UNSENT_LIMIT = 128 * 1024
_TCP_NOTSENT_LOWAT = {"linux": 25, "darwin": 0x201}.get(sys.platform)


def _keep_little_unsent(writer: AbstractStreamWriter) -> None:
    """Let the kernel hold only a little of the body that is not sent yet.

    sock_read starts once the last of the body is handed to the kernel, whose send buffer grows to
    megabytes. On a slow uplink, sending what it still holds could take longer than the timeout,
    while the tracker can only answer once it has it all. What is in flight is not limited, so a
    fast link keeps its speed. Where the option is missing (Windows), the kernel keeps its buffer.
    """
    transport = writer.transport if isinstance(writer, StreamWriter) else None
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is not None and _TCP_NOTSENT_LOWAT is not None:
        with suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, _TCP_NOTSENT_LOWAT, _UNSENT_LIMIT)


class _StallBoundBody(aiohttp.payload.Payload):
    """A request body sent in slices, failing once one does not go out within `stall_secs`.

    aiohttp bounds no part of sending a body: sock_read only starts once all of it is sent. A
    tracker that stops reading would hold the request for good, and with it every later request
    of the client that is not idempotent, as those go one at a time. A slow but moving upload
    goes through, however long it takes.

    The kernel is also kept from holding much of the body not sent yet (_keep_little_unsent), so
    sock_read starts once little of it is left to send.
    """

    _value: bytes
    _autoclose = True

    def __init__(self, value: bytes, content_type: str, stall_secs: int) -> None:
        super().__init__(value, content_type=content_type)
        self._size = len(value)
        self._stall_secs = stall_secs

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self._value.decode(encoding, errors)

    async def write(self, writer: AbstractStreamWriter) -> None:
        await self.write_with_length(writer, None)

    async def write_with_length(self, writer: AbstractStreamWriter, content_length: int | None) -> None:
        _keep_little_unsent(writer)
        body = memoryview(self._value)[:content_length]
        for start in range(0, len(body), _SEND_SLICE):
            try:
                async with asyncio.timeout(self._stall_secs):
                    await writer.write(body[start : start + _SEND_SLICE])
            except TimeoutError:
                raise TimeoutError(f"sending the request stalled for {self._stall_secs} s") from None


async def _stall_bound_body(data: Any, stall_secs: int) -> _StallBoundBody:
    """Turn request data into a body sent within a stall bound, as aiohttp would turn it into a payload."""
    if isinstance(data, FormData):
        data = data()
    try:
        body = aiohttp.payload.get_payload(data)
    except aiohttp.payload.LookupError:
        # A dict or a list of fields, sent as a form.
        body = FormData(data)()
    return _StallBoundBody(await body.as_bytes(), body.content_type, stall_secs)


class RetryableError(RequestError):
    """A failed request that may be sent again. Raised as is once the retries run out."""

    pass


class HttpResponse(msgspec.Struct, frozen=True):
    """HTTP response data extracted from aiohttp.ClientResponse."""

    text: str
    url: str
    status: int


class BaseGazelleApi:
    """Base API client for Gazelle-based trackers."""

    # Subclasses must set these attributes before calling __init__
    cookie: str
    base_url: str
    tracker_url: str
    site_code: str
    site_string: str
    api_key: str = ""  # Optional, only for API key upload

    # Artist roles (from ARTIST_IMPORTANCES) that this tracker's upload form does not offer.
    # Uploads drop artists with these roles rather than send an unrecognised importance value.
    unsupported_artist_roles: frozenset[str] = frozenset()

    # Rate limiter: 5 requests per 10 seconds (shared across all instances)
    _rate_limiter = AsyncLimiter(5, 10)

    def __init__(self) -> None:
        """Initialize the API client. Subclasses should call this after setting cookie/base_url."""
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": cfg.upload.user_agent,
        }
        if not hasattr(self, "dot_torrents_dir"):
            self.dot_torrents_dir = cfg.directory.dottorrents_dir

        self.release_types = RELEASE_TYPES
        self.authkey: str | None = None
        self.passkey: str | None = None
        self._authenticated = False
        # The authentication in progress, shared by the requests waiting for it, and how many they are.
        self._authentication: asyncio.Task[None] | None = None
        self._authentication_waiters = 0
        self._session: aiohttp.ClientSession | None = None
        # Held while a request that is not idempotent is in flight, on its own connection.
        self._non_idempotent_lock = asyncio.Lock()

    def _get_cookies(self) -> dict[str, str]:
        """Get cookies dict for requests."""
        return {"session": _normalize_session_cookie(self.cookie)}

    def _new_session(self, connections: int) -> aiohttp.ClientSession:
        """Make an HTTP session with a pool of at most `connections` connections, through the tracker's proxy if any."""
        # DummyCookieJar keeps nothing between requests, so an api-key request
        # still goes out without a session cookie.
        return aiohttp.ClientSession(
            connector=proxy.connector(self.site_code.lower(), limit=connections),
            cookie_jar=aiohttp.DummyCookieJar(),
        )

    def _http_session(self) -> aiohttp.ClientSession:
        """Get the persistent HTTP session for this API instance."""
        if self._session is None or self._session.closed:
            # A small, reused pool, so gathered calls cannot burst one TLS handshake
            # per request from one IP and read as scanner traffic to tracker edges.
            # Two connections keep short batches from queueing behind a single one;
            # long batches are paced by the rate limiter anyway.
            # Per instance, as a ClientSession binds to the running loop.
            self._session = self._new_session(connections=2)
            with suppress(RuntimeError):
                click.get_current_context().call_on_close(self.close)
        return self._session

    async def close(self) -> None:
        """Close the persistent HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    @asynccontextmanager
    async def _session_for(self, idempotent: bool) -> AsyncIterator[aiohttp.ClientSession]:
        """Get the HTTP session to send a request and its redirect hops through.

        A pooled connection may be one the tracker is closing as idle, and a request that fails
        on it cannot be told from one the tracker acted on. A request that is not idempotent
        goes out on a new connection instead, in a session of its own, closed once the request
        is done. Closing the shared pool for it would cut off the other requests in flight on
        it (#472). Such requests go one at a time per client: gathered, each would open its
        new connection at once, the burst of handshakes the pool's cap is there to prevent.

        Args:
            idempotent: Whether the request is idempotent.

        Yields:
            The shared session, or a session of the request's own.
        """
        if idempotent:
            yield self._http_session()
        else:
            async with self._non_idempotent_lock, self._new_session(connections=1) as session:
                yield session

    @property
    def has_session_cookie(self) -> bool:
        """Whether a session cookie is configured. Site pages outside the API need one."""
        return bool(self.cookie.strip())

    def _secrets(self) -> list[str | None]:
        """This tracker's secrets, as what it sends back may repeat them.

        The session cookie as configured, decoded and as sent, the api key, and the authkey and
        passkey once authenticated.
        """
        cookie = self.cookie.strip()
        return [cookie, unquote(cookie), _normalize_session_cookie(cookie), self.api_key, self.authkey, self.passkey]

    def _redact(self, text: str) -> str:
        """Mask this tracker's secrets in text about to be printed or raised."""
        return redact_tracker_text(text, self._secrets())

    @property
    def announce(self) -> str:
        """Get the announce URL."""
        return f"{self.tracker_url}/{self.passkey}/announce"

    def request_url(self, id: int) -> str:
        """Get URL for a request page.

        Args:
            id: The request ID.

        Returns:
            The request URL.
        """
        return f"{self.base_url}/requests.php?action=view&id={id}"

    async def authenticate(self) -> None:
        """Authenticate with the tracker API and get authkey/passkey."""
        acctinfo = await self.api_call("index")
        self.authkey = acctinfo["authkey"]
        self.passkey = acctinfo["passkey"]
        self._authenticated = True

    async def ensure_authenticated(self) -> None:
        """Ensure we are authenticated before making requests.

        Requests that go out together on a fresh client share one authentication attempt,
        so one index call, instead of each sending its own (#468). If it fails, each of them
        gets its error; none tries again for itself. A call made once it is over tries again.
        The attempt runs on its own, so cancelling one request leaves it to the others, and
        is cancelled only when no request waits for it any more.
        """
        if self._authenticated:
            return
        if self._authentication is None or self._authentication.done():
            self._authentication = asyncio.create_task(self.authenticate())
        attempt = self._authentication
        self._authentication_waiters += 1
        try:
            await asyncio.shield(attempt)
        finally:
            self._authentication_waiters -= 1
            if not self._authentication_waiters and not attempt.done():
                attempt.cancel()
                # A request arriving meanwhile must not wait on an attempt being cancelled.
                self._authentication = None

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
        timeout_secs: int = 10,
        prefer_api_key: bool = False,
        idempotent: bool | None = None,
        needs_authkey: bool = True,
        expected_error_statuses: Collection[int] = (),
    ) -> HttpResponse:
        """Authenticated HTTP request, returns response data.

        Args:
            method: HTTP method (e.g. "GET", "POST").
            url: The URL to request.
            params: Query parameters.
            data: POST body data.
            timeout_secs: How long, in seconds, connecting, each read and reading the whole
                answer may take, and how long sending the body may stall.
            prefer_api_key: If True and api_key is set, use Authorization header
                only (no cookie). If False or api_key is empty, use cookie only
                (no Authorization header).
            idempotent: Whether sending the request twice leaves the tracker as sending
                it once. Defaults to False for POST and True otherwise.
            needs_authkey: Whether the request needs the authkey, which a fresh client
                fetches with an index call first. An api key request that sends no auth
                field does not.
            expected_error_statuses: Error statuses the endpoint answers with a body the
                caller reads itself: the response is returned instead of raising. Not on a
                later hop of a request that is not idempotent, which the tracker has acted on.

        Redirects within the site are followed, up to three hops, each one through the
        rate limiter. A redirect to the login page raises LoginError without requesting it.

        Returns:
            HttpResponse with text, url, status, and headers.

        Raises:
            RetryableError: If the request still fails after its retries.
            UnknownOutcomeError: If a request that is not idempotent fails after it may
                have reached the tracker.
        """
        # Before the retries, not within them: the index call has retries of its own, and
        # authenticating again on each retry would send up to 25 index calls per request.
        if needs_authkey and not (params and params.get("action") == "index"):
            await self.ensure_authenticated()
        return await self._send(
            method, url, params, data, timeout_secs, prefer_api_key, idempotent, expected_error_statuses
        )

    # The retry policy. A failed request is sent again only when that cannot do more on the
    # tracker than sending it once: the request is idempotent, or the tracker cannot have
    # acted on it (no connection was made, or it answered 429). A state-changing request
    # that fails once it may have reached the tracker (a timeout, a dropped connection, a
    # 5xx, or a failure on a redirect after it) raises UnknownOutcomeError instead, as
    # sending it again could upload or report twice (#446).
    @retry(
        retry=retry_if_exception_type(RetryableError),
        stop=stop_after_attempt(5),
        # Backing off beats hammering at a fixed 1s, and the random term keeps a
        # batch that fails together from retrying in one synchronized salvo.
        wait=wait_exponential(multiplier=1, min=1, max=30) + wait_random(0, 2),
        reraise=True,
    )
    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        data: Any,
        timeout_secs: int,
        prefer_api_key: bool,
        idempotent: bool | None,
        expected_error_statuses: Collection[int],
    ) -> HttpResponse:
        """Send a request with the retry policy. _request authenticates first; see it for the arguments."""
        if idempotent is None:
            idempotent = method != "POST"
        # Once the tracker redirects, it has acted on the request, whatever happens next.
        redirected = False

        def failure(message: str, *, not_acted_on: bool = False) -> RequestError:
            if idempotent or (not_acted_on and not redirected):
                return RetryableError(message)
            return UnknownOutcomeError(message)

        if data is not None:
            # Nothing else bounds sending a body.
            data = await _stall_bound_body(data, timeout_secs)

        use_api_key = prefer_api_key and bool(self.api_key)
        headers = {**self.headers, **({"Authorization": self.api_key} if use_api_key else {})}
        cookies = {} if use_api_key else self._get_cookies()

        if cfg.upload.debug_tracker_connection:
            _secho(f"[DEBUG] {method} {self._redact(url)}", fg="cyan")
            _secho(f"[DEBUG] params: {self._redact(msgspec.json.encode(params).decode())}", fg="cyan")
            _secho(f"[DEBUG] use_api_key: {use_api_key}", fg="cyan")

        try:
            # No total: it would also count the wait for a free pooled connection,
            # so a request queued behind others could expire, be aborted and retried.
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout_secs, sock_read=timeout_secs)
            async with self._session_for(idempotent) as session:
                # aiohttp would follow redirects within one rate limiter slot, and follow a
                # bounce to the login page up to ten times (#432). Each hop goes out here
                # instead, through the limiter, and the login page is never requested.
                for _ in range(_MAX_REDIRECTS + 1):
                    async with (
                        self._rate_limiter,
                        session.request(
                            method,
                            url,
                            params=params,
                            data=data,
                            headers=headers,
                            cookies=cookies,
                            timeout=timeout,
                            allow_redirects=False,
                        ) as resp,
                    ):
                        # sock_read restarts on every chunk, so a trickled body needs a bound of its
                        # own. It starts once the answer has come, after any wait for a free pooled
                        # connection, which must still not count.
                        async with asyncio.timeout(timeout_secs):
                            text = await resp.text()

                        if cfg.upload.debug_tracker_connection:
                            _secho(f"[DEBUG] status: {resp.status}", fg="cyan")
                            headers_shown = redact_tracker_headers(resp.headers.items(), self._secrets())
                            _secho(
                                f"[DEBUG] response headers: {msgspec.json.encode(headers_shown).decode()}", fg="cyan"
                            )
                            _secho(f"[DEBUG] response body: {self._redact(text)}", fg="green")

                        if not resp.ok:
                            # Checked before any status: the tracker acted on the request when it
                            # redirected it, so a failed later hop leaves its outcome unknown. An
                            # error status the caller expects answers that later hop, not the request.
                            if redirected and not idempotent:
                                raise UnknownOutcomeError(f"{self.site_string} answered {resp.status} on a later hop")

                            error_msg = text
                            with suppress(msgspec.DecodeError, ValueError):
                                error_msg = msgspec.json.encode(msgspec.json.decode(text)["error"]).decode()
                            # Printed and raised: an error page carries the authkey in its links and forms.
                            error_msg = self._redact(error_msg)

                            if resp.status == HTTPStatus.TOO_MANY_REQUESTS or "rate limit" in error_msg.lower():
                                retry_after = float(resp.headers.get("Retry-After", "20"))
                                if _held_request_messages.get() is not None:
                                    # This is only printed after the wait is over (once the held
                                    # messages are flushed), so word it in the past.
                                    _secho(f"Rate limit exceeded, waited {retry_after} seconds", fg="yellow")
                                else:
                                    _secho(f"Rate limit exceeded, waiting {retry_after} seconds...", fg="yellow")
                                await asyncio.sleep(retry_after)
                                raise failure("Rate limit exceeded", not_acted_on=True)

                            if resp.status == HTTPStatus.UNAUTHORIZED:
                                _secho(
                                    f"Authentication to {self.site_string} failed: {error_msg}.\n"
                                    "Your API key may be invalid.",
                                    fg="red",
                                )
                                raise LoginError(error_msg)

                            if resp.status in expected_error_statuses:
                                return HttpResponse(text=text, url=str(resp.url), status=resp.status)

                            # Any 5xx may follow the tracker acting on a POST; a GET is resent only on these.
                            if resp.status >= HTTPStatus.INTERNAL_SERVER_ERROR and (
                                not idempotent or resp.status in _TRANSIENT_5XX
                            ):
                                raise failure(f"Server error {resp.status}")

                            _secho(
                                f"Request to {self.site_string} failed ({resp.status}): {error_msg}",
                                fg="red",
                            )
                            raise RequestFailedError(error_msg)

                        location = resp.headers.get(aiohttp.hdrs.LOCATION)
                        if resp.status not in _REDIRECT_STATUSES or not location:
                            return HttpResponse(
                                text=text,
                                url=str(resp.url),
                                status=resp.status,
                            )

                        current = urlparse(str(resp.url))
                        target = urlparse(urljoin(str(resp.url), location))
                        if target.path.endswith("/login.php"):
                            if redirected and not idempotent:
                                # As for an error status on a later hop: the tracker has acted.
                                raise UnknownOutcomeError(f"{self.site_string} sent a later hop to its login page")
                            _secho(
                                f"{self.site_string} sent this request to its login page: your session cookie is "
                                f"missing or expired. Check tracker.{self.site_code.lower()}.session in your config.",
                                fg="red",
                                bold=True,
                            )
                            raise LoginError(f"{self.site_string} redirected to its login page")
                        if (target.scheme, target.netloc) != (current.scheme, current.netloc):
                            _secho(
                                f"{self.site_string} redirected to {target.scheme}://{target.netloc}, not following.",
                                fg="red",
                            )
                            if redirected and not idempotent:
                                raise UnknownOutcomeError(f"{self.site_string} redirected a later hop to another site")
                            raise RequestFailedError(f"{self.site_string} redirected to another site")

                        if resp.status == HTTPStatus.SEE_OTHER or (
                            resp.status in (HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND) and method == "POST"
                        ):
                            # As browsers and aiohttp do, the target of a redirected POST is fetched with GET.
                            method, data = "GET", None
                        elif not idempotent and method != "GET":
                            # A 307 or 308 asks for the request to be sent again as it is, body and all.
                            # The tracker may have acted on it already, so it is not sent again.
                            raise UnknownOutcomeError(
                                f"{self.site_string} asked for the request to be sent again to {target.path}"
                            )
                        url, params = target._replace(fragment="").geturl(), None
                        redirected = True

                _secho(f"Too many redirects from {self.site_string}, last to {urlparse(url).path}", fg="red")
                message = f"Too many redirects from {self.site_string}"
                # The tracker redirected at least once, so it has acted on the request.
                raise RequestFailedError(message) if idempotent else UnknownOutcomeError(message)
        except (TimeoutError, aiohttp.ClientError) as err:
            # The body read's own timeout raises a TimeoutError with no message.
            reason = self._redact(str(err)) or f"no full answer within {timeout_secs} s"
            raise failure(f"Network error: {reason}", not_acted_on=isinstance(err, _NOT_SENT_ERRORS)) from err

    async def api_call(self, action: str, params: dict[str, Any] | None = None) -> dict:
        """Make a request to the site API with rate limiting.

        Args:
            action: The API action to perform.
            params: Additional parameters for the request.

        Returns:
            The API response data.

        Raises:
            LoginError: If authentication fails.
            RequestFailedError: If the request fails.
            RetryableError: If network error persists after retries.
        """
        url = self.base_url + "/ajax.php"
        params = {"action": action, **(params or {})}

        resp = await self._request("GET", url, params=params, timeout_secs=5, prefer_api_key=True)

        try:
            resp_json = msgspec.json.decode(resp.text)
        except (msgspec.DecodeError, ValueError):
            resp_json = {"status": "error", "error": resp.text}

        if resp_json.get("status") != "success":
            raise RequestFailedError(self._redact(str(resp_json.get("error", resp.text))))
        return cast("dict", resp_json["response"])

    async def torrentgroup(self, group_id: int) -> dict:
        """Get information about a torrent group.

        Args:
            group_id: The torrent group ID.

        Returns:
            The torrent group data.
        """
        return await self.api_call("torrentgroup", params={"id": group_id})

    async def get_redirect_torrentgroupid(self, torrentid: int) -> int | None:
        """Get torrent group ID from torrent ID via redirect.

        Args:
            torrentid: The torrent ID.

        Returns:
            The torrent group ID as int, or None if not found.
        """
        url = self.base_url + "/torrents.php"
        try:
            resp = await self._request("GET", url, params={"torrentid": torrentid}, timeout_secs=5)
        except TimeoutError:
            click.secho("Connection to API timed out, try script again later. Gomen!", fg="red")
            raise click.Abort() from None
        parsed = urlparse(resp.url)
        query = parse_qs(parsed.query)
        group_id = query.get("id", [None])[0]
        if group_id:
            return int(group_id)
        click.secho("Couldn't retrieve torrent_group_id from torrent_id, no Redirect found!", fg="red")
        raise click.Abort()

    async def get_request(self, id: int) -> dict:
        """Get information about a request.

        Args:
            id: The request ID.

        Returns:
            The request data.
        """
        return await self.api_call("request", params={"id": id})

    async def fetch_log(self, page: int) -> str:
        """Fetch a page of the site log.

        Args:
            page: The page number.

        Returns:
            The page HTML text.
        """
        url = f"{self.base_url}/log.php"
        resp = await self._request("GET", url, params={"page": page})
        return resp.text

    async def fetch_riplog(self, torrentid: int) -> str:
        """Fetch rip log for a torrent.

        Args:
            torrentid: The torrent ID.

        Returns:
            The log text with some content stripped.
        """
        url = f"{self.base_url}/torrents.php"
        resp = await self._request("GET", url, params={"action": "loglist", "torrentid": torrentid})
        return re.sub(r" ?\([^)]+\)", "", resp.text)

    async def get_uploads_from_log(self, max_pages: int = 10) -> list:
        """Crawl log pages and return uploads.

        Args:
            max_pages: Maximum number of pages to crawl.

        Returns:
            List of (torrent_id, artist, title) tuples.
        """
        # Page 1 goes alone first: if the log cannot be read, that costs one request,
        # not one per page (#432).
        pages = [await self.fetch_log(1)]
        pages += await asyncio.gather(*(self.fetch_log(i) for i in range(2, max_pages)))
        recent_uploads = []
        for page_text in pages:
            recent_uploads += self.parse_uploads_from_log_html(page_text)
        return recent_uploads

    async def api_key_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via API.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey

        try:
            response = await self._request(
                "POST", url, data=_compose_form_data(files, data), timeout_secs=30, prefer_api_key=True
            )
        except UnknownOutcomeError as err:
            return await self._find_lost_upload(files, err)
        try:
            resp = msgspec.json.decode(response.text)
        except (msgspec.DecodeError, ValueError) as e:
            click.secho("❌ Failed to decode JSON response", fg="red", err=True)
            click.secho(f"Status code: {response.status}", fg="red", err=True)
            click.secho(f"Response text: {repr(self._redact(response.text))}", fg="red", err=True)
            raise click.Abort from e

        try:
            if resp["status"] != "success":
                raise RequestError(f"API upload failed: {self._redact(str(resp['error']))}")
            if ("requestid" in resp["response"] and resp["response"]["requestid"]) or (
                "fillRequest" in resp["response"]
                and resp["response"]["fillRequest"]
                and resp["response"]["fillRequest"]["requestId"]
            ):
                requestId = (
                    resp["response"]["requestid"]
                    if "requestid" in resp["response"]
                    else resp["response"]["fillRequest"]["requestId"]
                )
                if requestId == -1:
                    click.secho("Request fill failed!", fg="red")
                else:
                    click.secho("Filled request: " + self.request_url(requestId), fg="green")
            torrent_id = 0
            group_id = 0
            if "torrentid" in resp["response"]:
                torrent_id = resp["response"]["torrentid"]
                group_id = resp["response"]["groupid"]
            elif "torrentId" in resp["response"]:
                torrent_id = resp["response"]["torrentId"]
                group_id = resp["response"]["groupId"]
            return torrent_id, group_id
        except TypeError as err:
            raise RequestError(f"API upload failed, response: {self._redact(str(resp))}") from err

    async def site_page_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via upload.php.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        if "groupid" in data:
            url = self.base_url + f"/upload.php?groupid={data['groupid']}"
        else:
            url = self.base_url + "/upload.php"
        data["auth"] = self.authkey

        try:
            response = await self._request("POST", url, data=_compose_form_data(files, data), timeout_secs=30)
        except UnknownOutcomeError as err:
            return await self._find_lost_upload(files, err)
        resp_text = response.text
        resp_url = response.url

        if self.announce in resp_text:
            match = re.search(r'<p style="color: red; text-align: center;">(.+)<\/p>', resp_text)
            if match:
                raise RequestError(f"Site upload failed: {self._redact(match[1])} ({response.status})")
        if "requests.php" in resp_url:
            try:
                torrent_id = self.parse_torrent_id_from_filled_request_page(resp_text)
                group_id = await self.get_redirect_torrentgroupid(torrent_id) or 0
                click.secho(f"Filled request: {resp_url}", fg="green")
                return torrent_id, group_id
            except (TypeError, ValueError) as err:
                soup = BeautifulSoup(resp_text, "lxml")
                error = soup.find("h2", text="Error")
                error_message = resp_text
                if error and error.parent and error.parent.parent:
                    p_tag = error.parent.parent.find("p")
                    if p_tag:
                        error_message = p_tag.text
                raise RequestError(f"Request fill failed: {self._redact(error_message)}") from err
        try:
            return self.parse_most_recent_torrent_and_group_id_from_group_page(resp_text)
        except TypeError as err:
            raise RequestError(f"Site upload failed, response text: {self._redact(resp_text)}") from err

    async def upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via API or upload.php.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        if self.api_key:
            return await self.api_key_upload(data, files)

        return await self.site_page_upload(data, files)

    async def _find_lost_upload(self, files: UploadFiles, err: UnknownOutcomeError) -> tuple[int, int]:
        """Look up an upload whose answer was lost, by the torrent's infohash.

        The tracker may still be handling the upload, so the lookup waits for it first. If the
        tracker answers that it does not have the torrent, it is looked up once more after a
        second wait, and no more.

        Args:
            files: UploadFiles that were uploaded.
            err: Why the outcome of the upload is unknown.

        Returns:
            Tuple of (torrent_id, group_id) if the tracker has the torrent.

        Raises:
            UnknownOutcomeError: If the torrent is not found: the upload may still have gone through.
        """
        try:
            # Uppercase, as Gazelle's API documentation asks for the hash.
            infohash = Torrent.read_stream(files.torrent_data).infohash.upper()
            click.secho(
                f"Could not tell whether {self.site_string} took the upload ({err}). "
                f"Waiting {_LOST_UPLOAD_FIRST_WAIT} s for {self.site_string} to finish processing it, "
                "then looking it up...",
                fg="yellow",
            )
            await asyncio.sleep(_LOST_UPLOAD_FIRST_WAIT)
            try:
                found = await self.api_call("torrent", params={"hash": infohash})
            except RequestFailedError as not_found:
                # The tracker answered, without the torrent. Any other failure is not worth another request.
                click.secho(
                    f"{self.site_string} does not have it yet ({not_found}). "
                    f"Waiting {_LOST_UPLOAD_SECOND_WAIT} s more, then looking it up one last time...",
                    fg="yellow",
                )
                await asyncio.sleep(_LOST_UPLOAD_SECOND_WAIT)
                found = await self.api_call("torrent", params={"hash": infohash})
            torrent_id, group_id = int(found["torrent"]["id"]), int(found["group"]["id"])
        except (RequestError, TorfError, KeyError, TypeError, ValueError) as lookup_err:
            raise UnknownOutcomeError(
                f"Could not tell whether {self.site_string} took the upload ({err}), and looking the torrent up "
                f"by its infohash did not confirm it ({lookup_err}). The upload may still have gone through: "
                f"check your uploads on {self.site_string} before uploading it again."
            ) from lookup_err
        except asyncio.CancelledError:
            # Ctrl-C while waiting: still warn before the user uploads it again.
            click.secho(
                f"Stopped before finding out. The upload may still have gone through: check your uploads on "
                f"{self.site_string} before uploading it again.",
                fg="yellow",
            )
            raise
        click.secho(f"Found the upload on {self.site_string}: torrent {torrent_id}.", fg="green")
        return torrent_id, group_id

    async def report_lossy_master(self, torrent_id: int, comment: str, source: str) -> bool:
        """Report torrent for lossy master/web approval.

        Args:
            torrent_id: The torrent ID.
            comment: Report comment.
            source: Media source (e.g., "WEB").

        Returns:
            True if successful.

        Raises:
            RequestError: If report fails.
        """
        url = self.base_url + "/reportsv2.php"
        type_ = "lossywebapproval" if source == "WEB" else "lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }
        resp = await self._request("POST", url, params={"action": "takereport"}, data=data)
        if "torrents.php" in resp.url:
            return True
        raise RequestError(f"Failed to report the torrent for lossy master, code {resp.status}.")

    async def append_to_torrent_description(self, torrent_id: int, description_addition: str) -> None:
        """Add text to start of torrent description.

        The edit form sets every field of the torrent, and one it lacks is cleared or unset:
        rebuilt from the API, it lost the edition, flags and marks the API does not give (#357).
        So the tracker's own edit form is read and sent back as a browser would, with only the
        description changed. It also holds the auth, so a fresh client needs no index call.

        Args:
            torrent_id: The torrent ID.
            description_addition: Text to prepend to description.

        Raises:
            RequestError: If the edit form cannot be read, in which case nothing is sent,
                or if the edit fails.
        """
        url = self.base_url + "/torrents.php"
        page = await self._request("GET", url, params={"action": "edit", "id": torrent_id}, needs_authkey=False)
        try:
            fields = _torrent_edit_fields(page.text, torrent_id)
        except RequestError as err:
            raise RequestError(
                f"Could not read the edit form of torrent {torrent_id} on {self.site_string} ({err}). "
                "Nothing was sent: its description is unchanged."
            ) from err
        new_data = [(name, description_addition + value if name == "release_desc" else value) for name, value in fields]
        # Every field is the one the form showed, or the description computed above, so sending
        # it twice leaves the torrent as sending it once.
        resp = await self._request("POST", url, data=new_data, idempotent=True, needs_authkey=False)
        resp_text = resp.text

        soup = BeautifulSoup(resp_text, "lxml")
        edit_error = soup.find("h2", text="Error")
        if edit_error and edit_error.parent and edit_error.parent.parent:
            p_tag = edit_error.parent.parent.find("p")
            error_message = p_tag.text if p_tag else "Unknown error"
            raise RequestError(f"Failed to edit torrent: {error_message}")
        else:
            click.secho("Added spectrals to the torrent description.", fg="green")

    """The following three parsing functions are part of the gazelle class
    in order that they be easily overwritten in the derivative site classes.
    It is not because they depend on anything from the class"""

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text: str) -> tuple[int, int]:
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        torrent_ids: list[int] = []
        group_ids: list[int] = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a", class_="tooltip"):
            href = pl.get("href", "")
            torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", str(href))
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        for pl in soup.find_all("a", class_="brackets"):
            href = pl.get("href", "")
            group_url = re.search(r"upload.php\?groupid=(\d+)", str(href))
            if group_url:
                group_ids.append(int(group_url[1]))

        if not torrent_ids or not group_ids:
            raise TypeError("Could not parse torrent/group id from group page")

        return max(torrent_ids), max(group_ids)

    def parse_torrent_id_from_filled_request_page(self, text: str) -> int:
        """
        Given the HTML (ew) response from filling a request,
        find the filling torrent (hopefully our upload)
        """
        torrent_ids: list[int] = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a"):
            if pl.string == "Yes":
                href = pl.get("href", "")
                torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", str(href))
                if torrent_url:
                    torrent_ids.append(int(torrent_url[1]))
        return max(torrent_ids)

    def parse_uploads_from_log_html(self, text: str) -> list[tuple[str, str, str]]:
        """Parses a log page and returns best guess at
        (torrent id, 'Artist', 'title') tuples for uploads"""
        log_uploads: list[tuple[str, str, str]] = []
        soup = BeautifulSoup(text, "lxml")
        for entry in soup.find_all("span", class_="log_upload"):
            anchor = entry.find("a")
            if not anchor:
                continue
            href = anchor.get("href", "")
            torrent_id = str(href)[23:]
            try:
                # it having class log_upload is no guarantee that is what it is. Nice one log.
                next_sib = anchor.next_sibling
                if not next_sib:
                    continue
                torrent_string = re.findall(r"\((.*?)\) \(", str(next_sib))[0].split(" - ")
            except (IndexError, TypeError):
                continue
            artist = torrent_string[0]
            if len(torrent_string) > 1:
                title = torrent_string[1]
            else:
                artist = ""
                title = torrent_string[0]
            log_uploads.append((torrent_id, artist, title))
        return log_uploads
