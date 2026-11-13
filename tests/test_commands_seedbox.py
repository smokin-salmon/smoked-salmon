"""`salmon checkconf --seedbox` actually contacts the seedbox and rclone remote (issue #444).

Never touches a real torrent client, rclone binary or remote: the torrent client login and
`anyio.run_process` are monkeypatched throughout.
"""

import shutil
import subprocess

import anyio
import pytest
import qbittorrentapi

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

    async def fake_run_process(cmd, check=True, **_kwargs):
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

    async def fake_run_process(cmd, check=True, **_kwargs):
        return FakeResult()

    monkeypatch.setattr(commands_module.anyio, "run_process", fake_run_process)

    _run_seedbox_check()

    out = capsys.readouterr().out.lower()
    assert "failed" in out
    assert "directory not found" in out


def test_seedboxhealth_command_does_not_exist() -> None:
    assert "seedboxhealth" not in commandgroup.commands


def test_a_credential_bearing_remote_is_not_printed(monkeypatch, capsys) -> None:
    # rclone accepts a connection string as the remote, so the url itself can carry a password.
    remote = ":sftp,host=box,user=dean,pass=hunter2"
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(url=remote, torrent_client="qbittorrent+http://127.0.0.1:8080")])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())
    monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/usr/bin/rclone")

    class FakeResult:
        returncode = 0
        stdout = b""
        stderr = b""

    async def fake_run_process(cmd, check=True, **_kwargs):
        return FakeResult()

    monkeypatch.setattr(commands_module.anyio, "run_process", fake_run_process)

    _run_seedbox_check()

    out = capsys.readouterr().out
    assert "URL: :sftp,host=box,user=dean,pass=[REDACTED]" in out
    assert "is accessible" in out
    assert "hunter2" not in out


def test_failed_login_output_never_contains_the_password(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cfg, "seedbox", [_seedbox(type="local", torrent_client="qbittorrent+http://user:hunter2@box:8080")]
    )

    def refuse(self):
        raise qbittorrentapi.APIConnectionError("Failed to connect to http://user:hunter2@box:8080?password=hunter2")

    monkeypatch.setattr(qbittorrentapi.Client, "auth_log_in", refuse)

    _run_seedbox_check()

    out = capsys.readouterr().out
    assert "connection failed" in out.lower()
    assert "hunter2" not in out


def test_an_unstructured_login_error_never_contains_the_password(monkeypatch, capsys) -> None:
    # No URL or password= for a pattern to find: only the configured value itself identifies it.
    monkeypatch.setattr(
        cfg, "seedbox", [_seedbox(type="local", torrent_client="qbittorrent+http://user:hunter2@box:8080")]
    )

    def refuse(self):
        raise qbittorrentapi.APIConnectionError("authentication failed for hunter2")

    monkeypatch.setattr(qbittorrentapi.Client, "auth_log_in", refuse)

    _run_seedbox_check()

    out = capsys.readouterr().out
    assert "authentication failed" in out
    assert "hunter2" not in out


def test_rclone_failure_output_never_contains_the_password(monkeypatch, capsys) -> None:
    remote = ":sftp,host=box,user=dean,pass=hunter2"
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(url=remote)])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())
    monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/usr/bin/rclone")

    class FakeResult:
        returncode = 1
        stdout = b""
        stderr = (
            b'CRITICAL: Failed to create file system for ":sftp,host=box,user=dean,pass=hunter2:": '
            b"couldn't connect to sftp://dean:hunter2@box\nauthentication failed for hunter2"
        )

    async def fake_run_process(cmd, check=True, **_kwargs):
        return FakeResult()

    monkeypatch.setattr(commands_module.anyio, "run_process", fake_run_process)

    _run_seedbox_check()

    out = capsys.readouterr().out
    # rclone's own stderr must be shown, masked, not just the probe's verdict.
    assert "authentication failed" in out
    assert "hunter2" not in out


def test_a_failed_probe_never_repeats_a_configured_secret(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(extra_args=["--sftp-pass", "hunter2"])])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())
    monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/usr/bin/rclone")

    async def fake_run_process(cmd, check=True, **_kwargs):
        raise OSError("rclone refused hunter2")

    monkeypatch.setattr(commands_module.anyio, "run_process", fake_run_process)

    _run_seedbox_check()

    out = capsys.readouterr().out
    assert "rclone refused" in out
    assert "hunter2" not in out


def test_the_qobuz_connection_test_skips_the_shipped_placeholders(monkeypatch, capsys) -> None:
    """config.default.toml's placeholder Qobuz values are truthy; they must read as unset, with no request."""
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "app-id")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "user_auth_token")
    asked: list[str] = []

    async def fake_fetch_data(_self, url, *_args, **_kwargs):
        asked.append(url)

    monkeypatch.setattr(commands_module.salmon.sources.QobuzBase, "fetch_data", fake_fetch_data)
    for other in (commands_module.salmon.sources.DiscogsBase, commands_module.salmon.sources.TidalBase):
        monkeypatch.setattr(other, "fetch_data", lambda *_a, **_k: anyio.sleep(0))

    anyio.run(commands_module._test_metadata_sources)

    out = capsys.readouterr().out
    assert asked == []
    assert "Qobuz: Missing required configuration" in out


def _rclone_seedbox(monkeypatch, **overrides) -> list[tuple[list[str], object]]:
    """An rclone seedbox whose lsd succeeds; give back each command run and the stdin it was given."""
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(**overrides)])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())
    monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/usr/bin/rclone")
    ran: list[tuple[list[str], object]] = []

    async def fake_run_process(cmd, check=True, **kwargs):
        ran.append((list(cmd), kwargs.get("stdin")))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(commands_module.anyio, "run_process", fake_run_process)
    return ran


def test_the_rclone_check_reads_the_config_uploads_read(monkeypatch, capsys) -> None:
    ran = _rclone_seedbox(monkeypatch, extra_args=["--checksum", "--config", "/box/rclone.conf", "-q"])

    _run_seedbox_check()

    # No terminal: a config password prompt would wait there unseen.
    assert ran == [(["rclone", "lsd", "myremote:", "--config", "/box/rclone.conf"], subprocess.DEVNULL)]
    out = capsys.readouterr().out
    assert "✔ Rclone remote 'myremote' is accessible" in out


def test_extra_args_choosing_no_config_leave_the_check_as_it_was(monkeypatch) -> None:
    ran = _rclone_seedbox(monkeypatch, extra_args=["--checksum", "-P", "--sftp-path-override", "@/volume3"])

    _run_seedbox_check()

    assert ran == [(["rclone", "lsd", "myremote:"], subprocess.DEVNULL)]


def test_a_check_that_runs_out_of_time_says_so(monkeypatch, capsys) -> None:
    _rclone_seedbox(monkeypatch, extra_args=["--config", "/box/rclone.conf", "--password-command", "pinentry-wait"])
    monkeypatch.setattr(commands_module, "_RCLONE_CHECK_SECONDS", 0.05)

    async def never_answers(cmd, check=True, **_kwargs):
        await anyio.sleep_forever()

    monkeypatch.setattr(commands_module.anyio, "run_process", never_answers)

    _run_seedbox_check()

    out = capsys.readouterr().out
    assert "✖ Rclone remote 'myremote' check timed out after 0.05 seconds" in out


def _real_rclone(monkeypatch, tmp_path, url: str, extra_args: list[str]) -> None:
    """Run the check through the real rclone binary, on a memory remote "box" only tmp_path's config names."""
    (tmp_path / "rclone.conf").write_text("[box]\ntype = memory\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "no-such.conf"))
    monkeypatch.setenv("RCLONE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(url=url, extra_args=extra_args)])
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: object())
    _run_seedbox_check()


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
def test_real_rclone_reaches_a_remote_only_the_extra_args_config_names(monkeypatch, capsys, tmp_path) -> None:
    report = tmp_path / "combined.txt"
    conf = str(tmp_path / "rclone.conf")
    _real_rclone(monkeypatch, tmp_path, "box", ["-q", "--combined", str(report), "--max-depth", "5", "--config", conf])

    out = capsys.readouterr().out
    assert "✔ Rclone remote 'box' is accessible" in out
    assert not report.exists()


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
def test_real_rclone_failure_is_shown_when_extra_args_hold_logging_flags(monkeypatch, capsys, tmp_path) -> None:
    log = tmp_path / "rclone.log"
    conf = str(tmp_path / "rclone.conf")
    _real_rclone(monkeypatch, tmp_path, "nosuch", ["-q", "--log-file", str(log), "--config", conf])

    out = capsys.readouterr().out
    assert "✖ Rclone remote 'nosuch' failed: " in out
    assert "failed: exit code" not in out
    assert not log.exists()


# rclone.conf holding [box] type = memory, encrypted with the password "pw-for-test".
_ENCRYPTED_CONF = (
    "# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\n"
    "0/QW/BBzruHrQi0UBrH0Hj/W+fpb9FwHOos+imgHLomnsbz9rBV7wLRbePc4wFs/gHsziwHkzgvLg2v2Gg==\n"
)


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
def test_real_rclone_encrypted_config_without_its_password_fails_at_once(monkeypatch, capsys, tmp_path) -> None:
    conf = tmp_path / "encrypted.conf"
    conf.write_text(_ENCRYPTED_CONF)
    _real_rclone(monkeypatch, tmp_path, "box", ["--config", str(conf)])

    out = capsys.readouterr().out
    assert "✖ Rclone remote 'box' failed: " in out
    assert "timed out" not in out


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
def test_real_rclone_opens_an_encrypted_config_with_its_password_command(monkeypatch, capsys, tmp_path) -> None:
    conf = tmp_path / "encrypted.conf"
    conf.write_text(_ENCRYPTED_CONF)
    _real_rclone(monkeypatch, tmp_path, "box", ["--config", str(conf), "--password-command", "echo pw-for-test"])

    out = capsys.readouterr().out
    assert "✔ Rclone remote 'box' is accessible" in out
