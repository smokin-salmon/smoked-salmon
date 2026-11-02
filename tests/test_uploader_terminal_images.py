"""The native spectrals viewer draws in a terminal that speaks the Kitty graphics protocol, and falls back elsewhere."""

import asyncio
import base64
import contextlib
import io
import os
import re
import threading
from functools import partial
from pathlib import Path

import anyio
import pytest
from PIL import Image

from salmon import cfg
from salmon.uploader import spectrals, terminal_images
from salmon.uploader.terminal_images import _kitty_placement, _layout, _parse_reply

OK = b"\x1b_Gi=31;OK\x1b\\"
DA1 = b"\x1b[?62;22c"


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        (OK + DA1, True),
        (DA1, False),
        (b"\x1b_Gi=31;ENOTSUPPORTED:no\x1b\\" + DA1, False),
        (DA1 + OK, False),
        (OK, None),
        (b"\x1b_Gi=31;O", None),
        (b"", None),
    ],
)
def test_the_reply_is_read_once_the_device_attributes_are_in(reply: bytes, answer: bool | None) -> None:
    assert _parse_reply(reply) is answer


def test_there_is_no_query_without_a_terminal(monkeypatch, capsys) -> None:
    monkeypatch.setattr(terminal_images, "_supported", None)
    monkeypatch.setattr(terminal_images.sys, "stdin", io.StringIO())

    def no_query(*_args) -> bool:
        raise AssertionError("queried a terminal that is not there")

    monkeypatch.setattr(terminal_images, "_query_terminal", no_query)

    assert anyio.run(terminal_images.supports_kitty_graphics) is False
    assert capsys.readouterr().out == ""


def test_the_terminal_is_asked_once_per_run(monkeypatch) -> None:
    monkeypatch.setattr(terminal_images, "_supported", True)
    monkeypatch.setattr(terminal_images, "_query_terminal", lambda *_args: pytest.fail("asked again"))

    assert anyio.run(terminal_images.supports_kitty_graphics) is True


@pytest.mark.skipif(os.name != "posix", reason="a pty and termios are POSIX only")
@pytest.mark.parametrize(("reply", "answer"), [(OK + DA1, True), (DA1, False), (None, False)])
def test_the_terminal_is_asked_over_a_pty_and_its_settings_put_back(reply: bytes | None, answer: bool) -> None:
    import pty
    import termios

    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        received = bytearray()

        def terminal() -> None:
            while b"\x1b[c" not in received:
                received.extend(os.read(master, 1024))
            if reply is not None:
                os.write(master, reply)

        thread = threading.Thread(target=terminal, daemon=True)
        thread.start()

        assert terminal_images._query_terminal(slave, slave, timeout=0.5) is answer

        thread.join(timeout=1)
        assert bytes(received).startswith(b"\x1b_Gi=31,")
        after = termios.tcgetattr(slave)
        # macOS sets PENDIN, a flag of its own that the next read clears, when line editing is turned back on without
        # flushing input, as _query_terminal does (TCSANOW). _keypresses restores with TCSAFLUSH, which does not set it.
        after[3] &= ~getattr(termios, "PENDIN", 0)
        assert after == before
    finally:
        os.close(master)
        os.close(slave)


def test_a_png_is_sent_in_chunks_that_give_it_back() -> None:
    png = os.urandom(10_000)

    sequences = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", _kitty_placement(png, 40, 10, 2))

    assert len(sequences) > 1
    keys = [dict(pair.split("=") for pair in keys.split(",")) for keys, _ in sequences]
    assert keys[0] | {"m": "1"} == {
        "a": "T", "f": "100", "t": "d", "q": "2", "C": "1", "i": "2", "c": "40", "r": "10", "m": "1"
    }  # fmt: skip
    assert [k["m"] for k in keys] == ["1"] * (len(keys) - 1) + ["0"]
    assert all(len(chunk) <= 4096 for _, chunk in sequences)
    assert base64.standard_b64decode("".join(chunk for _, chunk in sequences)) == png


# What sox makes: a full spectral, wide, and a zoomed one, tall; and the averaged spectrum.
FULL, ZOOM, SPECTRUM = (2144, 613), (644, 1125), (1200, 500)


def test_the_layout_fills_the_width_and_stays_on_the_screen() -> None:
    cell = (10, 20)

    (full, zoom), (spectrum,) = _layout(200, 60, cell, [[FULL, ZOOM], [SPECTRUM]])

    assert full.row == zoom.row == 2
    assert full.rows == zoom.rows
    assert full.col == 1 and zoom.col == full.cols + 2
    assert spectrum.row == full.row + full.rows + 1
    for box in (full, zoom, spectrum):
        assert box.col + box.cols - 1 <= 200
        assert box.row + box.rows - 1 <= 60 - 2
    # The full spectral keeps its shape, within a cell.
    assert abs(full.cols * cell[0] / (full.rows * cell[1]) - FULL[0] / FULL[1]) < FULL[0] / FULL[1] * 0.1


def test_a_wide_terminal_is_limited_by_its_height() -> None:
    (full, zoom), (spectrum,) = _layout(400, 30, (10, 20), [[FULL, ZOOM], [SPECTRUM]])

    assert spectrum.row + spectrum.rows - 1 <= 30 - 2
    assert zoom.col + zoom.cols - 1 < 400


def test_a_track_without_its_spectrum_has_one_row() -> None:
    assert _layout(200, 60, (10, 20), [[FULL, ZOOM], []]) == _layout(200, 60, (10, 20), [[FULL, ZOOM]])
    assert len(_layout(200, 60, (10, 20), [[FULL, ZOOM], []])) == 1


def test_a_tiny_terminal_gets_no_empty_box() -> None:
    boxes = [box for row in _layout(5, 3, (10, 20), [[FULL, ZOOM], [SPECTRUM]]) for box in row]

    assert len(boxes) == 3
    assert all(box.cols >= 1 and box.rows >= 1 for box in boxes)


def _png(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size).save(path)


def _specs(tmp_path: Path) -> Path:
    path = tmp_path / "Spectrals"
    path.mkdir()
    for sid in (1, 2):
        _png(path / f"{sid:02d} Full.png", (200, 60))
        _png(path / f"{sid:02d} Zoom.png", (60, 110))
    _png(path / spectrals.spectrum_plot_name(2), (120, 50))
    return path


RIGHT, LEFT, ENTER, ESC = b"\x1b[C", b"\x1b[D", b"\r", b"\x1b"


@pytest.mark.parametrize(
    ("keys", "actions"),
    [
        (RIGHT, ["right"]),
        (b"\x1bOC", ["right"]),
        (LEFT, ["left"]),
        (b"\x1bOD", ["left"]),
        (b"l", ["right"]),
        (b"h", ["left"]),
        (b"\x7f", ["left"]),
        (ENTER, ["enter"]),
        (b" ", ["enter"]),
        (b"q", ["quit"]),
        (ESC, ["quit"]),
        # A held arrow, read in one go: each press counts.
        (RIGHT * 3, ["right"] * 3),
        (RIGHT + b"q", ["right", "quit"]),
        # Up and down, and a letter of no use, do nothing.
        (b"\x1b[A\x1b[Bx", []),
    ],
)
def test_keys_are_read_as_the_viewer_uses_them(keys: bytes, actions: list[str]) -> None:
    assert terminal_images._actions(keys) == actions


@pytest.mark.parametrize(
    ("index", "actions", "to"),
    [
        (0, ["right"], 1),
        (1, ["left"], 0),
        (0, ["left"], 0),
        (2, ["right"], 2),
        (0, ["enter"], 1),
        (2, ["enter"], None),
        (0, ["quit"], None),
        (0, ["right", "right", "right", "left"], 1),
        (1, ["right", "enter"], None),
    ],
)
def test_the_arrows_stop_at_either_end_and_enter_ends_on_the_last_page(
    index: int, actions: list[str], to: int | None
) -> None:
    assert terminal_images._step(index, 3, actions) == to


@pytest.mark.skipif(os.name != "posix", reason="add_reader on a pipe is POSIX only")
def test_an_escape_sequence_is_read_whole_and_a_lone_esc_alone() -> None:
    read_fd, write_fd = os.pipe()
    try:

        async def read(*writes: bytes) -> bytes:
            for data in writes:
                os.write(write_fd, data)
            return await terminal_images._read_keys(read_fd)

        assert anyio.run(read, LEFT) == LEFT
        assert anyio.run(read, ESC) == ESC

        async def esc_then_the_rest_soon_after() -> bytes:
            os.write(write_fd, ESC)
            # As an arrow split in two over a slow link: the rest comes well inside the wait.
            asyncio.get_running_loop().call_later(0.005, os.write, write_fd, b"[D")
            return await terminal_images._read_keys(read_fd)

        assert anyio.run(esc_then_the_rest_soon_after) == LEFT
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="a pty and termios are POSIX only")
def test_reading_keys_puts_the_terminal_back() -> None:
    import pty
    import termios

    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        with pytest.raises(KeyboardInterrupt), terminal_images._keypresses(slave):
            assert not termios.tcgetattr(slave)[3] & (termios.ECHO | termios.ICANON)
            raise KeyboardInterrupt
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(os.name != "posix", reason="a pty and termios are POSIX only")
def test_keys_left_unread_do_not_answer_the_next_question() -> None:
    import pty
    import select

    master, slave = pty.openpty()
    try:
        with terminal_images._keypresses(slave):
            # A second enter, pressed as the viewing ends: it would take the lossy-master question's default.
            os.write(master, ENTER)
            assert select.select([slave], [], [], 1)[0]
        assert not select.select([slave], [], [], 0.1)[0]
    finally:
        os.close(master)
        os.close(slave)


@pytest.fixture
def pressing(monkeypatch):
    """Feed the viewer the keys given, a read each, and record the heading of every page it draws."""
    monkeypatch.setattr(terminal_images, "_terminal_size", lambda: (100, 40, (10, 20)))
    monkeypatch.setattr(terminal_images, "_keypresses", lambda _fd: contextlib.nullcontext())
    monkeypatch.setattr(terminal_images.sys, "stdin", io.StringIO())
    monkeypatch.setattr(terminal_images.sys.stdin, "fileno", lambda: 0, raising=False)
    drawn: list[str] = []
    real_draw = terminal_images._draw_page

    def draw(heading: str, footer: str, rows_of_paths: list[list[str]]) -> None:
        drawn.append(heading)
        real_draw(heading, footer, rows_of_paths)

    monkeypatch.setattr(terminal_images, "_draw_page", draw)

    def press(*reads: bytes) -> None:
        keys = iter(reads)

        async def read_keys(_fd: int) -> bytes:
            key = next(keys)
            if isinstance(key, BaseException):
                raise key
            return key

        monkeypatch.setattr(terminal_images, "_read_keys", read_keys)

    return press, drawn


def _view_in_terminal(path: Path, ids: dict[int, str]) -> None:
    anyio.run(terminal_images.show_spectrals_in_terminal, str(path), ids)


def test_the_viewer_goes_back_and_forth(pressing, tmp_path, capsys) -> None:
    press, drawn = pressing
    path = _specs(tmp_path)
    press(RIGHT, LEFT, LEFT, RIGHT, RIGHT, ENTER)

    _view_in_terminal(path, {2: "02 Two.flac", 1: "01 One.flac"})

    # ← on the first page and → on the last draw nothing again.
    assert [heading.split(":")[0] for heading in drawn] == [
        "Track 01 (1/2)",
        "Track 02 (2/2)",
        "Track 01 (1/2)",
        "Track 02 (2/2)",
    ]
    out = capsys.readouterr().out
    assert out.startswith(terminal_images._ALT_SCREEN_ON + terminal_images._HIDE_CURSOR)
    assert out.endswith(terminal_images._DELETE_ALL + terminal_images._SHOW_CURSOR + terminal_images._ALT_SCREEN_OFF)
    # Full and Zoom for the first track; Full, Zoom and the spectrum for the second.
    assert out.count("a=T,") == 2 + 3 + 2 + 3
    assert "01 One.flac" in out and "02 Two.flac" in out


def test_q_stops_the_viewing_at_once(pressing, tmp_path) -> None:
    press, drawn = pressing
    press(b"q")

    _view_in_terminal(_specs(tmp_path), {1: "01.flac", 2: "02.flac"})

    assert len(drawn) == 1


def test_the_viewer_ends_when_the_terminal_is_gone(pressing, tmp_path) -> None:
    press, drawn = pressing
    press(b"")

    _view_in_terminal(_specs(tmp_path), {1: "01.flac", 2: "02.flac"})

    assert len(drawn) == 1


def test_the_viewer_leaves_the_alternate_screen_when_stopped(pressing, tmp_path, capsys) -> None:
    press, _drawn = pressing
    press(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _view_in_terminal(_specs(tmp_path), {1: "01.flac"})

    assert capsys.readouterr().out.endswith(terminal_images._SHOW_CURSOR + terminal_images._ALT_SCREEN_OFF)


def test_the_viewer_skips_tracks_without_images(pressing, tmp_path) -> None:
    press, drawn = pressing
    press(ENTER)

    _view_in_terminal(_specs(tmp_path), {1: "01.flac", 7: "07.flac"})

    assert drawn == ["Track 01 (1/1): 01.flac"]


@pytest.mark.skipif(os.name != "posix", reason="a pty and termios are POSIX only")
def test_the_viewer_reads_keys_from_a_real_terminal(monkeypatch, tmp_path) -> None:
    import pty
    import termios

    monkeypatch.setattr(terminal_images, "_terminal_size", lambda: (100, 40, (10, 20)))
    master, slave = pty.openpty()
    drawn: list[str] = []
    try:
        before = termios.tcgetattr(slave)
        monkeypatch.setattr(terminal_images.sys, "stdin", open(slave, closefd=False))  # noqa: SIM115

        def draw(heading: str, _footer: str, _rows_of_paths: list[list[str]]) -> None:
            drawn.append(heading.split(":")[0])
            # The keys for this page, pressed once it is drawn.
            # → to the second, ← back, then keys that cancel out (no page is drawn for them) and q.
            os.write(master, {1: RIGHT, 2: LEFT, 3: RIGHT + LEFT + b"q"}[len(drawn)])

        monkeypatch.setattr(terminal_images, "_draw_page", draw)

        async def view() -> None:
            with anyio.fail_after(5):
                await terminal_images.show_spectrals_in_terminal(str(_specs(tmp_path)), {1: "01.flac", 2: "02.flac"})

        anyio.run(view)

        assert drawn == ["Track 01 (1/2)", "Track 02 (2/2)", "Track 01 (1/2)"]
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_the_viewer_with_no_image_draws_nothing(tmp_path, capsys) -> None:
    _view_in_terminal(tmp_path, {1: "01.flac"})

    out = capsys.readouterr().out
    assert terminal_images._ALT_SCREEN_ON not in out and "No PNG files" in out


@pytest.fixture
def viewers(monkeypatch) -> list[str]:
    """Which viewer view_spectrals opened, the terminal being one that draws Kitty graphics unless a test says not."""
    opened: list[str] = []

    async def record(name: str, *_args) -> None:
        opened.append(name)

    async def supported() -> bool:
        opened.append("query")
        return True

    monkeypatch.setattr(spectrals.platform, "system", lambda: "Linux")
    monkeypatch.setattr(terminal_images, "supports_kitty_graphics", supported)
    monkeypatch.setattr(terminal_images, "show_spectrals_in_terminal", partial(record, "terminal"))
    monkeypatch.setattr(spectrals, "_open_specs_on_linux", partial(record, "linux"))
    monkeypatch.setattr(spectrals, "_open_specs_in_web_server", partial(record, "web"))
    monkeypatch.setattr(cfg.upload, "native_spectrals_viewer", True)
    monkeypatch.setattr(cfg.upload, "terminal_spectrals_viewer", True)
    return opened


async def _no_enter(_text: str) -> None:
    pass


def _view() -> None:
    anyio.run(spectrals.view_spectrals, "/specs", {1: "01.flac"}, _no_enter)


def test_the_native_viewer_draws_in_a_terminal_that_can(viewers) -> None:
    _view()
    assert viewers == ["query", "terminal"]


def test_the_native_viewer_opens_the_linux_viewer_in_a_terminal_that_cannot(monkeypatch, viewers) -> None:
    async def unsupported() -> bool:
        return False

    monkeypatch.setattr(terminal_images, "supports_kitty_graphics", unsupported)
    _view()
    assert viewers == ["linux"]


def test_the_terminal_is_not_asked_with_terminal_spectrals_viewer_off(monkeypatch, viewers) -> None:
    monkeypatch.setattr(cfg.upload, "terminal_spectrals_viewer", False)
    _view()
    assert viewers == ["linux"]


def test_the_web_viewer_does_not_ask_the_terminal(monkeypatch, viewers) -> None:
    monkeypatch.setattr(cfg.upload, "native_spectrals_viewer", False)
    _view()
    assert viewers == ["web"]
