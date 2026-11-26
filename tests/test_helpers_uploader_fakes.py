"""tests/helpers/uploader_fakes.py: a run with check_spectrals stubbed makes no spectrals in the background (#1199).

The real check_spectrals waits for the background generation (spectrals_in_background) before it touches the
spectrals folder. A stand-in does not: with sox installed, the generation's sox went on writing into the folder the
stand-in was replacing, and the run failed now and then with "Could not remove Spectrals ... (Directory not empty)".
"""

from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers.uploader_dry_run import _album, _lossy_with_spectrals, _run_up
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see the usefixtures mark)
)

from salmon import cfg
from salmon.uploader import spectrals

pytestmark = pytest.mark.usefixtures("image_uploads")


def test_a_stand_in_check_spectrals_runs_no_spectral_generation_beside_it(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path / "downloads"))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    (tmp_path / "downloads").mkdir()
    (tmp_path / "torrents").mkdir()
    sox_runs: list[str] = []

    async def sox(_path: str, filename: str, spectrals_path: str, _info: Any, idx: int) -> tuple[int, str]:
        """Stands in for a sox run: writes its images into the folder, and is still running when cancelled."""
        sox_runs.append(filename)
        for kind in ("Full", "Zoom"):
            Path(spectrals_path, f"{idx + 1:02d} {kind}.png").write_bytes(b"png")
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def check_spectrals(path: str, *args: Any, **kwargs: Any) -> tuple[bool, dict[int, str]]:
        # Every other task blocked first: a background generation has then reached its sox runs.
        await anyio.wait_all_tasks_blocked()
        return await _lossy_with_spectrals(path, *args, **kwargs)

    monkeypatch.setattr(spectrals, "_generate_spectral_for_file", sox)

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        tmp_path / "torrents",
        trackers=("RED",),
        multi_tracker_upload=False,
        check_spectrals=check_spectrals,
    )

    assert run.result.exit_code == 0, run.result.output
    assert sox_runs == [], f"sox ran into the folder check_spectrals replaces: {sox_runs}"
