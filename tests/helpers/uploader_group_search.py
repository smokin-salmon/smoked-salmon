"""Helpers of tests/test_uploader_group_search.py that other test modules use too."""

import asyncio
import time
from typing import Any

import anyio
import pytest
from aiohttp import web

import salmon.trackers
import salmon.uploader
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import dupe_checker

GROUP = {
    "groupId": 5,
    "groupName": "Hits Vol. 2",
    "artist": "Artist",
    "groupYear": 2020,
    "releaseType": "Album",
    "tags": ["rock"],
    "torrents": [],
}


class FakeTracker:
    def __init__(self, browse_results: list | None = None, browse_status: int = 200, browse_delay: float = 0) -> None:
        self.browse_results = browse_results if browse_results is not None else [GROUP]
        self.browse_status = browse_status
        self.browse_delay = browse_delay
        self.hits: list[str] = []
        self.peers: dict[str, set[int]] = {}
        self.browse_arrived = anyio.Event()

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ajax.php", self.ajax)
        app.router.add_get("/log.php", self.log)
        app.router.add_get("/login.php", self.login)
        # A slow answer stops when the client hangs up, as a real server's would.
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    def _hit(self, request: web.Request, what: str) -> None:
        self.hits.append(what)
        assert request.transport is not None
        instance = request.headers.get("X-Instance", "")
        self.peers.setdefault(instance, set()).add(request.transport.get_extra_info("peername")[1])

    async def ajax(self, request: web.Request) -> web.Response:
        action = request.query["action"]
        response: dict[str, Any] = {}
        if action == "index":
            self._hit(request, "index")
            response = {"authkey": "a", "passkey": "p"}
        elif action == "browse":
            self._hit(request, f"browse {request.query['searchstr']}")
            self.browse_arrived.set()
            await asyncio.sleep(self.browse_delay)
            if self.browse_status != 200:
                return web.json_response({"status": "failure", "error": "bad search"}, status=self.browse_status)
            response = {"results": self.browse_results}
        elif action == "requests":
            self._hit(request, f"requests {request.query['search']}")
            response = {"results": []}
        elif action == "torrentgroup":
            self._hit(request, f"torrentgroup {request.query['id']}")
            response = {
                "group": {"id": 5, "name": "Hits Vol. 2", "year": 2020, "musicInfo": {"artists": [{"name": "A"}]}},
                "torrents": [],
            }
        return web.json_response({"status": "success", "response": response})

    async def log(self, request: web.Request) -> web.Response:
        self._hit(request, f"log {request.query['page']}")
        if request.cookies.get("session") != "good":
            raise web.HTTPFound("/login.php")
        return web.Response(text="<html></html>", content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        self._hit(request, "login")
        raise web.HTTPFound("/log.php")


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    tracker_url = "http://127.0.0.1"

    def __init__(self, base_url: str, cookie: str, api_key: str = "", instance: str = "upload") -> None:
        self.base_url = base_url
        self.cookie = cookie
        self.api_key = api_key
        super().__init__()
        self.headers["X-Instance"] = instance


class Flow:
    """upload() with every step that does not talk to the tracker stubbed out, and a log of what happened."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tracker: FakeTracker) -> None:
        self.monkeypatch = monkeypatch
        self.tracker = tracker
        self.events: list[str] = []
        # How the background search ended: "finished", "failed" or "cancelled".
        self.search = "not started"
        self.search_task: asyncio.Task | None = None
        # The checks the search overlaps: by default they take long enough for the search to be over.
        self.checks = self.wait_for_the_search_to_end
        self.api: FakeApi | None = None
        self.returned_at = 0.0

    def echo(self, event: str) -> None:
        self.events.append(event)
        salmon.uploader.click.echo(f"<<{event}>>")

    async def wait_for_the_search_to_arrive(self) -> None:
        with anyio.fail_after(5):
            await self.tracker.browse_arrived.wait()

    async def wait_for_the_search_to_end(self) -> None:
        with anyio.fail_after(5):
            while self.search in ("not started", "running"):
                await anyio.sleep(0.01)

    def install(self, path: str) -> None:
        flow = self
        search = dupe_checker.fetch_existing_group_candidates

        async def watched_search(*args, **kwargs):
            flow.search, flow.search_task = "running", asyncio.current_task()
            try:
                result = await search(*args, **kwargs)
            except anyio.get_cancelled_exc_class():
                flow.search = "cancelled"
                raise
            except Exception:
                flow.search = "failed"
                raise
            flow.search = "finished"
            return result

        def returning(result: Any = None):
            def fake(*_args, **_kwargs) -> Any:
                return result

            return fake

        def returning_async(result: Any = None):
            async def fake(*_args, **_kwargs) -> Any:
                return result

            return fake

        async def checks(*_args, **_kwargs) -> None:
            flow.echo("checks start")
            await flow.checks()
            flow.echo("checks end")

        async def check_spectrals(*_args, **_kwargs):
            flow.echo("spectral step")
            return False, None

        async def upload_and_report(*_args, **_kwargs):
            flow.echo("upload")
            return 1, 5, None, None, "url"

        async def prompt(text: str, *_args, **_kwargs) -> str:
            flow.echo("group prompt" if "[N]ew group" in text else f"prompt: {text}")
            return "n"

        class FakeUploadManager:
            async def execute_upload(self) -> None:
                pass

            def remove_local_copies(self, keep_because: str | None = None) -> None:
                pass

        rls_data = {"format": "FLAC", "encoding": "Lossless", "artists": [("Artist", "main")], "title": "Hits Vol. 2"}
        rls_data["catno"] = None
        metadata = {**rls_data, "cover": None}
        for name, fake in {
            "gather_audio_info": returning({}),
            "check_hybrid": returning(False),
            "standardize_tags": returning(),
            "gather_tags": returning({}),
            "construct_rls_data": returning_async(rls_data),
            "mqa_test": checks,
            "check_spectrals": check_spectrals,
            "get_metadata": returning_async((metadata, None)),
            "edit_metadata": returning_async((path, metadata, {}, {})),
            "concat_track_data": returning({}),
            "resolve_cover_url": returning_async((True, None)),
            "UploadManager": FakeUploadManager,
            "upload_and_report": upload_and_report,
        }.items():
            self.monkeypatch.setattr(salmon.uploader, name, fake)
        self.monkeypatch.setattr(dupe_checker, "fetch_existing_group_candidates", watched_search)
        self.monkeypatch.setattr(dupe_checker.click, "prompt", prompt)
        self.monkeypatch.setattr(salmon.uploader.click, "confirm", returning(False))
        self.monkeypatch.setattr(salmon.trackers, "choose_tracker", returning_async(None))
        # RED alone: no tracker follows it, so a failed search ends the run.
        self.monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
        self.monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)

    async def run(self, cookie: str = "good", api_key: str = "", path: str = "/release") -> None:
        self.install(path)
        self.api = FakeApi(await self.tracker.start(), cookie, api_key)
        try:
            await salmon.uploader.upload(self.api, path, None, "WEB", None, (), None)
        finally:
            self.returned_at = time.monotonic()
            await self.api.close()
            await self.tracker.runner.cleanup()
