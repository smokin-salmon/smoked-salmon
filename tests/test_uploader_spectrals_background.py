"""salmon up makes the spectrals in the background while the MQA, upconvert and log checks and the group question run.

upload() runs as in test_uploader_group_search (a local fake tracker, the steps that do not concern the spectrals
stubbed), but with the real check_spectrals and spectral generation, and a fake `sox` on PATH: a real process, which
writes the images it is asked for and records itself in a folder.
"""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers.uploader_dry_run import _write_flac
from helpers.uploader_group_search import (
    FakeApi,
    FakeTracker,
    Flow,
)
from test_uploader_group_search import (
    _fast_limiter,  # noqa: F401
)

import salmon.uploader
from salmon import cfg
from salmon.uploader import spectrals

pytestmark = pytest.mark.usefixtures("_fast_limiter")

# Records its pid and arguments in $SOX_RUNS, waits while $SOX_HOLD exists, fails if $SOX_FAIL exists, then writes
# the images named after -o.
FAKE_SOX = """
import json, os, sys, time
runs = os.environ["SOX_RUNS"]
with open(os.path.join(runs, f"{os.getpid()}.json"), "w") as f:
    json.dump(sys.argv[1:], f)
while os.path.exists(os.environ["SOX_HOLD"]):
    time.sleep(0.02)
if os.path.exists(os.environ["SOX_FAIL"]):
    sys.exit("sox FAIL formats: can't open input file")
for i, arg in enumerate(sys.argv):
    if arg == "-o":
        with open(sys.argv[i + 1], "wb") as f:
            f.write(b"\\x89PNG spectral")
with open(os.path.join(runs, f"{os.getpid()}.done"), "w") as f:
    pass
"""

TRACKS = ["01 - one.flac", "02 - two.flac"]


@pytest.fixture
def sox(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> "Sox":
    found = Sox(tmp_path_factory.mktemp("sox"))
    script = found.bin / "sox"
    script.write_text(f"#!{sys.executable}\n{FAKE_SOX}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{found.bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SOX_RUNS", str(found.runs))
    monkeypatch.setenv("SOX_HOLD", str(found.hold))
    monkeypatch.setenv("SOX_FAIL", str(found.fail))
    return found


class Sox:
    def __init__(self, root: Path) -> None:
        self.bin = root / "bin"
        self.runs = root / "runs"
        self.hold = root / "hold"
        self.fail = root / "fail"
        self.bin.mkdir()
        self.runs.mkdir()

    def started(self) -> list[int]:
        return sorted(int(p.stem) for p in self.runs.glob("*.json"))

    def finished(self) -> list[int]:
        return sorted(int(p.stem) for p in self.runs.glob("*.done"))

    def args(self) -> list[list[str]]:
        return [json.loads(p.read_text()) for p in sorted(self.runs.glob("*.json"))]

    async def wait_until_started(self, count: int = 1) -> None:
        with anyio.fail_after(10):
            while len(self.started()) < count:
                await anyio.sleep(0.01)

    async def wait_until_finished(self, count: int) -> None:
        with anyio.fail_after(10):
            while len(self.finished()) < count:
                await anyio.sleep(0.01)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead: it only waits for its parent to read its status. ps works off Linux too, unlike /proc.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _album(tmp_path: Path) -> Path:
    album = tmp_path / "release"
    album.mkdir()
    for number, name in enumerate(TRACKS, 1):
        _write_flac(album / name, title=name, tracknumber=str(number))
    return album


AUDIO_INFO = {name: {"duration": 60, "sample rate": 44100, "precision": 16} for name in TRACKS}


class SpectralsFlow(Flow):
    """Flow with the real check_spectrals; the group question, the frequency analysis and the prompts recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, sox: Sox, rls_format: str = "FLAC") -> None:
        super().__init__(monkeypatch, FakeTracker())
        self.sox = sox
        self.rls_format = rls_format
        # What the run did, in order, without printing it (printing during the checks is itself tested).
        self.steps: list[str] = []
        self.offered: dict[int, str] | None = None
        self.edited_with: Any = "not reached"
        # Run when the group question is asked, before it is answered.
        self.at_the_group_question = self.nothing
        self.printed_during_the_checks: tuple[str, str] | None = None
        # The spectrals folder's images when they were uploaded: the upload deletes them.
        self.uploaded_images: list[str] | None = None

    async def nothing(self) -> None:
        pass

    def install(self, path: str) -> None:
        super().install(path)
        flow = self
        monkeypatch = self.monkeypatch

        async def resolve_existing_group(*_args: Any, **_kwargs: Any) -> None:
            flow.steps.append("group question")
            await flow.at_the_group_question()
            flow.steps.append(f"group answered, sox started {len(flow.sox.started())}")

        async def prompt_spectrals(spectral_ids: dict[int, str], *_args: Any, **_kwargs: Any) -> dict[int, str]:
            flow.offered = dict(spectral_ids)
            return spectral_ids

        async def edit_metadata(path: str, tags: Any, metadata: Any, *args: Any, **_kwargs: Any) -> Any:
            flow.edited_with = args[5]  # spectral_ids
            return path, metadata, {}, {}

        async def upload_spectrals(spectrals_path: str, *_args: Any, **_kwargs: Any) -> None:
            flow.uploaded_images = sorted(os.listdir(spectrals_path)) if os.path.isdir(spectrals_path) else []

        rls_data = {"format": self.rls_format, "encoding": "Lossless", "artists": [("Artist", "main")]}
        rls_data |= {"title": "Hits Vol. 2", "catno": None}

        async def construct_rls_data(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return rls_data

        for name, fake in {
            "check_spectrals": spectrals.check_spectrals,
            "gather_audio_info": lambda *_a, **_k: AUDIO_INFO,
            "construct_rls_data": construct_rls_data,
            "resolve_existing_group": resolve_existing_group,
            "edit_metadata": edit_metadata,
            "post_upload_spectral_check": self.returning_async((False, None, None, None)),
        }.items():
            monkeypatch.setattr(salmon.uploader, name, fake)
        for name, fake in {
            "print_frequency_analysis": self.returning_async(False),
            "prompt_lossy_master": self.returning_async(False),
            "prompt_spectrals": prompt_spectrals,
            "upload_spectrals": upload_spectrals,
        }.items():
            monkeypatch.setattr(spectrals, name, fake)
        monkeypatch.setattr(spectrals.interaction, "show_spectrals", self.returning_async(None))
        monkeypatch.setattr(salmon.uploader, "prompt_lossy_master", self.returning_async(False))
        monkeypatch.setattr(cfg.upload.compression, "compress_spectrals", False)
        monkeypatch.setattr(cfg.directory, "tmp_dir", None)

    @staticmethod
    def returning_async(result: Any):
        async def fake(*_args: Any, **_kwargs: Any) -> Any:
            return result

        return fake

    async def run_upload(self, path: Path, **kwargs: Any) -> None:
        self.install(str(path))
        self.api = FakeApi(await self.tracker.start(), "good", "")
        try:
            spectral_ids = kwargs.pop("spectrals", ())
            await salmon.uploader.upload(self.api, str(path), None, "WEB", None, spectral_ids, None, **kwargs)
        finally:
            self.returned_at = time.monotonic()
            await self.api.close()
            await self.tracker.runner.cleanup()


def test_the_spectrals_are_generated_while_the_group_question_waits(monkeypatch, tmp_path, sox) -> None:
    album = _album(tmp_path)
    flow = SpectralsFlow(monkeypatch, sox)
    # The question is only answered once every spectral is made: only possible if they are made meanwhile.
    flow.at_the_group_question = lambda: sox.wait_until_finished(len(TRACKS))

    anyio.run(flow.run_upload, album)

    assert flow.steps == ["group question", "group answered, sox started 2"]
    assert flow.offered == {1: TRACKS[0], 2: TRACKS[1]}
    assert flow.edited_with == {1: TRACKS[0], 2: TRACKS[1]}
    assert flow.uploaded_images == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]
    assert len(sox.started()) == len(TRACKS)


def test_the_images_are_made_as_they_are_in_the_foreground(monkeypatch, tmp_path, sox) -> None:
    album = _album(tmp_path)
    flow = SpectralsFlow(monkeypatch, sox)
    anyio.run(flow.run_upload, album)
    background = sorted(sox.args())

    for run in sox.runs.iterdir():
        run.unlink()
    foreground = tmp_path / "foreground"
    foreground.mkdir()
    ids = anyio.run(spectrals.generate_spectrals_all, str(album), str(foreground), AUDIO_INFO)

    assert ids == flow.offered
    expected = sorted(sox.args())
    assert [[a.replace(str(album / "Spectrals"), "<out>") for a in run] for run in background] == [
        [a.replace(str(foreground), "<out>") for a in run] for run in expected
    ]


def test_nothing_is_printed_while_the_checks_and_the_question_run(monkeypatch, tmp_path, sox, capsys) -> None:
    album = _album(tmp_path)
    flow = SpectralsFlow(monkeypatch, sox)

    async def checks() -> None:
        await sox.wait_until_finished(len(TRACKS))
        # Long enough for the background work to print, had it anything to print.
        await anyio.sleep(0.2)
        flow.printed_during_the_checks = capsys.readouterr()

    flow.checks = checks
    anyio.run(flow.run_upload, album)

    assert flow.printed_during_the_checks is not None
    out, err = flow.printed_during_the_checks
    assert "spectral" not in out.lower() and "spectral" not in err.lower()
    out, err = capsys.readouterr()
    # Printed once the spectral step takes over, as in the foreground.
    assert out.index("Checking lossy master / spectrals...") < out.index("Finished generating spectrals.")
    assert "Generating Spectrals" in err


def test_aborting_during_the_checks_stops_the_generation(monkeypatch, tmp_path, sox, capsys) -> None:
    album = _album(tmp_path)
    flow = SpectralsFlow(monkeypatch, sox)
    sox.hold.touch()

    async def abort() -> None:
        await sox.wait_until_started(len(TRACKS))
        raise salmon.uploader.click.Abort

    flow.checks = abort
    started = time.monotonic()
    anyio.run(flow.run_upload, album)

    assert "Aborting upload..." in capsys.readouterr().out
    assert time.monotonic() - started < 5
    assert flow.steps == []
    assert [pid for pid in sox.started() if _alive(pid)] == []
    assert sox.finished() == []
    # The run never reached the spectral step: it leaves no spectrals folder, as before.
    assert not (album / "Spectrals").exists()


def test_an_error_in_the_generation_is_raised_at_the_spectral_step(monkeypatch, tmp_path, sox) -> None:
    album = _album(tmp_path)
    sox.fail.touch()
    flow = SpectralsFlow(monkeypatch, sox)
    # The question waits for the failure, so the failure comes before the step that reports it.
    flow.at_the_group_question = lambda: sox.wait_until_started(len(TRACKS))

    # As in the foreground: the failed sox runs, grouped by the task group that ran them.
    with pytest.raises(ExceptionGroup) as raised:
        anyio.run(flow.run_upload, album)

    assert raised.group_contains(subprocess.CalledProcessError, depth=1)
    assert flow.events == ["checks start", "checks end"]
    assert flow.steps == ["group question", "group answered, sox started 2"]
    assert flow.offered is None


def test_given_spectral_ids_are_generated_in_the_background_too(monkeypatch, tmp_path, sox) -> None:
    album = _album(tmp_path)
    flow = SpectralsFlow(monkeypatch, sox)
    flow.at_the_group_question = lambda: sox.wait_until_finished(1)

    anyio.run(lambda: flow.run_upload(album, spectrals=(2,)))

    assert flow.steps == ["group question", "group answered, sox started 1"]
    # As in the foreground, the images are numbered over the picked tracks.
    assert flow.edited_with == {1: TRACKS[1]}
    assert flow.uploaded_images == ["01 Full.png", "01 Zoom.png"]
    assert TRACKS[1] in sox.args()[0][1]


@pytest.mark.parametrize(
    ("rls_format", "kwargs"),
    [
        ("FLAC", {"spectrals_after": True}),
        ("MP3", {}),
        ("FLAC", {"spectrals": (0,)}),
        ("FLAC", {"spectrals": (1,), "given_spectral_urls": {1: ["https://images.example/01.png"]}}),
    ],
    ids=["spectrals after the upload", "not FLAC", "no spectrals", "spectral URLs given"],
)
def test_nothing_is_generated_when_the_run_makes_no_spectrals_then(
    monkeypatch, tmp_path, sox, rls_format: str, kwargs: dict[str, Any]
) -> None:
    album = _album(tmp_path)
    flow = SpectralsFlow(monkeypatch, sox, rls_format=rls_format)
    # Long enough for a sox started in the background to have started.
    flow.at_the_group_question = lambda: anyio.sleep(0.5)

    anyio.run(lambda: flow.run_upload(album, **kwargs))

    assert flow.steps == ["group question", "group answered, sox started 0"]
    assert sox.started() == []
