"""An asyncio Server woken twice does not raise (installed by tests/conftest.py; tests/test_helpers_servers.py).

asyncio accepts a connection in two steps: a reader callback accepts the socket, then a task builds its transport
and attaches it to the Server. On Python 3.13 and 3.14, a server closed between the two leaves that transport
half-built: `_attach` fails its assert, but the transport keeps its socket and server, and when it is
garbage-collected its `__del__` closes the socket and detaches it, which wakes the closed server a second time:
`_wakeup` iterates `_waiters`, set to None by the first, and raises TypeError. pytest reports that as a warning,
which fails the run (`max_warnings = 0`), at random. A client connecting as a server stops does this: salmon
cancelling a request mid-connect as its run ends (an authentication no request waits for any more, #468), as a
test's fake stops.

So, on those Pythons, `_wakeup` does nothing once it has run: what it does has been done. Not a `filterwarnings`
entry: one matching this warning would hide any exception raised in a transport's `__del__`, this only the second
wakeup. Remove it once no Python CI runs raises (the first test in tests/test_helpers_servers.py then skips).
"""

import sys
from asyncio import base_events
from collections.abc import Callable
from typing import Any

_original_wakeup: Callable[[Any], None] | None = None


def _wakeup_once(self: Any) -> None:
    if self._waiters is not None:
        assert _original_wakeup is not None
        _original_wakeup(self)


def original_wakeup() -> Callable[[Any], None]:
    """Server._wakeup as asyncio has it, installed or not."""
    return _original_wakeup or base_events.Server._wakeup  # type: ignore[attr-defined]


def install() -> None:
    global _original_wakeup
    if sys.version_info < (3, 13) or _original_wakeup is not None:
        return
    _original_wakeup = base_events.Server._wakeup  # type: ignore[attr-defined]
    base_events.Server._wakeup = _wakeup_once  # type: ignore[attr-defined]
