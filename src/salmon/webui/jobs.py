"""salmon web's jobs: each in a worker thread with an event loop of its own (ADR 0004, section 1).

A step that blocks (hashing a torrent, copying an album, waiting for an answer) stalls its own job only, while the
server's loop goes on serving, and sending every job's tracker requests (see ``trackers.account``). At most
``[web] max_jobs`` jobs run at once; the others wait their turn in the order they came, and a job waits while
another works on the same album folder. A question left unanswered for 30 minutes stops its job. Jobs and their
history live in memory only, and so many of them: the oldest finished jobs are dropped.

Everything about a job is changed on the server's loop: its thread hands each change over. Every event goes through
the egress filter where it is made, so what is kept and sent holds no secret.

Ported from the fork's ``webui/jobs.py``: worker threads from styx-techno (7cd3173c), the bounds and the question
timeout from chodeus (18fc08d9). Unlike the fork, jobs past the limit wait instead of being refused, and a job whose
request may have reached the tracker ends with a status of its own.
"""

import asyncio
import concurrent.futures
import itertools
import os
import threading
import traceback
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import asyncclick as click
import msgspec

from salmon import dryrun, interaction
from salmon.errors import UnknownOutcomeError
from salmon.webui import output
from salmon.webui.egress import Redactor
from salmon.webui.interaction import NoAnswerError, WebInteraction

Status = Literal["queued", "running", "waiting", "done", "failed", "cancelled", "unknown_outcome"]
FINISHED: frozenset[Status] = frozenset({"done", "failed", "cancelled", "unknown_outcome"})

# A question left unanswered this long stops its job: there is no terminal to fall back to.
QUESTION_TIMEOUT = 30 * 60
UNKNOWN_OUTCOME = "The request may have reached the tracker: check the site before trying again."
# Finished jobs kept, oldest dropped first; jobs waiting their turn at most.
MAX_FINISHED_JOBS = 100
MAX_QUEUED_JOBS = 100
# Lines of a job's log kept, oldest dropped first.
MAX_LOG_LINES = 5000
# Live connections (browser tabs) at once, and events waiting for a slow one before it is cut off.
MAX_SUBSCRIBERS = 16
SUBSCRIBER_BACKLOG = 2000


@dataclass(frozen=True)
class JobKind:
    """Something salmon web can run as a job.

    Attributes:
        name: What a request to start one names.
        params: The msgspec struct its parameters are decoded into.
        run: Runs the job, in its thread; returns the result, which must be JSON.
        title: The job's title in the list.
        folder: The album folder it works on, if any: one job per folder at a time.
    """

    name: str
    params: type[msgspec.Struct]
    run: Callable[[Any], Awaitable[Any]]
    title: Callable[[Any], str]
    folder: Callable[[Any], str | None] = lambda _params: None


KINDS: dict[str, JobKind] = {}


def register(kind: JobKind) -> None:
    KINDS[kind.name] = kind


class JobError(Exception):
    """A job that cannot be started, with the HTTP status that says why."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> str:
    return datetime.now(UTC).isoformat()


_ids = itertools.count(1)


class Job:
    """A job's state. Changed on the server's loop only, but for the handles its thread reaches through `_lock`."""

    def __init__(
        self,
        kind: JobKind,
        params: msgspec.Struct,
        shown_params: Any,
        title: str,
        folder: str | None,
        dry_run: bool,
        assume_defaults: bool,
    ) -> None:
        self.id = f"job-{next(_ids)}"
        self.kind = kind
        self.params = params
        self.shown_params = shown_params
        self.title = title
        self.folder = folder
        self.dry_run = dry_run
        self.assume_defaults = assume_defaults
        self.status: Status = "queued"
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.error: str | None = None
        self.result: Any = None
        self.question: dict[str, Any] | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_LINES)
        # Lines logged so far, the dropped ones included.
        self.lines = 0
        # The folder and names of the spectral images the browser may fetch.
        self.spectrals: tuple[str, list[str]] | None = None
        # Reached from the job's thread too.
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._cancel_requested = False
        # The open question's id, as the job made it, and where its answer goes.
        self._question_id: str | None = None
        self._answer: concurrent.futures.Future[Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.name,
            "title": self.title,
            "params": self.shown_params,
            "dry_run": self.dry_run,
            "assume_defaults": self.assume_defaults,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "question": self.question,
            "spectrals": self.spectrals[1] if self.spectrals else None,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "log": list(self.log)}


class _Subscriber:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.cut_off = False


def _leaves(error: BaseException) -> list[BaseException]:
    """An exception group's exceptions, nested ones included; or the exception itself."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for each in error.exceptions for leaf in _leaves(each)]
    return [error]


def _unknown_outcome(error: BaseException) -> UnknownOutcomeError | None:
    """The UnknownOutcomeError behind `error`, raised as it is, in a group or as the cause of another."""
    for leaf in _leaves(error):
        seen: BaseException | None = leaf
        while seen is not None:
            if isinstance(seen, UnknownOutcomeError):
                return seen
            seen = seen.__cause__ or seen.__context__
    return None


class _JobAsker:
    """A job's questions, from its thread to the browser and back."""

    def __init__(self, manager: "JobManager", job: Job, lines: output.Lines) -> None:
        self._manager = manager
        self._job = job
        self._lines = lines

    async def ask(self, question: dict[str, Any]) -> Any:
        # What the job printed before the question, so the log reads in order.
        self._lines.flush()
        answer: concurrent.futures.Future[Any] = concurrent.futures.Future()
        question = {"id": f"{self._job.id}-q{next(_ids)}", **question}
        self._manager._call(self._manager._open_question, self._job, question, answer)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(answer), QUESTION_TIMEOUT)
        except TimeoutError:
            minutes = QUESTION_TIMEOUT // 60
            click.secho(f"No answer for {minutes} minutes: the job stopped.", fg="red")
            raise NoAnswerError() from None
        finally:
            answer.cancel()
            self._manager._call(self._manager._close_question, self._job, question["id"])

    def show_spectrals(self, folder: str, files: list[str]) -> None:
        self._manager._call(self._manager._show_spectrals, self._job, os.path.realpath(folder), files)


class JobManager:
    """The jobs of one salmon web server. Its methods run on the server's loop."""

    def __init__(self, max_jobs: int, redactor: Redactor) -> None:
        self.max_jobs = max_jobs
        self.jobs: dict[str, Job] = {}
        self._redactor = redactor
        self._queue: list[Job] = []
        self._running: set[Job] = set()
        self._subscribers: set[_Subscriber] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._idle = asyncio.Event()
        self._idle.set()

    def open(self) -> None:
        """Take jobs, run on the running loop."""
        self._loop = asyncio.get_running_loop()

    async def stop(self, timeout: float = 30) -> None:
        """Cancel every job, and wait for the running ones to end, up to `timeout` seconds."""
        self._stopping = True
        for job in list(self._queue):
            self.cancel(job.id)
        for job in list(self._running):
            self.cancel(job.id)
        try:
            async with asyncio.timeout(timeout):
                await self._idle.wait()
        except TimeoutError:
            names = ", ".join(sorted(job.id for job in self._running))
            print(f"salmon web: stopped while jobs were still running: {names}", file=output.real_stderr())

    # --- Starting, cancelling, answering ---------------------------------------

    def start(self, kind_name: str, params: Any, *, dry_run: bool = False, assume_defaults: bool = False) -> Job:
        """Start a job of the kind named, or queue it.

        Raises:
            JobError: An unknown kind, parameters it does not take, too many jobs waiting, or the server stopping.
        """
        if self._stopping:
            raise JobError(503, "salmon web is stopping.")
        kind = KINDS.get(kind_name)
        if kind is None:
            raise JobError(400, f"Unknown job kind: {self._redactor.text(kind_name)}")
        try:
            decoded = msgspec.convert(params, kind.params)
        except msgspec.ValidationError as e:
            raise JobError(400, self._redactor.text(f"Invalid parameters: {e}")) from None
        if len(self._queue) >= MAX_QUEUED_JOBS:
            raise JobError(429, f"{MAX_QUEUED_JOBS} jobs are already waiting: try again once some have run.")
        folder = kind.folder(decoded)
        job = Job(
            kind,
            decoded,
            self._redactor.value(msgspec.to_builtins(decoded)),
            self._redactor.text(kind.title(decoded)),
            os.path.realpath(folder) if folder else None,
            dry_run,
            assume_defaults,
        )
        self.jobs[job.id] = job
        self._queue.append(job)
        self._publish({"event": "created", "job": job.summary()})
        self._schedule()
        self._prune()
        return job

    def cancel(self, job_id: str) -> bool:
        """Cancel a job that has not finished. False if there is none."""
        job = self.jobs.get(job_id)
        if job is None or job.status in FINISHED:
            return False
        if job in self._queue:
            self._queue.remove(job)
            self._finish(job, "cancelled", None, None)
            return True
        with job._lock:
            job._cancel_requested = True
            loop, task = job._loop, job._task
        if loop is not None and task is not None:
            # A closed loop: the job has ended meanwhile.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        return True

    def answer(self, job_id: str, question_id: str, value: Any) -> bool:
        """Answer a job's open question. False if it has no question of that id open."""
        job = self.jobs.get(job_id)
        if job is None or job._question_id != question_id or job._answer is None:
            return False
        try:
            job._answer.set_result(value)
        except concurrent.futures.InvalidStateError:
            return False
        return True

    # --- Events ------------------------------------------------------------------

    def subscribe(self) -> _Subscriber | None:
        """A queue of every event from now on, or None if too many connections listen already."""
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            return None
        subscriber = _Subscriber()
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def _publish(self, event: dict[str, Any]) -> None:
        """Send an event to every connection. Its content was filtered when it was made."""
        for subscriber in list(self._subscribers):
            if subscriber.events.qsize() >= SUBSCRIBER_BACKLOG:
                # A connection that does not keep up is cut off: the browser connects again and reloads the jobs,
                # where dropping some events would leave it showing a log with holes.
                self._subscribers.discard(subscriber)
                subscriber.cut_off = True
                subscriber.events.put_nowait(None)
            else:
                subscriber.events.put_nowait(event)

    # --- Running jobs --------------------------------------------------------------

    def _schedule(self) -> None:
        """Start the jobs waiting, in order, while fewer than max_jobs run; a job waits while its folder is busy."""
        for job in list(self._queue):
            if len(self._running) >= self.max_jobs:
                break
            if job.folder is not None and any(other.folder == job.folder for other in self._running):
                continue
            self._queue.remove(job)
            self._running.add(job)
            self._idle.clear()
            job.status = "running"
            job.started_at = _now()
            self._publish({"event": "status", "job_id": job.id, "status": job.status, "started_at": job.started_at})
            threading.Thread(target=self._thread_main, args=(job,), name=f"salmon web {job.id}", daemon=True).start()

    def _call(self, callback: Callable[..., None], *args: Any) -> None:
        """Run `callback` on the server's loop, from any thread."""
        assert self._loop is not None
        # A closed loop: the server has stopped.
        with suppress(RuntimeError):
            self._loop.call_soon_threadsafe(callback, *args)

    def _thread_main(self, job: Job) -> None:
        """The job's thread: its own loop, until the job ends."""
        try:
            status, error, result = asyncio.run(self._run(job))
        except BaseException as err:
            traceback.print_exception(err, file=output.real_stderr())
            status, error, result = "failed", f"{type(err).__name__}: {err}", None
        self._call(self._finish, job, status, error, result)

    async def _run(self, job: Job) -> tuple[Status, str | None, Any]:
        with job._lock:
            if job._cancel_requested:
                return "cancelled", None, None
            job._loop = asyncio.get_running_loop()
            job._task = asyncio.current_task()
        lines = output.Lines(lambda line, err: self._call(self._log, job, line, err))
        asker = _JobAsker(self, job, lines)
        try:
            with (
                output.to(lines.write),
                interaction.using(WebInteraction(asker)),
                interaction.assuming_defaults(job.assume_defaults),
                dryrun.mode(job.dry_run),
            ):
                try:
                    result = await job.kind.run(job.params)
                finally:
                    lines.flush()
            return "done", None, msgspec.to_builtins(result)
        except BaseException as err:
            return self._outcome(job, err)

    def _outcome(self, job: Job, error: BaseException) -> tuple[Status, str | None, Any]:
        """A job's status and error once it raised `error`."""
        unknown = _unknown_outcome(error)
        if unknown is not None:
            return "unknown_outcome", f"{UNKNOWN_OUTCOME} ({unknown})", None
        with job._lock:
            cancelled = job._cancel_requested
        leaves = _leaves(error)
        if cancelled or all(isinstance(leaf, asyncio.CancelledError) for leaf in leaves):
            return "cancelled", None, None
        if any(isinstance(leaf, NoAnswerError) for leaf in leaves):
            return "failed", f"No answer for {QUESTION_TIMEOUT // 60} minutes: the job stopped.", None
        if any(isinstance(leaf, click.Abort) for leaf in leaves):
            return "failed", "Aborted.", None
        if len(leaves) == 1 and isinstance(leaves[0], click.exceptions.Exit) and leaves[0].exit_code == 0:
            return "done", None, None
        if len(leaves) == 1 and isinstance(leaves[0], click.ClickException):
            return "failed", leaves[0].format_message(), None
        # The server's own stderr, never the job's log: a chained error may repeat a request's URL.
        print(f"salmon web: {job.id} ({job.kind.name}) failed:", file=output.real_stderr())
        traceback.print_exception(error, file=output.real_stderr())
        return "failed", "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves), None

    # --- What a job's thread hands over -----------------------------------------------

    def _log(self, job: Job, line: str, err: bool) -> None:
        # Numbered, so a browser that reloads the log can tell the lines it has from those it lacks.
        job.lines += 1
        entry = {"n": job.lines, "text": self._redactor.text(line), "err": err}
        job.log.append(entry)
        self._publish({"event": "log", "job_id": job.id, "line": entry})

    def _open_question(self, job: Job, question: dict[str, Any], answer: concurrent.futures.Future[Any]) -> None:
        if job.status in FINISHED:
            answer.cancel()
            return
        job.question = self._redactor.value(question)
        job._question_id = question["id"]
        job._answer = answer
        job.status = "waiting"
        self._publish({"event": "question", "job_id": job.id, "question": job.question, "status": job.status})

    def _close_question(self, job: Job, question_id: str) -> None:
        if job._question_id != question_id:
            return
        job.question = None
        job._question_id = None
        job._answer = None
        if job.status == "waiting":
            job.status = "running"
        self._publish({"event": "answered", "job_id": job.id, "question_id": question_id, "status": job.status})

    def _show_spectrals(self, job: Job, folder: str, files: list[str]) -> None:
        job.spectrals = (folder, files)
        self._publish({"event": "spectrals", "job_id": job.id, "files": self._redactor.value(files)})

    def _finish(self, job: Job, status: Status, error: str | None, result: Any) -> None:
        job.status = status
        job.error = self._redactor.text(error) if error else None
        job.result = self._redactor.value(result)
        job.question = None
        job._question_id = None
        job._answer = None
        job.finished_at = _now()
        self._running.discard(job)
        if not self._running:
            self._idle.set()
        self._publish({"event": "finished", "job": job.summary()})
        if not self._stopping:
            self._schedule()
        self._prune()

    def _prune(self) -> None:
        finished = [job for job in self.jobs.values() if job.status in FINISHED]
        for job in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
            del self.jobs[job.id]
