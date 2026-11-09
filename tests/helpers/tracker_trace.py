"""The harness of tests/test_uploader_tracker_trace.py: `salmon up` through run.main, and what each tracker saw.

A fake server per tracker, so each has its own request sequence; a recording image host and seedbox; a fake clock.
The audio tools, the metadata source and the review are stubbed as in helpers.uploader_dry_run. Everything else is
the real upload. No real tracker, no credentials, nothing wall-clock dependent.
"""

import asyncio
import hashlib
import io
import itertools
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
from aiohttp import web
from tenacity import wait_none
from test_run_exit_status import _exit_of_main
from torf import Torrent

import salmon.images
import salmon.run
import salmon.trackers
import salmon.uploader
from helpers.uploader_dry_run import (
    AUTHKEY,
    GROUP_ID,
    RENAMED,
    SOURCE_URL,
    FakeTracker,
    Sent,
    _album,
    _client,
    _form_fields,
    _group,
    _success,
    _torrent_lines,
    fake_upload_world,
)
from helpers.webui_jobs import _log, _with_app
from helpers.webui_upload import _drive, _start
from salmon import cfg
from salmon.config.validations import Seedbox
from salmon.images.base import BaseImageUploader
from salmon.release_notification import get_version
from salmon.trackers import base
from salmon.uploader import conversion_upload, record, seedbox
from salmon.uploader.seedbox import UploadManager

_QUEUE_A_SEEDBOX_TASK = UploadManager.add_upload_task
ANY_STDIN = "\n" * 30
# A group on the tracker holding the release's CD, not its WEB: uploading into it is no dupe.
CD_FLAC = {**_group()["torrents"][0], "id": 13, "media": "CD"}
GROUP_ROW = {
    "groupId": GROUP_ID,
    "groupName": "Album",
    "groupYear": 2020,
    "artist": "Artist",
    "releaseType": "Album",
    "tags": [],
    "torrents": [CD_FLAC],
}
REQUEST_ID = 901
REQUEST_ROW = {
    "requestId": REQUEST_ID,
    "artists": [[{"name": "Artist"}]],
    "title": "Album",
    "year": 2020,
    "releaseType": "Album",
    "bitrateList": ["Lossless"],
    "formatList": ["FLAC"],
    "mediaList": ["WEB"],
    "categoryName": "Music",
}
REQUEST = {
    **REQUEST_ROW,
    "musicInfo": {"artists": [{"name": "Artist"}]},
    "requestorName": "someone",
    "bounty": 1024,
    "bbDescription": "want",
}


@dataclass
class Seen(Sent):
    """A request a tracker's server got: how it was authenticated, and what it was answered."""

    auth: str = "no auth"
    status: int = 200
    order: int = 0


@dataclass(frozen=True)
class Site:
    """How one tracker answers. Anything left out answers as helpers.uploader_dry_run's fake does."""

    browse: tuple[dict[str, Any], ...] = ()
    requests: tuple[dict[str, Any], ...] = ()
    group_torrents: tuple[dict[str, Any], ...] | None = None
    # The status to answer a GET with, by its ajax action or its path.
    fail: Mapping[str, int] = field(default_factory=dict)
    # The answer to each upload POST in turn: None as usual, or "refused", "lost" (a 500) or "no group".
    uploads: tuple[str | None, ...] = ()
    cookie: str = ""


class Server(FakeTracker):
    """One tracker on a port of its own, keeping what it got in `sent`, numbered by a counter its run shares."""

    def __init__(self, site: Site, counter: Any) -> None:
        super().__init__(list(site.group_torrents) if site.group_torrents is not None else None)
        self.site = site
        self._counter = counter
        self._uploads = itertools.count()

    def _auth_kind(self, request: web.Request) -> str:
        if request.headers.get("Authorization"):
            return "API key"
        return "cookie" if request.cookies.get("session") else "no auth"

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        fields = await _form_fields(request)
        seen = Seen(
            request.method,
            request.path,
            dict(request.query),
            fields,
            auth=self._auth_kind(request),
            order=next(self._counter),
        )
        self.sent.append(seen)
        try:
            answer = self._scripted(request, fields)
            if answer is None:
                answer = self._usual_answer(request, fields)
        except web.HTTPException as redirect:
            seen.status = redirect.status
            raise
        seen.status = answer.status
        return answer

    def _scripted(self, request: web.Request, fields: list[tuple[str, Any]]) -> web.StreamResponse | None:
        """The answer the scenario sets for this request, else None for the usual one."""
        action = request.query.get("action") or request.path
        if action in self.site.fail:
            return web.Response(status=self.site.fail[action], text="Bad gateway")
        if action == "browse" and self.site.browse:
            return _success({"results": list(self.site.browse)})
        if action == "requests" and self.site.requests:
            return _success({"results": list(self.site.requests)})
        if action == "request":
            return _success(REQUEST)
        if action == "torrent":
            # The infohash lookup after a lost answer: the tracker does not have it.
            return web.json_response({"status": "failure", "error": "bad hash"}, status=400)
        if action == "edit":
            return web.Response(text=_edit_form(request.query["id"]), content_type="text/html")
        if action == "upload" and request.method == "POST":
            return self._upload_answer(dict(fields).get("groupid"))
        return None

    def _upload_answer(self, group_id: str | None) -> web.StreamResponse | None:
        count = next(self._uploads)
        kind = self.site.uploads[count] if count < len(self.site.uploads) else None
        if kind == "refused":
            return web.json_response({"status": "failure", "error": "Your upload was refused."})
        if kind == "lost":
            return web.Response(status=500, text="Internal Server Error")
        torrent_id, self._next_torrent_id = self._next_torrent_id, self._next_torrent_id + 1
        if kind == "no group":
            return _success({"torrentid": torrent_id})
        # Into the group the form named: its own `groupid` is what a tracker answers.
        return _success({"torrentid": torrent_id, "groupid": int(group_id) if group_id else GROUP_ID})


def _edit_form(torrent_id: str) -> str:
    """torrents.php?action=edit as a browser gets it: the description to edit, the authkey and the torrent's ID."""
    return (
        '<html><form name="torrent" action="torrents.php" method="post">'
        '<input type="hidden" name="action" value="takeedit">'
        f'<input type="hidden" name="torrentid" value="{torrent_id}">'
        f'<input type="hidden" name="auth" value="{AUTHKEY}">'
        '<textarea name="release_desc">Uploaded before.</textarea>'
        "</form></html>"
    )


@asynccontextmanager
async def all_serving(servers: Mapping[str, Server]) -> AsyncIterator[None]:
    """Every server of the run listening."""
    stack = [server.serving() for server in servers.values()]
    for server in stack:
        await server.__aenter__()
    try:
        yield
    finally:
        for server in reversed(stack):
            await server.__aexit__(None, None, None)


class _Answers(io.StringIO):
    """The stdin of a run, whose answers show in stdout, after the prompt that took them."""

    def readline(self, *args: Any) -> str:
        line = super().readline(*args)
        sys.stdout.write(f"<answered: {line.rstrip()!r}>\n" if line else "<stdin closed>\n")
        return line


class _Clock:
    """Stands in for a module's asyncio or anyio: a sleep is noted, not waited for."""

    def __init__(self, module: Any, slept: list[float]) -> None:
        self._module = module
        self._slept = slept

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    async def sleep(self, seconds: float, *_args: Any, **_kwargs: Any) -> None:
        self._slept.append(seconds)
        await self._module.sleep(0)


@dataclass(frozen=True)
class Scenario:
    """One run of `salmon up`, or several on the same trackers.

    Attributes:
        about: What it shows, as #982's table says it.
        trackers: The trackers configured, in order. Without `defaults`, each named with -t.
        defaults: tracker.default_trackers instead of -t: the others are then offered.
        yes_all: Run with -yyy.
        args: More arguments for `salmon up`.
        stdin: What the prompts read.
        sites: How each tracker answers.
        fakes: Called for each run: more of the uploader's seams to replace, by name.
        setup: Called with the monkeypatch and the album folder, before the run.
        multi_tracker: upload.multi_tracker_upload.
        at_final_name: The album is already at the name salmon would rename it to.
        runs: How many times to run it on the same trackers.
        copy_exit: What rclone exits with for each copy.
        web: Run it as a salmon web upload job instead, the prompts answered by `web_answers`.
    """

    about: str
    trackers: tuple[str, ...] = ("RED", "OPS")
    defaults: tuple[str, ...] | None = None
    yes_all: bool = True
    args: tuple[str, ...] = ()
    stdin: str = ANY_STDIN
    sites: Mapping[str, Site] = field(default_factory=dict)
    fakes: Callable[[], dict[str, Any]] = dict
    setup: Callable[..., None] | None = None
    multi_tracker: bool = True
    at_final_name: bool = False
    runs: int = 1
    copy_exit: int = 0
    web: bool = False
    web_answers: tuple[Any, ...] = ()


@dataclass
class World:
    """What the run's fakes noted, beside what its servers got."""

    servers: dict[str, Server]
    images: list[str] = field(default_factory=list)
    seedbox: list[str] = field(default_factory=list)
    logins: list[str | None] = field(default_factory=list)
    keep_reasons: list[str | None] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    records: list[record.RunRecord] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)


def _install(monkeypatch, scenario: Scenario, torrents: Path, world: World) -> None:
    """Set up the fakes of one run: the servers, the image host, the seedbox and the clock."""
    fake_tracker = next(iter(world.servers.values()))
    _unused, _queued, world_logins = fake_upload_world(
        monkeypatch, torrents, tracker=fake_tracker, multi_tracker_upload=scenario.multi_tracker, **scenario.fakes()
    )
    world.logins = world_logins

    def client(code: str) -> Any:
        site = _client(salmon.trackers.tracker_classes[code], world.servers[code], torrents)
        site.cookie = world.servers[code].site.cookie
        return site

    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: lambda: client(code))
    monkeypatch.setattr(salmon.trackers, "tracker_list", list(scenario.trackers))
    monkeypatch.setattr(cfg.tracker, "default_trackers", list(scenario.defaults or ()))
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    _install_image_host(monkeypatch, world)
    _install_seedbox(monkeypatch, scenario, world)
    _install_clock(monkeypatch, world)

    real_remove = salmon.uploader.remove_source_folder

    async def remove_source_folder(*args: Any, **kwargs: Any) -> Any:
        world.removals.append(_removal_line(args, kwargs))
        return await real_remove(*args, **kwargs)

    monkeypatch.setattr(salmon.uploader, "remove_source_folder", remove_source_folder)

    @contextmanager
    def recording():
        with record.recording() as run:
            world.records.append(run)
            yield run

    monkeypatch.setattr(salmon.run, "recording", recording)
    monkeypatch.setattr(salmon.run, "show_release_notification", lambda: None)
    monkeypatch.setattr(salmon.run, "cleanup_tmp_dir", lambda: None)
    # Process-wide: what these runs learn stays out of other tests, and the other way round.
    monkeypatch.setattr(base, "_learned_secrets", set())


def _removal_line(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """What upload() handed remove_source_folder: the run's result, beside the folder it started from."""
    _path, final, original = args
    return f"remove_source_folder: final={final} original={original} {' '.join(f'{k}={v}' for k, v in kwargs.items())}"


def _install_image_host(monkeypatch, world: World) -> None:
    """A fake image host that notes what it takes. Each image's URL is its name: the uploads run together."""

    class ImageUploader(BaseImageUploader):
        async def upload_file(self, filename: str) -> tuple[str, None]:
            name = os.path.basename(filename)
            world.images.append(name)
            return f"https://images.test/{name.replace(' ', '_')}", None

    monkeypatch.setitem(salmon.images.HOSTS, "testhost", SimpleNamespace(ImageUploader=ImageUploader))
    monkeypatch.setattr(cfg.image, "cover_uploader", "testhost")
    monkeypatch.setattr(cfg.image, "specs_uploader", "testhost")


def _install_seedbox(monkeypatch, scenario: Scenario, world: World) -> None:
    """A real UploadManager and one seedbox; rclone and the torrent client note what they are asked."""
    monkeypatch.setattr(
        cfg,
        "seedbox",
        [Seedbox(name="box", type="rclone", url="box", torrent_client="qbittorrent+http://127.0.0.1:9")],
    )
    monkeypatch.setattr(UploadManager, "add_upload_task", _QUEUE_A_SEEDBOX_TASK)

    async def run_rclone(commands: list[str], _secrets: Any) -> int:
        world.seedbox.append(f"copy {commands[2]} -> {commands[3]}")
        return scenario.copy_exit

    async def add_to_downloader(_client: Any, shell_path: str, torrent_path: str, *_rest: Any, **_kw: Any) -> bool:
        world.seedbox.append(f"add {torrent_path} -> {shell_path}")
        return True

    monkeypatch.setattr(seedbox, "_run_rclone", run_rclone)
    monkeypatch.setattr(seedbox, "_add_to_downloader", add_to_downloader)

    real_remove_local_copies = UploadManager.remove_local_copies

    def remove_local_copies(self: UploadManager, keep_because: str | None = None) -> None:
        world.keep_reasons.append(keep_because)
        real_remove_local_copies(self, keep_because)

    monkeypatch.setattr(UploadManager, "remove_local_copies", remove_local_copies)


def _install_clock(monkeypatch, world: World) -> None:
    """Waits are noted and not waited for: the lost-upload lookups, a 429's wait, the retries' backoff."""
    monkeypatch.setattr(base, "asyncio", _Clock(asyncio, world.slept))
    monkeypatch.setattr(base.BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]
    monkeypatch.setattr(conversion_upload, "anyio", _Clock(anyio, []))


# The trace


def _sorted_group_search(sent: list[Sent]) -> list[Sent]:
    """The group search's GETs go out together, so their order among themselves is not part of the trace."""
    result: list[Sent] = []
    for group_search, requests in itertools.groupby(sent, key=lambda seen: seen.query.get("action") == "browse"):
        members = list(requests)
        result += sorted(members, key=lambda seen: seen.query["searchstr"]) if group_search else members
    return result


def _field_lines(name: str, value: Any) -> list[str]:
    if isinstance(value, tuple):
        filename, content = value
        if name == "file_input":
            torrent = Torrent.read_stream(content)
            announce = ", ".join(tier[0].split("/")[2] for tier in torrent.trackers)
            hashed = f"{filename}: infohash {torrent.infohash}, private {torrent.private}, announce {announce}"
            return [f"  {name}: {hashed}", *_torrent_lines(content)]
        return [f"  {name}: {filename}, sha256 {hashlib.sha256(content).hexdigest()}"]
    first, *more = str(value).split("\n")
    return [f"  {name}: {first}", *(f"      {line}" for line in more)]


def _request_lines(seen: Seen) -> list[str]:
    query = " ".join(f"{name}={seen.query[name]}" for name in sorted(seen.query) if name != "auth")
    answered = f" -> {seen.status}" if seen.status != 200 else ""
    lines = [f"{seen.method} {seen.path} {query} [{seen.auth}]{answered}"]
    for name, value in seen.fields:
        if name != "auth":
            lines += _field_lines(name, value)
    return lines


def tracker_lines(server: Server) -> list[str]:
    """What this tracker's server got, in the order it got it."""
    return [line for seen in _sorted_group_search(server.sent) for line in _request_lines(_as_seen(seen))]


def _as_seen(sent: Sent) -> Seen:
    assert isinstance(sent, Seen)
    return sent


def _record_lines(run: record.RunRecord, exit_code: Any, keep_reason: Any, removal: str) -> list[str]:
    outcomes = [f"unknown outcome: {outcome}" for outcome in run.unknown_outcomes]
    uploads = [f"uploaded: {up['tracker']} {up['format']} {up['url']}" for up in run.uploads]
    failures = [f"seedbox failure: {failure}" for failure in run.seedbox_failures]
    return [
        f"exit: {exit_code}",
        f"sent: {run.sent}, complete: {run.complete}",
        *uploads,
        *outcomes,
        *failures,
        f"remove_local_copies reason: {keep_reason}",
        removal or "remove_source_folder: not reached",
    ]


def _files_lines(tmp: Path) -> list[str]:
    return sorted(str(entry.relative_to(tmp)) for entry in tmp.rglob("*") if entry.is_file())


def _torrent_file_lines(torrents: Path) -> list[str]:
    lines = []
    for path in sorted(torrents.glob("*.torrent")):
        torrent = Torrent.read(path)
        lines.append(f"{path.name}: comment {torrent.comment}, infohash {torrent.infohash}")
    return lines


def _clean_output(text: str, tmp: Path, world: World) -> str:
    """The run's own text, its addresses and paths and the one that changes every release made neutral."""
    for code, server in world.servers.items():
        text = text.replace(server.url, f"<{code}>")
    for path in {str(tmp), os.path.realpath(tmp)}:
        text = text.replace(path, "<tmp>")
    text = text.replace(str(get_version()), "<version>")
    # A scratch folder's random name.
    return re.sub(r"(\.salmon-staging/run-|salmon-[a-z-]*-)[a-z0-9_]{6,}", r"\1<random>", text)


def _stderr_lines(text: str) -> list[str]:
    """What the run printed to stderr, without its tracebacks' frames: their line numbers move with any change."""
    return [line for line in text.splitlines() if line.strip() and not line.startswith(("  ", "Traceback", "|", "+"))]


def trace_text(world: World, tmp: Path, torrents: Path, head: list[str], out: list[str], err: list[str]) -> str:
    """The trace of a run: each section under a `== name ==` line, the text normalised."""
    sections = {
        "run": [*head, f"waited (s): {world.slept}"],
        **{code: tracker_lines(server) for code, server in world.servers.items()},
        "images": sorted(world.images),
        "seedbox": [*(f"login {url}" for url in world.logins), *world.seedbox],
        "torrent files": _torrent_file_lines(torrents),
        "files": _files_lines(tmp),
        "stdout": out,
        "stderr": err,
    }
    text = "".join(
        f"== {name} ==\n" + "".join(f"{line}\n" for line in lines) + "\n" for name, lines in sections.items()
    )
    return _clean_output(text, tmp, world)


def order_across_trackers(world: World) -> str:
    """The trackers in the order their requests came, the runs of one collapsed. Reported beside the trace, never
    compared: nothing fixes the order of requests sent together to different trackers."""
    pairs = sorted(
        ((_as_seen(sent).order, code) for code, server in world.servers.items() for sent in server.sent),
    )
    runs = [(code, len(list(group))) for code, group in itertools.groupby(code for _order, code in pairs)]
    return ", ".join(f"{code} {count}" for code, count in runs) or "no requests"


def run_scenario(monkeypatch, capsys, scenario: Scenario, dirs: tuple[Path, Path]) -> tuple[str, World]:
    """Run a scenario through run.main or salmon web's upload job, and give its trace and what its fakes noted."""
    downloads, torrents = dirs
    tmp = downloads.parent
    counter = itertools.count()
    world = World({code: Server(scenario.sites.get(code, Site()), counter) for code in scenario.trackers})
    _install(monkeypatch, scenario, torrents, world)
    album_folder = downloads / RENAMED if scenario.at_final_name else tmp / "seeding" / "Album"
    album = _album(downloads / "Album" if scenario.web else album_folder)
    if scenario.setup:
        scenario.setup(monkeypatch, album)
    if scenario.web:
        head, out, err = _run_job(scenario, world, album)
    else:
        head, out, err = _run_commands(monkeypatch, capsys, scenario, world, album)
    return trace_text(world, tmp, torrents, head, out, err), world


def _run_commands(
    monkeypatch, capsys, scenario: Scenario, world: World, album: Path
) -> tuple[list[str], list[str], list[str]]:
    """`salmon up` through run.main, as many times as the scenario asks."""
    named = [] if scenario.defaults else [arg for code in scenario.trackers for arg in ("-t", code)]
    argv = [
        "salmon",
        "up",
        str(album),
        *named,
        "-s",
        "WEB",
        "-n",
        *(["-yyy"] if scenario.yes_all else []),
        "--skip-integrity-check",
        "--skip-up",
        "--source-url",
        SOURCE_URL,
        *scenario.args,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    exits: list[Any] = []
    out: list[str] = []
    err: list[str] = []

    async def run() -> None:
        async with all_serving(world.servers):
            for _ in range(scenario.runs):
                monkeypatch.setattr(sys, "stdin", _Answers(scenario.stdin))
                exits.append(await anyio.to_thread.run_sync(_exit_of_main))
                captured = capsys.readouterr()
                out.extend(click.unstyle(captured.out).splitlines())
                err.extend(_stderr_lines(click.unstyle(captured.err)))

    anyio.run(run)
    head = [f"salmon {' '.join(argv[1:])}"]
    for i, (exit_code, run_record) in enumerate(zip(exits, world.records, strict=True)):
        if scenario.runs > 1:
            head.append(f"--- run {i + 1}")
        head += _record_lines(
            run_record,
            exit_code,
            world.keep_reasons[i] if i < len(world.keep_reasons) else "not reached",
            world.removals[i] if i < len(world.removals) else "",
        )
    return head, out, err


def _run_job(scenario: Scenario, world: World, album: Path) -> tuple[list[str], list[str], list[str]]:
    """The scenario as a salmon web upload job, its prompts answered in turn from `web_answers`, else by default."""
    answers = iter(scenario.web_answers)
    params = {"trackers": list(scenario.trackers)}
    finished: list[Any] = []
    questions: list[str] = []

    async def test(client: Any, manager: Any) -> None:
        def answer(question: dict[str, Any]) -> Any:
            value = next(answers, "")
            questions.append(f"asked: {click.unstyle(question['text']).strip()} <answered: {value!r}>")
            return value

        async with all_serving(world.servers):
            job, _asked = await _drive(
                client, manager, await _start(client, album, params=params, assume_defaults=scenario.yes_all), answer
            )
        finished.append(job)

    _with_app(test)
    job = finished[0]
    keep = [f"remove_local_copies reason: {world.keep_reasons[0] if world.keep_reasons else 'not reached'}"]
    head = [
        f"salmon web upload job: {json.dumps(params, sort_keys=True)}",
        f"status: {job.status}",
        f"error: {job.error}",
        f"result: {json.dumps(job.result, sort_keys=True)}",
        *keep,
        world.removals[0] if world.removals else "remove_source_folder: not reached",
        *questions,
    ]
    return head, [click.unstyle(line) for line in _log(job)], []
