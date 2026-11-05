"""A server stopped just as a client connects leaves nothing to raise (tests/helpers/servers.py).

The race: asyncio has accepted the socket, but the task that builds its transport has not run when the server
closes. Garbage-collecting that half-built transport then wakes the closed server a second time, which raises
TypeError in asyncio's Server._wakeup on Python 3.13 and 3.14; pytest would report it as a warning, which fails the
run (`max_warnings = 0`).
"""

import asyncio
import gc
import socket
import sys
from asyncio import base_events

import pytest
from aiohttp import web
from helpers import servers


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


def test_without_the_helper_the_race_raises(monkeypatch):
    # What the helper is for: when this Python no longer raises, remove tests/helpers/servers.py.
    monkeypatch.setattr(base_events.Server, "_wakeup", servers.original_wakeup())
    if not asyncio.run(_race()):
        pytest.skip(f"Python {sys.version_info[:3]} does not raise here: once no CI Python does, remove the helper")


def test_the_race_leaves_nothing_to_raise():
    assert asyncio.run(_race()) == []
