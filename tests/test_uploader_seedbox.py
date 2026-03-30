import subprocess

import anyio

from salmon.config.validations import Seedbox
from salmon.uploader import seedbox


def test_rclone_upload_folder_streams_progress_output(monkeypatch) -> None:
    run_process_calls: list[tuple[list[str], dict[str, object]]] = []
    messages: list[str] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        run_process_calls.append((commands, kwargs))
        return subprocess.CompletedProcess(commands, 0)

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url="seedbox", extra_args=["--checksum", "-P"]),
        "/music",
        "/tmp/Artist - Album",
    )

    assert run_process_calls == [
        (
            ["rclone", "copy", "/tmp/Artist - Album", "seedbox:/music/Artist - Album", "--checksum", "-P"],
            {"stdout": None, "stderr": None, "check": False},
        )
    ]
    assert any("Rclone upload successful" in message for message in messages)


def test_rclone_upload_folder_reports_nonzero_exit_code(monkeypatch) -> None:
    messages: list[str] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 7)

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url="seedbox", extra_args=["-P"]),
        "/music",
        "/tmp/Artist - Album",
    )

    assert "Rclone upload failed with exit code 7" in messages
