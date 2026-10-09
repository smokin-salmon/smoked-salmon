"""What every client of one tracker account shares: its rate limit, its connection pool and its lock.

A tracker limits the account, not a client object, and a command may hold several clients of one
tracker (ADR 0004, amending ADR 0001). Each tracker has its own. The state is kept per event loop, as
an asyncio lock or an aiohttp session cannot be used from another loop.

salmon web runs each job on a loop of its own, so that loop's state would be the job's only. While it
runs, every tracker request is handed over to its loop instead (`requests_on_this_loop`), so all jobs
share one rate limit, lock and pool per account. In the CLI nothing is handed over.
"""

import asyncio
import contextvars
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar
from weakref import WeakSet

import anyio
import asyncclick as click

from salmon.errors import RequestFailedError, UnknownOutcomeError

if TYPE_CHECKING:
    import aiohttp

T = TypeVar("T")

# At most this many requests enter in any window of RATE_LIMIT_PERIOD + RATE_LIMIT_MARGIN seconds.
RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_PERIOD = 10.0
# A request reaches the tracker a little after it enters the limiter (a handshake, the network), and
# not always as late as the one entered a period after it, so two requests entered exactly a period
# apart can arrive less than a period apart. With 0 to 300 ms of random latency per request
# (#628), no margin put 6 arrivals within 9.80 s; any margin above 0.3 s cannot, and 0.5 s leaves
# room for a slower handshake.
RATE_LIMIT_MARGIN = 0.5
# Kept-alive connections in an account's pool (ADR 0001).
POOL_CONNECTIONS = 2


class SlidingWindowLimiter:
    """Lets at most `max_rate` requests enter in any `period` seconds, in the order they come.

    A leaky bucket (aiolimiter) lets a burst through, then one request every period / max_rate: 6
    requests within 2 s, 9 within 10 s. Here a sixth request waits until the first entered a period
    ago. Ported from chodeus's SharedLimiter, without its thread lock: every request to a tracker
    account goes out on one event loop.
    """

    def __init__(self, max_rate: int, period: float) -> None:
        self._max_rate = max_rate
        self._period = period
        self._entered: deque[float] = deque()
        self._paused_until = 0.0
        # Waiting requests enter one at a time, first come first served.
        self._queue = asyncio.Lock()

    def pause(self, seconds: float) -> None:
        """Hold every entry for `seconds` from now, as when the tracker answers 429."""
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    def _wait(self) -> float:
        """How long until a request may enter: 0 when it may now."""
        now = time.monotonic()
        if now < self._paused_until:
            return self._paused_until - now
        while self._entered and now - self._entered[0] >= self._period:
            self._entered.popleft()
        if len(self._entered) < self._max_rate:
            return 0.0
        return self._period - (now - self._entered[0])

    async def __aenter__(self) -> None:
        async with self._queue:
            # Checked again after each wait: a 429 meanwhile may have paused the account.
            while (wait := self._wait()) > 0:
                await asyncio.sleep(wait)
            self._entered.append(time.monotonic())

    async def __aexit__(self, *_exc: object) -> None:
        return None


class TrackerAccount:
    """The state every client of one tracker shares on one event loop."""

    def __init__(self) -> None:
        self.limiter = SlidingWindowLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_PERIOD + RATE_LIMIT_MARGIN)
        # Held while a request that is not idempotent is in flight, on its own connection.
        self.non_idempotent_lock = asyncio.Lock()
        # A pooled request takes one before it enters the limiter, so it does not wait for a free
        # connection after: a request that entered and then queued behind slow answers would reach
        # the tracker late, right next to requests that entered a period after it.
        self.pool_slots = asyncio.Semaphore(POOL_CONNECTIONS)
        self.pool: aiohttp.ClientSession | None = None
        # The clients using the pool. The last one to be closed closes it.
        self.clients: WeakSet[object] = WeakSet()


_accounts: dict[asyncio.AbstractEventLoop, dict[str, TrackerAccount]] = {}
# Two threads, each with its own loop, may look up their accounts at once.
_accounts_lock = threading.Lock()


def account_for(site_code: str) -> TrackerAccount:
    """The account of the tracker `site_code` on the running event loop, made on first use."""
    loop = asyncio.get_running_loop()
    with _accounts_lock:
        # A closed loop sends nothing again; its accounts would otherwise be kept for good.
        for closed in [other for other in _accounts if other.is_closed()]:
            del _accounts[closed]
        accounts = _accounts.setdefault(loop, {})
        if site_code not in accounts:
            accounts[site_code] = TrackerAccount()
        return accounts[site_code]


async def _close_pools(loop: asyncio.AbstractEventLoop) -> None:
    """Close the pools opened on `loop`, the running one, and forget its accounts."""
    with _accounts_lock:
        accounts = _accounts.pop(loop, {})
    for account in accounts.values():
        pool, account.pool = account.pool, None
        if pool is not None:
            await pool.close()


# The loop every tracker request runs on while salmon web runs, whichever loop makes it. None in the CLI,
# where a command has one loop and its requests run where they are made.
_request_loop: asyncio.AbstractEventLoop | None = None


class _HandOver:
    """A call handed over to the request loop: its task there, and whether a cancel may still stop it."""

    def __init__(self, committed: bool) -> None:
        self.task: asyncio.Task | None = None
        # Set once the call may have reached the tracker: a cancel then waits for it to end.
        self.committed = committed


_hand_over: ContextVar[_HandOver | None] = ContextVar("tracker_hand_over", default=None)


@asynccontextmanager
async def requests_on_this_loop() -> AsyncIterator[None]:
    """Run every tracker request made during the block on the running loop, whichever loop makes it.

    For salmon web, whose jobs each run on a loop of their own (ADR 0004). The pools opened on this loop
    are closed when the block ends.
    """
    global _request_loop
    if _request_loop is not None:
        raise RuntimeError("tracker requests already run on another loop")
    loop = asyncio.get_running_loop()
    _request_loop = loop
    try:
        yield
    finally:
        _request_loop = None
        await _close_pools(loop)


def handing_over() -> bool:
    """Whether a tracker request made here runs on another loop: salmon web's, from a job's own loop."""
    loop = _request_loop
    return loop is not None and loop is not asyncio.get_running_loop()


def on_request_loop() -> bool:
    """Whether the running loop is salmon web's request loop, which closes its pools when it stops."""
    loop = _request_loop
    return loop is not None and loop is asyncio.get_running_loop()


def commit() -> None:
    """Note that the call handed over may reach the tracker from now on: a cancel no longer stops it.

    Called right before a request that is not idempotent goes out. Does nothing in a call not handed over.
    """
    hand_over = _hand_over.get()
    if hand_over is not None:
        hand_over.committed = True


def _settle(finished: asyncio.Future[asyncio.Task], task: asyncio.Task) -> None:
    if not finished.done():
        finished.set_result(task)


async def run_on_request_loop(
    call: Callable[[], Coroutine[Any, Any, T]], *, cancellable: bool = True, what: str = ""
) -> T:
    """Run `call()` on the request loop, in a copy of this context, and return what it returns.

    The copy carries the caller's context variables (a dry run, held request messages, a job's output)
    to the request. Cancelling the caller cancels the call at once, unless it has committed (see
    `commit`): the caller then waits for it to end, and is cancelled after. An UnknownOutcomeError it
    raises still reaches the caller, so its outcome is never lost.

    Args:
        call: Makes the coroutine to run there.
        cancellable: False for a call no cancel may stop part way.
        what: What the call sends, for the message shown when a cancel came after it went out.

    Raises:
        RequestFailedError: If salmon web stopped before the call could run or end.
    """
    loop = _request_loop
    if loop is None or loop is asyncio.get_running_loop():
        return await call()
    context = contextvars.copy_context()
    hand_over = _HandOver(committed=not cancellable)
    context.run(_hand_over.set, hand_over)
    caller_loop = asyncio.get_running_loop()
    finished: asyncio.Future[asyncio.Task] = caller_loop.create_future()

    def tell_caller(task: asyncio.Task) -> None:
        # A closed loop has no one waiting on it any more.
        with suppress(RuntimeError):
            caller_loop.call_soon_threadsafe(_settle, finished, task)

    def start() -> None:
        hand_over.task = loop.create_task(call(), context=context)
        hand_over.task.add_done_callback(tell_caller)

    def cancel_unless_committed() -> None:
        if hand_over.task is not None and not hand_over.committed:
            hand_over.task.cancel()

    try:
        loop.call_soon_threadsafe(start)
    except RuntimeError as err:
        raise RequestFailedError("salmon web is stopping: the request was not sent") from err

    cancelled: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(finished)
    except asyncio.CancelledError as err:
        # Raised again once the call has ended: anyio knows its own cancellations by this one's message.
        cancelled = err
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(cancel_unless_committed)
        # Shielded from anyio too, which cancels a task again on each step until it ends.
        with anyio.CancelScope(shield=True):
            while not finished.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(finished)
    task = finished.result()
    if cancelled is None:
        if task.cancelled():
            # Only salmon web stopping cancels a call no one cancelled.
            if hand_over.committed and what:
                raise UnknownOutcomeError(f"salmon web stopped before {what} was answered")
            if hand_over.committed:
                raise RequestFailedError("salmon web stopped")
            raise RequestFailedError("salmon web stopped: the request was not sent")
        return task.result()
    if not task.cancelled():
        error = task.exception()
        if isinstance(error, UnknownOutcomeError):
            raise error
        if hand_over.committed and what:
            click.secho(f"Cancelled once {what} had been answered: check the site for what it did.", fg="yellow")
    raise cancelled
