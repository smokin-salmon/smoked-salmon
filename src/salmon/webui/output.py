"""What a salmon web job prints goes to its log (ADR 0004, section 2).

While salmon web runs, ``sys.stdout`` and ``sys.stderr`` are streams that send what is written to the log of the job
whose context writes it (``click.echo``, ``click.secho``, ``err=True``, ``print``), found through a context variable,
and anything else to the real streams. Nothing in asyncclick is patched: click looks up ``sys.stdout`` on each write.
A tracker request a job hands over to salmon web's request loop carries the job's context, so what it prints goes to
the job's log too.
"""

import io
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TextIO, cast

import asyncclick as click

# Where the running job's output goes: (text, whether it was written to stderr).
Sink = Callable[[str, bool], None]

_sink: ContextVar[Sink | None] = ContextVar("job_output", default=None)
# A line longer than this, still without its end, is sent as it is (a progress bar that never ends its line).
MAX_PARTIAL_LINE = 10_000


class _JobStream(io.TextIOBase):
    """``sys.stdout`` or ``sys.stderr`` while salmon web runs."""

    def __init__(self, real: TextIO, err: bool) -> None:
        super().__init__()
        self.real = real
        self._err = err

    @property
    def encoding(self) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        # click writes to a stream with an encoding as it is, and wraps one without.
        return "utf-8"

    @property
    def errors(self) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        return "strict"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(cast("object", text), str):
            # As a text stream does: click tries bytes to tell binary streams apart.
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        sink = _sink.get()
        if sink is None:
            return self.real.write(text)
        sink(text, self._err)
        return len(text)

    def flush(self) -> None:
        if _sink.get() is None:
            self.real.flush()

    def isatty(self) -> bool:
        # A job's log is no terminal: click leaves out the colour codes.
        return _sink.get() is None and self.real.isatty()

    def fileno(self) -> int:
        return self.real.fileno()


@contextmanager
def capturing() -> Iterator[None]:
    """Send what each job writes to ``sys.stdout`` and ``sys.stderr`` to its sink, for the block."""
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _JobStream(real_out, err=False), _JobStream(real_err, err=True)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def in_job() -> bool:
    """Whether what this context prints goes to a salmon web job's log, not to a terminal."""
    return _sink.get() is not None


def real_stderr() -> TextIO:
    """The server's own stderr, never a job's log: where tracebacks go."""
    stream = sys.stderr
    return stream.real if isinstance(stream, _JobStream) else stream


@contextmanager
def to(sink: Sink) -> Iterator[None]:
    """Send what this context writes, and every task it starts, to `sink`."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


class Lines:
    """Turns what a job writes, from any thread, into whole lines without colour codes.

    A carriage return starts the line again, as on a terminal: a progress bar ends as its last state.
    """

    def __init__(self, line: Callable[[str, bool], None]) -> None:
        self._line = line
        self._lock = threading.Lock()
        self._partial = {False: "", True: ""}

    def write(self, text: str, err: bool) -> None:
        with self._lock:
            *lines, partial = (self._partial[err] + text).split("\n")
            if "\r" in partial:
                partial = _shown(partial) + ("\r" if partial.endswith("\r") else "")
            if len(partial) > MAX_PARTIAL_LINE:
                lines.append(partial)
                partial = ""
            self._partial[err] = partial
            # Under the lock, so lines written by two threads are sent in the order they were written.
            for line in lines:
                self._line(click.unstyle(_shown(line)), err)

    def flush(self) -> None:
        """Send what is left of an unfinished line, as before a question or at the end of the job."""
        with self._lock:
            for err, partial in self._partial.items():
                if partial:
                    self._line(click.unstyle(_shown(partial)), err)
            self._partial = {False: "", True: ""}


def _shown(line: str) -> str:
    """What a terminal shows of a line written with carriage returns: the text after the last one."""
    line = line.rstrip("\r")
    return line[line.rfind("\r") + 1 :]
