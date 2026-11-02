"""On Linux the native spectrals viewer opens feh, or without it the desktop's default image viewer, or says why not."""

import subprocess
from pathlib import Path

import anyio
import pytest

from salmon import cfg
from salmon.uploader import spectrals

LOUPE = "org.gnome.Loupe.desktop"


class FakeSystem:
    """The tools installed, what xdg-mime answers and how xdg-open exits; records every command run."""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.installed: set[str] = {"feh", "xdg-mime", "xdg-open"}
        self.default_viewer = LOUPE
        self.xdg_open_exit = 0
        self.ran: list[list[str]] = []
        self.folder = tmp_path
        for name in ("02.png", "01.png", ".01.png.tmp"):
            (tmp_path / name).write_bytes(b"png")
        monkeypatch.setattr(
            spectrals.shutil, "which", lambda name: f"/usr/bin/{name}" if name in self.installed else None
        )
        monkeypatch.setattr(spectrals.anyio, "run_process", self._run_process)
        monkeypatch.setattr(cfg.upload, "feh_fullscreen", False)
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    async def _run_process(self, args: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        if args[0] not in self.installed:
            raise FileNotFoundError(args[0])
        self.ran.append(args)
        if args[0] == "xdg-mime":
            return subprocess.CompletedProcess(args, 0, f"{self.default_viewer}\n".encode(), b"")
        if args[0] == "xdg-open":
            return subprocess.CompletedProcess(args, self.xdg_open_exit, b"", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def view(self) -> None:
        anyio.run(spectrals._open_specs_on_linux, str(self.folder))

    @property
    def opened(self) -> list[str]:
        """The programs that showed the spectrals: xdg-mime only asks."""
        return [args[0] for args in self.ran if args[0] != "xdg-mime"]


@pytest.fixture
def system(monkeypatch, tmp_path) -> FakeSystem:
    return FakeSystem(monkeypatch, tmp_path)


def test_spectral_review_without_feh_does_not_crash(monkeypatch, system) -> None:
    async def unsupported() -> bool:
        return False

    async def no_enter(_text: str) -> None:
        pass

    monkeypatch.setattr(spectrals.platform, "system", lambda: "Linux")
    monkeypatch.setattr(spectrals.terminal_images, "supports_kitty_graphics", unsupported)
    monkeypatch.setattr(cfg.upload, "native_spectrals_viewer", True)
    system.installed.discard("feh")
    anyio.run(spectrals.view_spectrals, str(system.folder), {1: "01.flac", 2: "02.flac"}, no_enter)
    assert system.opened == ["xdg-open"]


def test_feh_opens_the_folder_as_before(system) -> None:
    system.view()
    assert system.ran == [
        ["feh", "--cycle-once", "--sort", "filename", "-d", "--auto-zoom", "-geometry", "-.", str(system.folder)]
    ]


def test_feh_goes_fullscreen_when_set(monkeypatch, system) -> None:
    monkeypatch.setattr(cfg.upload, "feh_fullscreen", True)
    system.view()
    assert system.ran[0][4] == "--fullscreen"


def test_the_default_viewer_is_told_where_the_other_spectrals_are(system, capsys) -> None:
    # Not every PNG handler pages through the folder (GIMP, a browser).
    system.installed.discard("feh")
    system.view()
    assert f"Opening 01.png with {LOUPE}. All the spectrals are in {system.folder}." in capsys.readouterr().out


def test_without_feh_the_default_viewer_opens_the_first_spectral(system) -> None:
    system.installed.discard("feh")
    system.view()
    assert system.ran == [
        ["xdg-mime", "query", "default", "image/png"],
        ["xdg-open", str(system.folder / "01.png")],
    ]


def test_wayland_without_xwayland_skips_feh(monkeypatch, system) -> None:
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    system.view()
    assert system.opened == ["xdg-open"]


@pytest.mark.parametrize("feh", [True, False])
def test_without_a_display_nothing_runs_and_the_folder_is_named(monkeypatch, system, capsys, feh) -> None:
    if not feh:
        system.installed.discard("feh")
    monkeypatch.delenv("DISPLAY")
    system.view()
    assert system.ran == []
    out = capsys.readouterr().out
    assert "no display" in out
    assert str(system.folder) in out


def test_without_feh_or_xdg_utils_it_says_so(system, capsys) -> None:
    system.installed = set()
    system.view()
    assert system.ran == []
    out = capsys.readouterr().out
    assert "feh is not installed, and there is no default image viewer." in out
    assert str(system.folder) in out


def test_without_a_default_viewer_xdg_open_is_not_run(system, capsys) -> None:
    system.installed.discard("feh")
    system.default_viewer = ""
    system.view()
    assert system.opened == []
    assert "feh is not installed, and there is no default image viewer." in capsys.readouterr().out


def test_feh_on_wayland_alone_is_not_called_missing(monkeypatch, system, capsys) -> None:
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    system.default_viewer = ""
    system.view()
    assert system.opened == []
    assert "feh needs an X display, and there is no default image viewer." in capsys.readouterr().out


def test_a_failed_xdg_open_names_the_viewer(system, capsys) -> None:
    system.installed.discard("feh")
    system.xdg_open_exit = 4
    system.view()
    assert system.opened == ["xdg-open"]
    assert f"feh is not installed, and the default image viewer ({LOUPE}) failed to open." in capsys.readouterr().out


def test_a_folder_without_spectrals_opens_nothing(system, capsys) -> None:
    for png in system.folder.glob("*.png"):
        png.unlink()
    system.view()
    assert system.ran == []
    assert capsys.readouterr().out == ""
