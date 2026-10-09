"""What every client of one tracker account shares: its rate limit, its connection pool and its lock.

A tracker limits the account, not a client object, and a command may hold several clients of one
tracker (ADR 0004, amending ADR 0001). Each tracker has its own. The state is kept per event loop, as
an asyncio lock or an aiohttp session cannot be used from another loop.
"""

import asyncio
import threading
import time
from collections import deque
from typing import TYPE_CHECKING
from weakref import WeakSet

if TYPE_CHECKING:
    import aiohttp

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
