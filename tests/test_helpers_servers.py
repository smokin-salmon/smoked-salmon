"""A server stopped just as a client connects leaves nothing to raise (tests/helpers/servers.py).

The race: asyncio has accepted the socket, but the task that builds its transport has not run when the server
closes. Garbage-collecting that half-built transport then wakes the closed server a second time, which raises
TypeError in asyncio's Server._wakeup on Python 3.13, and on 3.14 and later before their fix (FIXED_IN below);
pytest would report it as a warning, which fails the run (`max_warnings = 0`).
"""

import asyncio
import gc
import socket
import sys
from asyncio import base_events

import pytest
from aiohttp import web
from helpers import servers

# The first release of each Python that closes a connection accepted after its server closed, before building its
# transport (python/cpython#160851, merged 2026-10-29). 3.14.10, 3.15.2 and 3.16.0a1, the latest when it merged, do
# not have it. 3.13 got no backport; 3.17 and later have it from the start.
FIXED_IN: dict[tuple[int | str, ...], tuple[int | str, ...]] = {
    (3, 14): (3, 14, 11),
    (3, 15): (3, 15, 3),
    (3, 16): (3, 16, 0, "alpha", 2),
}


def has_the_race(version: tuple[int | str, ...] = tuple(sys.version_info)) -> bool:
    """Whether a server closed as a client connects raises TypeError on this Python with asyncio's own _wakeup
    (3.11 and 3.12 count connections: they raise nothing)."""
    minor = version[:2]
    return minor == (3, 13) or (minor in FIXED_IN and version < FIXED_IN[minor])


async def _race() -> list[TypeError]:
    """Stop a server between asyncio's accept of a connection and the building of its transport: the TypeErrors."""
    loop = asyncio.get_running_loop()
    accepted = asyncio.Event()
    accept = loop._accept_connection  # type: ignore[attr-defined]

    def wake_the_test_then_accept(*args, **kwargs):
        # The test then resumes before the accept task, so it stops the server before the transport is built.
        accepted.set()
        accept(*args, **kwargs)

    gc.collect()  # what earlier tests left is not part of this
    raised: list[TypeError] = []
    hook = sys.unraisablehook

    def keep_type_errors(unraisable) -> None:
        if isinstance(unraisable.exc_value, TypeError):
            raised.append(unraisable.exc_value)
        else:
            hook(unraisable)  # pytest still reports anything else

    sys.unraisablehook = keep_type_errors
    gc_was_enabled = gc.isenabled()
    gc.disable()  # collected only below, while the hook is ours
    loop._accept_connection = wake_the_test_then_accept  # type: ignore[attr-defined]
    runner = web.AppRunner(web.Application())
    cleaned_up = False
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        with socket.create_connection(runner.addresses[0][:2]):
            await asyncio.wait_for(accepted.wait(), 5)
            await runner.cleanup()
            cleaned_up = True
        gc.collect()
    finally:
        del loop._accept_connection  # type: ignore[attr-defined]
        if not cleaned_up:
            await runner.cleanup()
        if gc_was_enabled:
            gc.enable()
        sys.unraisablehook = hook
    return raised


def _run_race() -> list[TypeError]:
    # On a selector loop, whose accept is the two steps above (Linux and macOS use one by default). Windows' default
    # proactor loop accepts otherwise and is not covered here.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(_race())


def test_without_the_helper_the_race_raises_where_expected(monkeypatch):
    # What the helper is for, so this fails if a Python raises where FIXED_IN says it does not, or the other way. Once
    # no Python CI runs has the race, remove tests/helpers/servers.py, its install in conftest.py, and this file.
    monkeypatch.setattr(base_events.Server, "_wakeup", servers.original_wakeup())
    raised = _run_race()
    version = ".".join(map(str, sys.version_info[:3]))
    if has_the_race():
        assert raised, (
            f"the race raised nothing on Python {version}: either this test no longer stops the server between the "
            "accept and the transport (fix the test), or this Python has CPython's fix (add its first fixed release "
            "to FIXED_IN in this file)"
        )
    else:
        assert raised == [], (
            f"the race raised on Python {version}, which has_the_race() says has no race (3.11 and 3.12, or a release "
            "FIXED_IN lists as fixed): correct FIXED_IN, or has_the_race() for 3.11 and 3.12"
        )


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((3, 11, 15, "final", 0), False),
        ((3, 12, 14, "final", 0), False),
        ((3, 13, 14, "final", 0), True),
        ((3, 14, 10, "final", 0), True),
        ((3, 14, 11, "final", 0), False),
        ((3, 15, 2, "final", 0), True),
        ((3, 15, 3, "final", 0), False),
        ((3, 16, 0, "alpha", 1), True),
        ((3, 16, 0, "alpha", 2), False),
        ((3, 17, 0, "final", 0), False),
    ],
)
def test_which_pythons_have_the_race(version, expected):
    assert has_the_race(version) is expected


def test_the_race_leaves_nothing_to_raise():
    raised = _run_race()
    assert raised == []
