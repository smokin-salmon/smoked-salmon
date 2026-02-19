import asyncio
import contextlib
import platform
import sys
from typing import Any

import aiohttp
import asyncclick as click

from salmon.common.aliases import AliasedCommands
from salmon.common.constants import RE_FEAT
from salmon.common.figles import (
    compress,
    create_relative_path,
    get_audio_files,
)
from salmon.common.regexes import (
    parse_copyright,
    re_split,
    re_strip,
)
from salmon.common.strings import (
    fetch_genre,
    less_uppers,
    make_searchstrs,
    normalize_accents,
    strip_template_keys,
    truncate,
)
from salmon.errors import ScrapeError

__all__ = [
    "AliasedCommands",
    "RE_FEAT",
    "compress",
    "create_relative_path",
    "get_audio_files",
    "parse_copyright",
    "re_split",
    "re_strip",
    "fetch_genre",
    "less_uppers",
    "make_searchstrs",
    "normalize_accents",
    "strip_template_keys",
    "truncate",
    "ScrapeError",
    "commandgroup",
    "prompt_async",
    "flush_stdin",
    "str_to_int_if_int",
    "handle_scrape_errors",
    "Prompt",
]


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, cls=AliasedCommands)
async def commandgroup() -> None:
    """Main command group for salmon CLI."""
    pass


class Prompt:
    """Async prompt handler for reading stdin without blocking the event loop."""

    def __init__(self) -> None:
        """Initialize the prompt handler."""
        self.q: asyncio.Queue[str] = asyncio.Queue()
        self.reader_added = False
        self.is_windows = platform.system() == "Windows"
        self.reader_task: asyncio.Task[None] | None = None

    def got_input(self) -> None:
        """Callback when input is received on stdin."""
        asyncio.create_task(self.q.put(sys.stdin.readline()))

    async def __call__(self, msg: str, end: str = "\n", flush: bool = False) -> str:
        """Display a message and wait for user input.

        Args:
            msg: The message to display.
            end: String to append after the message.
            flush: Whether to flush stdout.

        Returns:
            The user input string.
        """
        if not self.reader_added:
            if not self.is_windows:
                loop = asyncio.get_running_loop()
                loop.add_reader(sys.stdin, self.got_input)
            else:
                self.reader_task = asyncio.create_task(self._windows_input_reader())
            self.reader_added = True
        print(msg, end=end, flush=flush)
        try:
            result = (await self.q.get()).rstrip("\n")
        finally:
            await self._cleanup()
        return result

    async def _windows_input_reader(self) -> None:
        """Read stdin in a thread for Windows compatibility."""
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                await self.q.put(line)
        except asyncio.CancelledError:
            pass

    async def _cleanup(self) -> None:
        """Clean up resources after input is received."""
        if self.is_windows and self.reader_task:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
            self.reader_task = None
        elif not self.is_windows:
            try:
                loop = asyncio.get_running_loop()
                loop.remove_reader(sys.stdin)
            except (RuntimeError, ValueError):
                pass
        self.reader_added = False


prompt_async = Prompt()


def flush_stdin():
    try:
        from termios import TCIOFLUSH, tcflush

        tcflush(sys.stdin, TCIOFLUSH)
    except Exception:
        try:
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getch()
        except Exception:
            pass


def str_to_int_if_int(string: str, zpad: bool = False) -> str | int:
    """Convert string to int if it's a digit string.

    Args:
        string: The string to convert.
        zpad: Whether to zero-pad the result to 2 digits.

    Returns:
        The integer or zero-padded string if digit, original string otherwise.
    """
    if string.isdigit():
        if zpad:
            return f"{int(string):02d}"
        return int(string)
    return string


async def handle_scrape_errors(task: Any, mute: bool = False) -> Any | None:
    """Handle errors during scraping tasks.

    Args:
        task: The async task to run.
        mute: If True, suppress error messages.

    Returns:
        The task result or None on error.
    """
    try:
        return await task
    except (ScrapeError, aiohttp.ClientError, TimeoutError, KeyError) as e:
        if not mute:
            click.secho(f"Error message: {e}", fg="red", bold=True)
    except Exception as e:
        # Catch any unexpected errors too
        if not mute:
            click.secho(f"Unexpected scrape error: {e}", fg="red", bold=True)
    return None
