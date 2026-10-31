"""Spectrals drawn in the terminal itself, in one that speaks the Kitty graphics protocol (kitty, Ghostty, WezTerm...).

Whether it does is asked of the terminal, not read from the environment, so the answer holds over SSH, where
TERM_PROGRAM and KITTY_WINDOW_ID do not follow. The images go to the terminal as data, never as a path, for the same
reason: a terminal on another machine cannot read salmon's files.

Nothing here reads the config: `view_spectrals` decides whether this viewer is used.
"""

import asyncio
import base64
import contextlib
import os
import re
import select
import shutil
import struct
import sys
import time
from collections.abc import Iterator
from typing import NamedTuple

import anyio
import anyio.to_thread
import asyncclick as click
from PIL import Image

# The query the protocol documents: a one-pixel image the terminal checks without keeping, then a Primary Device
# Attributes request. Every VT-style terminal answers the second, so a terminal that knows nothing of the first
# still answers, and the wait ends there.
_QUERY = b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c"
_GRAPHICS_REPLY = re.compile(rb"\x1b_Gi=31;([^\x1b]*)\x1b\\")
_DA1_REPLY = re.compile(rb"\x1b\[\?[\d;]*c")
# Long enough for an SSH round trip; only a terminal that answers neither query waits it out.
QUERY_TIMEOUT = 1.5

# The base64 a single escape sequence may carry, as the protocol sets it.
_CHUNK = 4096
# Deletes every image on the screen, and frees its data.
_DELETE_ALL = "\x1b_Ga=d,d=A,q=2\x1b\\"
_ALT_SCREEN_ON = "\x1b[?1049h"
_ALT_SCREEN_OFF = "\x1b[?1049l"
_CLEAR = "\x1b[2J\x1b[H"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"

# The heading takes the first row, the keys that work the last.
_ROWS_ABOVE = 1
_ROWS_BELOW = 1
# What a cell is taken to measure when the terminal does not say: only its aspect matters.
_DEFAULT_CELL = (8, 16)

_supported: bool | None = None


class Box(NamedTuple):
    """Where an image goes, in cells: its top-left cell (counted from 1, as the terminal does) and its size."""

    col: int
    row: int
    cols: int
    rows: int


def _parse_reply(data: bytes) -> bool | None:
    """What the terminal's reply to `_QUERY` says: True if it draws Kitty graphics, None while the reply is incomplete.

    The answer is known only once the device attributes are in: until then, a graphics reply may still be followed by
    them, and they must not be left for the next prompt to read.
    """
    attributes = _DA1_REPLY.search(data)
    if attributes is None:
        return None
    graphics = _GRAPHICS_REPLY.search(data, 0, attributes.start())
    return graphics is not None and graphics.group(1) == b"OK"


def _query_terminal(fd_in: int, fd_out: int, timeout: float = QUERY_TIMEOUT) -> bool:
    """Ask the terminal on `fd_in` and `fd_out` whether it draws Kitty graphics. Blocks for up to `timeout` seconds.

    The terminal's settings are put back however this ends. A reply that comes too late is thrown away, so that it does
    not end up in the next answer the user types.
    """
    import termios

    try:
        saved = termios.tcgetattr(fd_in)
    except termios.error:
        return False
    try:
        quiet = termios.tcgetattr(fd_in)
        # No echo, so the reply is not printed, and no line editing, so it is read before a newline that never comes.
        quiet[3] &= ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(fd_in, termios.TCSANOW, quiet)
        os.write(fd_out, _QUERY)
        reply = b""
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([fd_in], [], [], left)
            if not readable:
                break
            chunk = os.read(fd_in, 1024)
            if not chunk:
                break
            reply += chunk
            answer = _parse_reply(reply)
            if answer is not None:
                return answer
        termios.tcflush(fd_in, termios.TCIFLUSH)
        return False
    except termios.error:
        return False
    finally:
        termios.tcsetattr(fd_in, termios.TCSANOW, saved)


async def supports_kitty_graphics() -> bool:
    """Whether the terminal salmon runs in draws Kitty graphics. Asked once per run; False when there is no terminal."""
    global _supported
    if _supported is None:
        _supported = False
        terminal = sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"
        if os.name == "posix" and terminal:
            sys.stdout.flush()
            with contextlib.suppress(OSError):
                _supported = await anyio.to_thread.run_sync(_query_terminal, sys.stdin.fileno(), sys.stdout.fileno())
    return _supported


def _kitty_placement(png: bytes, cols: int, rows: int, image_id: int) -> str:
    """The escape sequences that draw a PNG over `cols` by `rows` cells from the cursor, leaving the cursor there."""
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[i : i + _CHUNK] for i in range(0, len(data), _CHUNK)] or [""]
    # q=2: the terminal answers nothing, which would otherwise arrive as typed input.
    first = f"a=T,f=100,t=d,q=2,C=1,i={image_id},c={cols},r={rows},"
    out = []
    for index, chunk in enumerate(chunks):
        more = int(index < len(chunks) - 1)
        out.append(f"\x1b_G{first if index == 0 else ''}m={more};{chunk}\x1b\\")
    return "".join(out)


def _layout(
    term_cols: int, term_rows: int, cell: tuple[int, int], rows_of_sizes: list[list[tuple[int, int]]]
) -> list[list[Box]]:
    """Where each image goes: each row of images at one height across the width, every row shrunk alike to fit.

    Args:
        term_cols: The terminal's width in cells.
        term_rows: The terminal's height in cells.
        cell: A cell's width and height in pixels.
        rows_of_sizes: Each row's images, by their width and height in pixels.

    Returns:
        A box per image, in the shape of `rows_of_sizes`. No box is empty, even in a terminal too small for them.
    """
    cell_w, cell_h = cell
    rows_of_sizes = [sizes for sizes in rows_of_sizes if sizes]
    if not rows_of_sizes:
        return []
    width = term_cols * cell_w
    height = (term_rows - _ROWS_ABOVE - _ROWS_BELOW - (len(rows_of_sizes) - 1)) * cell_h
    # A row's height when it takes the whole width, a cell between images.
    full_heights = []
    for sizes in rows_of_sizes:
        aspect = sum(w / h for w, h in sizes)
        full_heights.append(max(width - (len(sizes) - 1) * cell_w, 0) / aspect)
    scale = min(1.0, max(height, 0) / sum(full_heights))

    boxes = []
    row = _ROWS_ABOVE + 1
    for sizes, full_height in zip(rows_of_sizes, full_heights, strict=True):
        row_height = full_height * scale
        rows = max(1, int(row_height // cell_h))
        boxes_in_row = []
        col = 1
        for w, h in sizes:
            cols = max(1, int(row_height * w / h // cell_w))
            boxes_in_row.append(Box(col, row, cols, rows))
            col += cols + 1
        boxes.append(boxes_in_row)
        row += rows + 1
    return boxes


def _terminal_size() -> tuple[int, int, tuple[int, int]]:
    """The terminal's width and height in cells, and a cell's size in pixels."""
    size = shutil.get_terminal_size()
    try:
        import fcntl
        import termios

        rows, cols, x_pixels, y_pixels = struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        )
    except (ImportError, OSError, ValueError):
        return size.columns, size.lines, _DEFAULT_CELL
    if not (rows and cols):
        return size.columns, size.lines, _DEFAULT_CELL
    if not (x_pixels and y_pixels):
        return cols, rows, _DEFAULT_CELL
    return cols, rows, (x_pixels // cols or 1, y_pixels // rows or 1)


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _page_images(spectrals_path: str, spectral_id: int) -> list[list[str]]:
    """A track's images that exist, by row: its full and zoomed spectrals, then its averaged spectrum."""
    # Imported here: the spectrals module imports this one.
    from salmon.uploader.spectrals import spectrum_plot_name

    rows = [[f"{spectral_id:02d} Full.png", f"{spectral_id:02d} Zoom.png"], [spectrum_plot_name(spectral_id)]]
    return [[path for name in names if os.path.isfile(path := os.path.join(spectrals_path, name))] for names in rows]


def _draw_page(heading: str, footer: str, rows_of_paths: list[list[str]]) -> None:
    """Clear the screen and draw a track's images in it, between a heading and a line of the keys that work."""
    cols, rows, cell = _terminal_size()
    sizes = []
    for paths in rows_of_paths:
        row_sizes = []
        for path in paths:
            with Image.open(path) as image:
                row_sizes.append(image.size)
        sizes.append(row_sizes)
    out = [_DELETE_ALL, _CLEAR, click.style(heading[:cols], bold=True)]
    image_id = 1
    for paths, boxes in zip((paths for paths in rows_of_paths if paths), _layout(cols, rows, cell, sizes), strict=True):
        for path, box in zip(paths, boxes, strict=True):
            with open(path, "rb") as f:
                png = f.read()
            out.append(f"\x1b[{box.row};{box.col}H")
            out.append(_kitty_placement(png, box.cols, box.rows, image_id))
            image_id += 1
    out.append(f"\x1b[{rows};1H")
    out.append(click.style(footer[:cols], fg="magenta", bold=True))
    _write("".join(out))


# One key each: an escape sequence (an arrow is CSI or SS3, as the terminal's cursor mode has it), a lone Esc, or a
# byte.
_KEY = re.compile(rb"\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z]|\x1b|.", re.DOTALL)
_KEY_ACTIONS = {
    b"\x1b[C": "right",
    b"\x1bOC": "right",
    b"l": "right",
    b"n": "right",
    b"\x1b[D": "left",
    b"\x1bOD": "left",
    b"h": "left",
    b"p": "left",
    b"\x7f": "left",
    b"\x08": "left",
    b"\r": "enter",
    b"\n": "enter",
    b" ": "enter",
    b"q": "quit",
    b"Q": "quit",
    b"\x1b": "quit",
}
# How long after an Esc the rest of an escape sequence may come: a lone Esc is a key of its own.
_ESC_WAIT = 0.05


def _actions(data: bytes) -> list[str]:
    """What the keys read mean to the viewer, in order, keys it has no use for left out."""
    return [action for key in _KEY.findall(data) if (action := _KEY_ACTIONS.get(key))]


def _step(index: int, count: int, actions: list[str]) -> int | None:
    """The page the keys lead to from `index`, of `count` pages, or None when they end the viewing.

    The arrows stop at either end. Enter is next too, and on the last page it ends, as q does anywhere.
    """
    for action in actions:
        if action == "quit" or (action == "enter" and index == count - 1):
            return None
        index = max(index - 1, 0) if action == "left" else min(index + 1, count - 1)
    return index


@contextlib.contextmanager
def _keypresses(fd: int) -> Iterator[None]:
    """For the block, let `fd` give each key as it is pressed, without echoing it, and throw away keys typed before.

    Ctrl-C still interrupts.
    """
    import termios

    saved = termios.tcgetattr(fd)
    keys = termios.tcgetattr(fd)
    keys[3] &= ~(termios.ECHO | termios.ICANON)
    keys[6][termios.VMIN] = 1
    keys[6][termios.VTIME] = 0
    # TCSAFLUSH: an enter typed ahead must not turn the first page before it is seen.
    termios.tcsetattr(fd, termios.TCSAFLUSH, keys)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)


async def _readable(fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    loop.add_reader(fd, lambda: ready.done() or ready.set_result(None))
    try:
        await ready
    finally:
        loop.remove_reader(fd)
    return os.read(fd, 64)


async def _read_keys(fd: int) -> bytes:
    """The keys pressed next on `fd`: at least one, and an escape sequence whole."""
    data = await _readable(fd)
    if data.endswith(b"\x1b"):
        with anyio.move_on_after(_ESC_WAIT):
            data += await _readable(fd)
    return data


def _footer(index: int, count: int) -> str:
    if index == count - 1:
        return "← previous   Enter / q: done"
    return "← → previous / next   Enter: next   q: done, skip the rest"


async def show_spectrals_in_terminal(spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
    """Draw the spectrals in the terminal, a track at a time, and return once the user is done with them.

    The arrow keys turn the pages, Enter goes on and ends on the last one, and q ends at once: a track that shows a
    lossy master is reason enough to stop looking. The pages are drawn on the terminal's alternate screen, which is
    left on return, so what salmon printed before them is on the screen again afterwards.

    This is the terminal's own viewer, which only TerminalInteraction opens (through `view_spectrals`), and only once
    the terminal has answered `supports_kitty_graphics`: it reads the keys from the terminal itself.

    Args:
        spectrals_path: Path to the spectrals folder.
        all_spectral_ids: The tracks' file names, by spectral ID.
    """
    pages = [(sid, rows) for sid in sorted(all_spectral_ids) if any(rows := _page_images(spectrals_path, sid))]
    if not pages:
        click.secho("No PNG files found to display.", fg="yellow")
        return
    fd = sys.stdin.fileno()
    _write(_ALT_SCREEN_ON + _HIDE_CURSOR)
    try:
        with _keypresses(fd):
            index: int | None = 0
            while index is not None:
                sid, rows_of_paths = pages[index]
                heading = f"Track {sid:02d} ({index + 1}/{len(pages)}): {all_spectral_ids[sid]}"
                await anyio.to_thread.run_sync(_draw_page, heading, _footer(index, len(pages)), rows_of_paths)
                shown = index
                # Keys that leave the page where it is (← on the first) draw nothing again.
                while index == shown:
                    keys = await _read_keys(fd)
                    # Nothing read: the terminal is gone.
                    index = _step(shown, len(pages), _actions(keys)) if keys else None
    finally:
        _write(_DELETE_ALL + _SHOW_CURSOR + _ALT_SCREEN_OFF)
