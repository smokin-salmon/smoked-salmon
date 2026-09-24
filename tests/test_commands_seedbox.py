"""`salmon checkconf --seedbox` actually contacts the seedbox and rclone remote (issue #444).

Never touches a real torrent client, rclone binary or remote: the torrent client login and
`anyio.run_process` are monkeypatched throughout.
"""

import anyio

import salmon.commands as commands_module
from salmon import cfg
from salmon.commands import _test_seedbox_connections, commandgroup
from salmon.config.validations import Seedbox
from salmon.uploader.torrent_client import QBittorrentClient

TORRENT_CLIENT_URL = "qbittorrent+http://user:pass@127.0.0.1:8080"


def _seedbox(**overrides) -> Seedbox:
    kwargs = {
        "name": "test-seedbox",
        "enabled": True,
        "url": "myremote",
        "type": "rclone",
        "torrent_client": TORRENT_CLIENT_URL,
    }
    kwargs.update(overrides)
    return Seedbox(**kwargs)


def _run_seedbox_check() -> None:
    anyio.run(_test_seedbox_connections)


def test_torrent_client_login_failure_reports_connection_failed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(type="local")])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: None)

    _run_seedbox_check()

    assert "connection failed" in capsys.readouterr().out.lower()


def test_torrent_client_login_success_reports_successful(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(type="local")])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())

    _run_seedbox_check()

    assert "successful" in capsys.readouterr().out.lower()


def test_rclone_lsd_success_reports_accessible(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg, "seedbox", [_seedbox()])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())
    monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/usr/bin/rclone")

    class FakeResult:
        returncode = 0
        stdout = b""
        stderr = b""

    async def fake_run_process(cmd, check=True):
        assert cmd[:2] == ["rclone", "lsd"]
        return FakeResult()

    monkeypatch.setattr(commands_module.anyio, "run_process", fake_run_process)

    _run_seedbox_check()

    assert "accessible" in capsys.readouterr().out.lower()


def test_rclone_lsd_failure_reports_failed_with_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg, "seedbox", [_seedbox()])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())
    monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/usr/bin/rclone")

    class FakeResult:
        returncode = 1
        stdout = b""
        stderr = b"directory not found"

    async def fake_run_process(cmd, check=True):
        return FakeResult()

    monkeypatch.setattr(commands_module.anyio, "run_process", fake_run_process)

    _run_seedbox_check()

    out = capsys.readouterr().out.lower()
    assert "failed" in out
    assert "directory not found" in out


def test_seedboxhealth_command_does_not_exist() -> None:
    assert "seedboxhealth" not in commandgroup.commands
