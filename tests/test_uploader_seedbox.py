import os
import subprocess
import sys

import anyio
import msgspec
import pytest

from salmon.config.validations import Seedbox
from salmon.uploader import seedbox


def _fake_rclone(monkeypatch, tmp_path, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
    """Put an `rclone` first on PATH that prints the given text and exits with exit_code."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "rclone"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _upload_with_fake_rclone(seedbox_config: Seedbox) -> bool:
    return anyio.run(seedbox._rclone_upload_folder, seedbox_config, "/music", "/tmp/Artist - Album")


needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="the fake rclone is a script with a shebang")


@needs_posix
@pytest.mark.parametrize(
    ("url", "extra_args"),
    [
        ("sbox", ["--sftp-pass", "UNIQUESECRET"]),
        ("sbox", ["--sftp-pass=UNIQUESECRET"]),
        ("sbox", ["--sftp-key-pem", "-----BEGIN KEY----- UNIQUESECRET -----END KEY-----"]),
        ("sbox", ["--sftp-key-pem=BEGIN KEY UNIQUESECRET DATA"]),
        ("web", ["--http-headers", "Authorization,Bearer UNIQUESECRET"]),
        ("web", ["--webdav-url", "https://dean:UNIQUESECRET@dav.example/remote.php"]),
        (":sftp,host=box,user=dean,pass=UNIQUESECRET", []),
    ],
    ids=["flag value", "equals form", "quoted with spaces", "equals form with spaces", "comma list", "url", "remote"],
)
def test_the_rclone_command_salmon_prints_hides_the_seedbox_secrets(
    monkeypatch, tmp_path, capfd, url: str, extra_args: list[str]
) -> None:
    _fake_rclone(monkeypatch, tmp_path)

    assert _upload_with_fake_rclone(Seedbox(url=url, extra_args=extra_args)) is True

    out, err = capfd.readouterr()
    assert "Executing: rclone copy" in out
    assert "Rclone upload successful" in out
    assert "UNIQUESECRET" not in out + err


@needs_posix
def test_the_rclone_command_salmon_prints_keeps_harmless_values(monkeypatch, tmp_path, capfd) -> None:
    _fake_rclone(monkeypatch, tmp_path)
    extra_args = ["--checksum", "-P", "--sftp-path-override", "@/volume3", "--transfers", "4", "--bwlimit=8M"]

    _upload_with_fake_rclone(Seedbox(url="sbox", extra_args=extra_args))

    assert (
        "Executing: rclone copy '/tmp/Artist - Album' 'sbox:/music/Artist - Album' "
        "--checksum -P --sftp-path-override @/volume3 --transfers 4 --bwlimit=8M"
    ) in capfd.readouterr().out


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


class _RecordingClient:
    def __init__(self) -> None:
        self.save_paths: list[str] = []
        self.torrents: list[bytes] = []

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> None:
        self.save_paths.append(remote_folder)
        self.torrents.append(torrent)


def _run_upload(
    monkeypatch,
    tmp_path,
    seedboxes: list[Seedbox],
    rclone_exit_codes: dict[str, int] | None = None,
) -> tuple[dict[str, _RecordingClient], list[list[str]]]:
    """Queue one release on the given seedboxes and run the upload with a fake client and rclone.

    Args:
        rclone_exit_codes: Maps a seedbox URL to the exit code its rclone call should return.
            Seedboxes not listed succeed (exit code 0).

    Returns:
        The torrent clients salmon logged in to, by URL, and the rclone commands it ran.
    """
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []
    rclone_exit_codes = rclone_exit_codes or {}

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        rclone_calls.append(commands)
        # commands[3] is "<url>:<remote_path>"; recover the seedbox url to look up its exit code.
        url = commands[3].split(":", 1)[0]
        return subprocess.CompletedProcess(commands, rclone_exit_codes.get(url, 0))

    monkeypatch.setattr(seedbox.cfg, "seedbox", seedboxes)
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent = tmp_path / "Artist - Album.torrent"
    torrent.write_bytes(b"d4:infod4:name5:Albumee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent), task_type="seed", is_flac=True, folder=str(release))
    anyio.run(manager.execute_upload)
    return clients, rclone_calls


def test_disabled_seedbox_is_skipped(monkeypatch, tmp_path) -> None:
    # A disabled local entry listed before the rclone one used to add the torrent first, with the
    # local download_directory as its save path (#478).
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            Seedbox(type="local", enabled=False, torrent_client="qbittorrent+http://local:8080"),
            Seedbox(type="rclone", enabled=False, url="old", torrent_client="qbittorrent+http://old:8080"),
            Seedbox(
                type="rclone",
                enabled=True,
                url="box",
                directory="/home/user/files",
                torrent_client="qbittorrent+http://box:8080",
            ),
        ],
    )

    assert list(clients) == ["qbittorrent+http://box:8080"]
    assert clients["qbittorrent+http://box:8080"].save_paths == ["/home/user/files"]
    assert [call[3] for call in rclone_calls] == ["box:/home/user/files/Artist - Album (2020) [WEB FLAC]"]


def test_seedbox_without_enabled_key_is_used(monkeypatch, tmp_path) -> None:
    # Configs written before enabled was honoured often leave it out; they must keep uploading.
    entry = msgspec.toml.decode(
        b'type = "rclone"\nurl = "box"\ndirectory = "/files"\ntorrent_client = "qbittorrent+http://box:8080"\n',
        type=Seedbox,
    )

    clients, rclone_calls = _run_upload(monkeypatch, tmp_path, [entry])

    assert clients["qbittorrent+http://box:8080"].save_paths == ["/files"]
    assert len(rclone_calls) == 1


def test_failed_rclone_copy_skips_seeding_on_that_seedbox(monkeypatch, tmp_path) -> None:
    # A failed rclone copy used to be reported but not acted on: the seed task still added
    # the torrent, so the client checked or downloaded a folder that was never uploaded (#503).
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            Seedbox(
                type="rclone",
                url="box",
                directory="/files",
                torrent_client="qbittorrent+http://box:8080",
            ),
        ],
        rclone_exit_codes={"box": 1},
    )

    assert len(rclone_calls) == 1
    assert clients["qbittorrent+http://box:8080"].save_paths == []


def test_failed_rclone_copy_does_not_affect_other_seedboxes(monkeypatch, tmp_path) -> None:
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            Seedbox(
                type="rclone",
                url="broken",
                directory="/files",
                torrent_client="qbittorrent+http://broken:8080",
            ),
            Seedbox(
                type="rclone",
                url="good",
                directory="/files",
                torrent_client="qbittorrent+http://good:8080",
            ),
        ],
        rclone_exit_codes={"broken": 1},
    )

    assert len(rclone_calls) == 2
    assert clients["qbittorrent+http://broken:8080"].save_paths == []
    assert clients["qbittorrent+http://good:8080"].save_paths == ["/files"]


def test_successful_rclone_copy_still_seeds(monkeypatch, tmp_path) -> None:
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            Seedbox(
                type="rclone",
                url="box",
                directory="/files",
                torrent_client="qbittorrent+http://box:8080",
            ),
        ],
    )

    assert len(rclone_calls) == 1
    assert clients["qbittorrent+http://box:8080"].save_paths == ["/files"]


def test_failed_copy_only_skips_seeding_its_own_folder(monkeypatch, tmp_path) -> None:
    # An upload run queues several releases (a FLAC plus each transcode). A failed copy of one
    # folder used to be keyed by seedbox alone, so it skipped the seed of every other folder
    # queued for that seedbox too, including ones whose files did arrive.
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        rclone_calls.append(commands)
        local_path = commands[2]
        returncode = 1 if "Album2" in local_path else 0
        return subprocess.CompletedProcess(commands, returncode)

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release1 = tmp_path / "Artist - Album1 (2020) [WEB FLAC]"
    release1.mkdir()
    release2 = tmp_path / "Artist - Album2 (2020) [WEB FLAC]"
    release2.mkdir()
    torrent1 = tmp_path / "Album1.torrent"
    torrent1.write_bytes(b"d4:infod4:name6:Album1ee")
    torrent2 = tmp_path / "Album2.torrent"
    torrent2.write_bytes(b"d4:infod4:name6:Album2ee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release1), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent1), task_type="seed", is_flac=True, folder=str(release1))
    manager.add_upload_task(str(release2), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent2), task_type="seed", is_flac=True, folder=str(release2))
    anyio.run(manager.execute_upload)

    assert len(rclone_calls) == 2
    client = clients["qbittorrent+http://box:8080"]
    assert client.torrents == [torrent1.read_bytes()]


def test_failed_copy_skips_both_torrents_of_multi_tracker_upload(monkeypatch, tmp_path) -> None:
    # multi_tracker_upload builds two torrents (RED and OPS) from the same release folder. A
    # failed copy of that folder must skip both seed tasks, not just the first one queued.
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        rclone_calls.append(commands)
        return subprocess.CompletedProcess(commands, 1)

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent_red = tmp_path / "Album [RED].torrent"
    torrent_red.write_bytes(b"d4:infod4:name5:Redeee")
    torrent_ops = tmp_path / "Album [OPS].torrent"
    torrent_ops.write_bytes(b"d4:infod4:name5:Opsxee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent_red), task_type="seed", is_flac=True, folder=str(release))
    manager.add_upload_task(str(torrent_ops), task_type="seed", is_flac=True, folder=str(release))
    anyio.run(manager.execute_upload)

    assert len(rclone_calls) == 1
    client = clients["qbittorrent+http://box:8080"]
    assert client.torrents == []


def test_rclone_not_installed_skips_seeding(monkeypatch, tmp_path) -> None:
    # A folder task that raises (rclone missing, unexpected I/O error, ...) must count as a
    # failed copy too, not just a logged and forgotten "critical error".
    clients: dict[str, _RecordingClient] = {}

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("rclone")

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent = tmp_path / "Album.torrent"
    torrent.write_bytes(b"d4:infod4:name5:Albumee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent), task_type="seed", is_flac=True, folder=str(release))
    anyio.run(manager.execute_upload)

    client = clients["qbittorrent+http://box:8080"]
    assert client.torrents == []
