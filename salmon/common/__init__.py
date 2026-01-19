import asyncio
import contextlib
import platform
import sys

import click
import httpx
from requests import RequestException

from salmon.common.aliases import AliasedCommands  # noqa: F401
from salmon.common.constants import RE_FEAT  # noqa: F401
from salmon.common.figles import (  # noqa: F401
    compress,
    create_relative_path,
    get_audio_files,
)
from salmon.common.regexes import (  # noqa: F401
    parse_copyright,
    re_split,
    re_strip,
)
from salmon.common.strings import (  # noqa: F401
    fetch_genre,
    less_uppers,
    make_searchstrs,
    normalize_accents,
    strip_template_keys,
    truncate,
)
from salmon.errors import ScrapeError


@click.group(context_settings=dict(help_option_names=["-h", "--help"]), cls=AliasedCommands)
def commandgroup():
    pass


class Prompt:
    # https://stackoverflow.com/a/35514777

    def __init__(self):
        self.q = asyncio.Queue()
        self.reader_added = False
        self.is_windows = platform.system() == "Windows"
        self.reader_task = None

    def got_input(self):
        asyncio.create_task(self.q.put(sys.stdin.readline()))

    async def __call__(self, msg, end="\n", flush=False):
        if not self.reader_added:
            if not self.is_windows:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError as exc:
                    raise RuntimeError("Prompt must be called from a running event loop.") from exc
                loop.add_reader(sys.stdin, self.got_input)
            else:
                self.reader_task = asyncio.create_task(self._windows_input_reader())
            self.reader_added = True
        print(msg, end=end, flush=flush)
        result = (await self.q.get()).rstrip("\n")

        # Clean up after getting input
        await self._cleanup()
        return result

    async def _windows_input_reader(self):
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                await self.q.put(line)
        except asyncio.CancelledError:
            pass

    async def _cleanup(self):
        """Clean up resources after input is received"""
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


def str_to_int_if_int(string, zpad=False):
    if string.isdigit():
        if zpad:
            return f"{int(string):02d}"
        return int(string)
    return string


async def handle_scrape_errors(task, mute=False):
    try:
        return await task
    except (ScrapeError, httpx.RequestError, httpx.TimeoutException, KeyError, RequestException) as e:
        if not mute:
            click.secho(f"Error message: {e}", fg="red", bold=True)
    except Exception as e:
        # Catch any unexpected errors too
        if not mute:
            click.secho(f"Unexpected scrape error: {e}", fg="red", bold=True)
