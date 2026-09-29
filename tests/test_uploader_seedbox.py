import os
import sys
import time

import anyio
import msgspec
import pytest

from salmon.config.validations import Seedbox
from salmon.uploader import seedbox
from salmon.uploader.torrent_client import DelugeClient, QBittorrentClient, TransmissionClient


def _fake_rclone(
    monkeypatch, tmp_path, *, stdout: str = "", stderr: str = "", exit_code: int = 0, code: str = ""
) -> None:
    """Put an `rclone` first on PATH that runs `code`, prints the given text and exits with exit_code."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "rclone"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"{code}\n"
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


@needs_posix
def test_rclone_writes_its_progress_straight_to_salmons_stdout(monkeypatch, tmp_path, capfd) -> None:
    # -P draws live progress on rclone's stdout, which only renders if it is salmon's own (the
    # terminal), not a pipe salmon reads.
    _fake_rclone(monkeypatch, tmp_path, code="print(sys.argv[1:], os.fstat(1).st_dev, os.fstat(1).st_ino)")
    salmons_stdout = f"{os.fstat(1).st_dev} {os.fstat(1).st_ino}"

    assert _upload_with_fake_rclone(Seedbox(url="seedbox", extra_args=["--checksum", "-P"])) is True

    out = capfd.readouterr().out
    assert f"{['copy', '/tmp/Artist - Album', 'seedbox:/music/Artist - Album', '--checksum', '-P']} " in out
    assert salmons_stdout in out
    assert "Rclone upload successful" in out


@needs_posix
def test_rclone_upload_folder_reports_nonzero_exit_code(monkeypatch, tmp_path, capfd) -> None:
    _fake_rclone(monkeypatch, tmp_path, exit_code=7)

    assert _upload_with_fake_rclone(Seedbox(url="seedbox", extra_args=["-P"])) is False

    assert "Rclone upload failed with exit code 7" in capfd.readouterr().out


@needs_posix
@pytest.mark.parametrize(
    ("url", "extra_args", "echo"),
    [
        (
            ":sftp,host=box,user=dean,pass=UNIQUESECRET",
            [],
            'CRITICAL: Failed to create file system for ":sftp,host=box,user=dean,pass=UNIQUESECRET:/music": '
            "couldn't connect SSH",
        ),
        (
            ":webdav,url='https://dean:UNIQUESECRET@dav.example/'",
            [],
            "CRITICAL: Failed to create file system for \":webdav,url='https://dean:UNIQUESECRET@dav.example/':\": 401",
        ),
        (
            "sbox",
            ["-vv", "--sftp-pass", "UNIQUESECRET"],
            'DEBUG : rclone: Version "v1.75.1" starting with parameters ["rclone" "copy" "-vv" "--sftp-pass" '
            '"UNIQUESECRET"]',
        ),
        ("sbox", ["--sftp-pass", "UNIQUESECRET"], "ERROR : authentication failed for UNIQUESECRET"),
        (
            "web",
            ["--http-headers", "Authorization,Bearer UNIQUESECRET"],
            "ERROR : webdav answered 401 for Bearer UNIQUESECRET",
        ),
        # rclone --dump auth, with a token from rclone's own config that salmon never sees.
        ("web", ["--dump", "auth"], "DEBUG : HTTP REQUEST\nAuthorization: Bearer UNIQUESECRET\nUser-Agent: rclone"),
        ("sbox", ["--sftp-pass", "UNIQUESECRET"], "ERROR : no newline after UNIQUESECRET"),
    ],
    ids=["remote", "url in remote", "-vv command line", "flag value", "comma list", "dumped header", "last line"],
)
def test_what_rclone_echoes_on_stderr_is_masked(
    monkeypatch, tmp_path, capfd, url: str, extra_args: list[str], echo: str
) -> None:
    _fake_rclone(monkeypatch, tmp_path, stderr=echo if "no newline" in echo else echo + "\n", exit_code=1)

    assert _upload_with_fake_rclone(Seedbox(url=url, extra_args=extra_args)) is False

    out, err = capfd.readouterr()
    # rclone's own line is still shown, only its secret is masked.
    assert echo[:20] in err
    assert "[REDACTED]" in err
    assert "UNIQUESECRET" not in out + err


@needs_posix
def test_rclone_errors_are_printed_as_they_come(monkeypatch, tmp_path) -> None:
    printed: list[tuple[float, str]] = []
    monkeypatch.setattr(seedbox.click, "echo", lambda message, **kwargs: printed.append((time.monotonic(), message)))
    _fake_rclone(
        monkeypatch, tmp_path, code="sys.stderr.write('early\\n'); sys.stderr.flush(); time.sleep(1)", stderr="late\n"
    )

    _upload_with_fake_rclone(Seedbox(url="sbox"))

    assert [message for _, message in printed] == ["early", "late"]
    # Not held back until rclone exits: an error or a prompt during a long copy shows at once.
    assert printed[1][0] - printed[0][0] > 0.5


class _RecordingClient:
    def __init__(self) -> None:
        self.save_paths: list[str] = []
        self.torrents: list[bytes] = []

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
        self.save_paths.append(remote_folder)
        self.torrents.append(torrent)
        return True


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

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        # commands[3] is "<url>:<remote_path>"; recover the seedbox url to look up its exit code.
        url = commands[3].split(":", 1)[0]
        return rclone_exit_codes.get(url, 0)

    monkeypatch.setattr(seedbox.cfg, "seedbox", seedboxes)
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    monkeypatch.setattr(seedbox, "_run_rclone", fake_run_rclone)
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


@needs_posix
def test_a_failed_copy_and_its_skipped_seed_are_reported_not_all_processed(monkeypatch, tmp_path, capfd) -> None:
    # A failed rclone copy used to still end with the green "All upload tasks processed" line,
    # because only add_to_downloader failures were counted towards the summary.
    _fake_rclone(monkeypatch, tmp_path, exit_code=1)
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RecordingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capfd.readouterr().out
    assert "All upload tasks processed" not in out
    assert "1 copy and 1 seed task failed" in out


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

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        local_path = commands[2]
        returncode = 1 if "Album2" in local_path else 0
        return returncode

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
    monkeypatch.setattr(seedbox, "_run_rclone", fake_run_rclone)
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

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        return 1

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
    monkeypatch.setattr(seedbox, "_run_rclone", fake_run_rclone)
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

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
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
    monkeypatch.setattr(seedbox, "_run_rclone", fake_run_rclone)
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


def _queue_one_release(tmp_path) -> "seedbox.UploadManager":
    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent = tmp_path / "Album.torrent"
    torrent.write_bytes(b"d4:infod4:name5:Albumee")
    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent), task_type="seed", is_flac=True, folder=str(release))
    return manager


@needs_posix
def test_the_upload_run_never_prints_a_remotes_password(monkeypatch, tmp_path, capfd) -> None:
    # The remote is named when the uploader is configured and when a failed copy skips the seed.
    _fake_rclone(monkeypatch, tmp_path, exit_code=1)
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="rclone", url=":sftp,host=box,pass=UNIQUESECRET", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RecordingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out, err = capfd.readouterr()
    assert "Configured rclone uploader to :sftp,host=box,pass=[REDACTED]" in out
    assert "Skipping seed on :sftp,host=box,pass=[REDACTED]" in out
    assert "UNIQUESECRET" not in out + err


def test_a_torrent_client_that_fails_to_configure_never_prints_its_password(monkeypatch, capsys) -> None:
    def refuse(url):
        raise ValueError(f"cannot parse {url}, password UNIQUESECRET")

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="local", torrent_client="qbittorrent+http://dean:UNIQUESECRET@box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(refuse))

    seedbox.UploadManager()

    out = capsys.readouterr().out
    assert "Failed to configure local uploader" in out
    assert "UNIQUESECRET" not in out


def test_a_failed_task_never_prints_the_seedbox_secrets(monkeypatch, tmp_path, capsys) -> None:
    async def refuse(commands: list[str], secrets: list[str]) -> int:
        raise OSError(f"could not start {' '.join(commands)}")

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="rclone", url="box", extra_args=["--sftp-pass", "UNIQUESECRET"], torrent_client="x")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RecordingClient()))
    monkeypatch.setattr(seedbox, "_run_rclone", refuse)

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Critical error during task: could not start rclone copy" in out
    assert "UNIQUESECRET" not in out


def test_a_torrent_the_client_refuses_never_prints_its_password(monkeypatch, tmp_path, capsys) -> None:
    class Refusing:
        def torrents_add(self, **kwargs):
            raise RuntimeError("POST http://dean:UNIQUESECRET@box:8080/api/v2/torrents/add failed for UNIQUESECRET")

    monkeypatch.setattr(seedbox.cfg, "seedbox", [Seedbox(type="local", torrent_client="unused")])
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: client))
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: Refusing())
    client = QBittorrentClient(username="dean", password="UNIQUESECRET", url="http://box:8080")

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Failed to add torrent" in out
    assert "UNIQUESECRET" not in out


class _RaisingClient:
    """A torrent client whose own add_to_downloader raises instead of catching its error."""

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
        raise RuntimeError("connection reset for UNIQUESECRET")


def test_seedbox_names_itself_when_the_client_raises_unexpectedly(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="local", name="My Box", torrent_client="qbittorrent+http://dean:UNIQUESECRET@box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RaisingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Failed to add torrent to client on My Box" in out
    assert "UNIQUESECRET" not in out


def _connected_client(monkeypatch, cls, fake_client):
    """Build a torrent client whose login() returns fake_client without touching a network."""
    monkeypatch.setattr(cls, "login", lambda self: fake_client)
    return cls(username="dean", password="UNIQUESECRET", url="http://box:8080", host="box", port=1)


def test_qbittorrent_fails_response_is_reported_as_not_added(monkeypatch, capsys) -> None:
    # qbittorrent-api's torrents_add returns "Fails." rather than raising, most commonly for a
    # duplicate torrent already in the client.
    class FakeApi:
        def torrents_add(self, **kwargs):
            return "Fails."

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    assert "successfully" not in capsys.readouterr().out.lower()


def test_qbittorrent_ok_response_is_reported_as_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def torrents_add(self, **kwargs):
            return "Ok."

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    assert "Torrent added successfully" in capsys.readouterr().out


def test_qbittorrent_5_1_metadata_success_is_reported_as_added(monkeypatch, capsys) -> None:
    # Web API v2.14.0+ (qBittorrent 5.1+) answers with a JSON object instead of "Ok."/"Fails.".
    class FakeApi:
        def torrents_add(self, **kwargs):
            return {"success_count": 1, "failure_count": 0, "pending_count": 0, "added_torrent_ids": ["abc"]}

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    assert "Torrent added successfully" in capsys.readouterr().out


def test_qbittorrent_5_1_metadata_failure_is_reported_as_not_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def torrents_add(self, **kwargs):
            return {"success_count": 0, "failure_count": 1, "pending_count": 0, "added_torrent_ids": []}

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    assert "successfully" not in capsys.readouterr().out.lower()


def test_deluge_none_result_is_reported_as_not_added(monkeypatch, capsys) -> None:
    # core.add_torrent_file returns None when the torrent is refused (already present).
    class FakeApi:
        def call(self, *args, **kwargs):
            return None

    client = _connected_client(monkeypatch, DelugeClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    assert "successfully" not in capsys.readouterr().out.lower()


def test_deluge_torrent_id_is_reported_as_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def call(self, *args, **kwargs):
            return "abc123"

    client = _connected_client(monkeypatch, DelugeClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    assert "Torrent added successfully" in capsys.readouterr().out


def test_transmission_torrent_result_is_reported_as_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def add_torrent(self, **kwargs):
            return object()

    client = _connected_client(monkeypatch, TransmissionClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    assert "Torrent added successfully" in capsys.readouterr().out


def test_a_client_call_that_raises_is_reported_as_not_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def torrents_add(self, **kwargs):
            raise RuntimeError("connection reset")

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    assert "successfully" not in capsys.readouterr().out.lower()


def test_a_client_that_never_connected_is_reported_as_not_added(monkeypatch, capsys) -> None:
    client = _connected_client(monkeypatch, QBittorrentClient, None)

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    assert "successfully" not in capsys.readouterr().out.lower()


class _RefusingClient:
    """A torrent client the seedbox layer sees as connected, but that never adds the torrent."""

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
        return False


def test_seedbox_reports_a_refused_torrent_plainly_by_name(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="local", name="My Box", torrent_client="unused")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RefusingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Torrent added to client successfully" not in out
    assert "Torrent was not added to the client on My Box" in out
    assert "seed task" in out.lower()
    assert "failed" in out.lower()


def test_seedbox_reports_a_refused_torrent_by_masked_url_without_a_name(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [Seedbox(type="rclone", url=":sftp,host=box,pass=UNIQUESECRET", torrent_client="unused", directory="/x")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RefusingClient()))
    monkeypatch.setattr(seedbox, "_rclone_upload_folder", lambda seedbox, remote, path: _async_true())

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Torrent added to client successfully" not in out
    assert "Torrent was not added to the client on :sftp,host=box,pass=[REDACTED]" in out
    assert "UNIQUESECRET" not in out


async def _async_true() -> bool:
    return True


def _manager_with_boxes(monkeypatch, seedboxes: list[Seedbox]) -> "seedbox.UploadManager":
    """UploadManager with the given seedboxes and a stubbed torrent client (no network)."""
    monkeypatch.setattr(seedbox.cfg, "seedbox", seedboxes)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RecordingClient()))
    return seedbox.UploadManager()


def test_add_upload_task_skips_a_box_pinned_to_a_different_tracker(monkeypatch) -> None:
    manager = _manager_with_boxes(
        monkeypatch, [Seedbox(name="red-box", trackers=["RED"], torrent_client="qbittorrent+http://box:8080")]
    )

    manager.add_upload_task("/tmp/Artist - Album", task_type="folder", is_flac=True, site_code="OPS")

    assert list(manager.tasks) == []


def test_add_upload_task_reaches_an_unpinned_box_for_any_tracker(monkeypatch) -> None:
    manager = _manager_with_boxes(monkeypatch, [Seedbox(name="any-box", torrent_client="qbittorrent+http://box:8080")])

    manager.add_upload_task("/tmp/Artist - Album", task_type="folder", is_flac=True, site_code="RED")
    manager.add_upload_task("/tmp/Artist - Album2", task_type="folder", is_flac=True, site_code="OPS")

    assert len(manager.tasks) == 2


def test_add_upload_task_reaches_a_box_pinned_to_both_trackers(monkeypatch) -> None:
    manager = _manager_with_boxes(
        monkeypatch,
        [Seedbox(name="both-box", trackers=["RED", "OPS"], torrent_client="qbittorrent+http://box:8080")],
    )

    manager.add_upload_task("/tmp/Artist - Album", task_type="folder", is_flac=True, site_code="RED")
    manager.add_upload_task("/tmp/Artist - Album2", task_type="folder", is_flac=True, site_code="OPS")

    assert len(manager.tasks) == 2


def test_red_and_ops_upload_seeds_only_the_pinned_box_for_each_and_copies_the_folder_once(
    monkeypatch, tmp_path
) -> None:
    # One release uploaded to both RED and OPS, with a seedbox pinned to each: the folder must be
    # copied once per box that serves either tracker, and each box's seed must be its own torrent.
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        return 0

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [
            Seedbox(
                name="red-box",
                trackers=["RED"],
                type="rclone",
                url="red",
                directory="/files/red",
                torrent_client="qbittorrent+http://red:8080",
            ),
            Seedbox(
                name="ops-box",
                trackers=["OPS"],
                type="rclone",
                url="ops",
                directory="/files/ops",
                torrent_client="qbittorrent+http://ops:8080",
            ),
        ],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    monkeypatch.setattr(seedbox, "_run_rclone", fake_run_rclone)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent_red = tmp_path / "Album [RED].torrent"
    torrent_red.write_bytes(b"d4:infod4:name5:Redeee")
    torrent_ops = tmp_path / "Album [OPS].torrent"
    torrent_ops.write_bytes(b"d4:infod4:name5:Opsxee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True, site_code="RED")
    manager.add_upload_task(str(torrent_red), task_type="seed", is_flac=True, folder=str(release), site_code="RED")
    manager.add_upload_task(str(release), task_type="folder", is_flac=True, site_code="OPS")
    manager.add_upload_task(str(torrent_ops), task_type="seed", is_flac=True, folder=str(release), site_code="OPS")
    anyio.run(manager.execute_upload)

    # One rclone copy per box, not one per (box, tracker) pair.
    assert len(rclone_calls) == 2
    assert clients["qbittorrent+http://red:8080"].torrents == [torrent_red.read_bytes()]
    assert clients["qbittorrent+http://ops:8080"].torrents == [torrent_ops.read_bytes()]


def test_seedbox_trackers_are_uppercased() -> None:
    assert Seedbox(trackers=["red", "ops"]).trackers == ["RED", "OPS"]


def test_seedbox_rejects_unknown_tracker() -> None:
    with pytest.raises(ValueError, match="Unknown tracker"):
        Seedbox(name="typo-box", trackers=["REDD"])
