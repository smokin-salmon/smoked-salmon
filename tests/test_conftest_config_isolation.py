"""The test run must not pick up a developer's real config, even via SALMON_CONFIG_DIR (bug in
docs/plans/conftest-real-config.md).

conftest.py sets SALMON_CONFIG_DIR itself, but only proves anything if it overrides a value the
shell already set: this drives a real `pytest` subprocess with SALMON_CONFIG_DIR pointing at a
sentinel config first, and checks the sentinel never won.
"""

import os
import subprocess
import sys
from pathlib import Path

_SENTINEL = "definitely-not-a-real-developer-directory"


def test_probe_config_is_not_the_sentinel() -> None:
    # Collected by the subprocess below (as this file's own conftest.py applies). Also passes
    # when collected normally, since salmon.cfg never points at the sentinel then either.
    import salmon

    assert _SENTINEL not in str(salmon.cfg.directory.download_directory)


def test_test_run_ignores_a_preset_salmon_config_dir_sentinel(tmp_path: Path) -> None:
    # A real (but sentinel-named) directory, so a bypass loads a config that validates fine,
    # rather than merely crashing on a bad download_directory.
    sentinel_dir = tmp_path / "developer-config"
    sentinel_download_dir = tmp_path / _SENTINEL
    sentinel_torrents_dir = tmp_path / f"{_SENTINEL}-torrents"
    sentinel_dir.mkdir()
    sentinel_download_dir.mkdir()
    sentinel_torrents_dir.mkdir()
    (sentinel_dir / "config.toml").write_text(
        "[directory]\n"
        f"download_directory = '{sentinel_download_dir.as_posix()}'\n"
        f"dottorrents_dir = '{sentinel_torrents_dir.as_posix()}'\n"
        "\n"
        "[tracker.red]\n"
        "session = 'sentinel-session-cookie'\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["SALMON_CONFIG_DIR"] = str(sentinel_dir)

    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{Path(__file__).name}::test_probe_config_is_not_the_sentinel",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root / "tests",
    )

    assert result.returncode == 0, result.stdout + result.stderr
