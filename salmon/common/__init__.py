import asyncio
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
    format_size,
    less_uppers,
    make_searchstrs,
    normalize_accents,
    strip_template_keys,
    truncate,
)
from salmon.errors import ScrapeError

loop = asyncio.get_event_loop()


@click.group(context_settings=dict(help_option_names=["-h", "--help"]), cls=AliasedCommands)
def commandgroup():
    pass


class Prompt:
    # https://stackoverflow.com/a/35514777

    def __init__(self):
        self.q = asyncio.Queue()
        self.reader_added = False
        self.is_windows = platform.system() == "Windows"

    def got_input(self):
        asyncio.create_task(self.q.put(sys.stdin.readline()))

    async def __call__(self, msg, end="\n", flush=False):
        if not self.reader_added:
            if not self.is_windows:
                loop.add_reader(sys.stdin, self.got_input)
            else:
                asyncio.create_task(self._windows_input_reader())
            self.reader_added = True
        print(msg, end=end, flush=flush)
        return (await self.q.get()).rstrip("\n")

    async def _windows_input_reader(self):
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            await self.q.put(line)


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
