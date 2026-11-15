"""The update check never ends a command in a traceback (#1067): whatever GitHub answers, one yellow line says the
check failed and the command runs as if it had not happened."""

import pytest
import requests

from salmon import cfg
from salmon.run import main

OK_TOML = b"""current = "{current}"

[[changelog]]
version = "{current}"
notes = "Something new"
date = "2030-01-01"
"""


class Answer:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


@pytest.fixture
def run_health(monkeypatch, capsys):
    """Run `salmon health` through main with GitHub answering `content`; return (exit code, output)."""

    def run(content: bytes):
        monkeypatch.setattr(cfg.upload, "update_notification", True, raising=False)
        monkeypatch.setattr(requests, "get", lambda url, timeout=None: Answer(content))
        monkeypatch.setattr("sys.argv", ["salmon", "health"])
        with pytest.raises(SystemExit) as ended:
            main()
        return ended.value.code, capsys.readouterr().out

    return run


def _check(code, out, failure: str):
    assert code in (0, None)
    assert "Required Dependencies:" in out, "the command did not run"
    lines = [line for line in out.splitlines() if "Update check failed" in line]
    assert len(lines) == 1
    assert failure in lines[0]
    assert "Traceback" not in out


def test_html_answer(run_health):
    code, out = run_health(b"<!DOCTYPE html><html><body>Rate limited</body></html>")
    _check(code, out, "DecodeError")


def test_unparsable_version(run_health):
    code, out = run_health(OK_TOML.replace(b"{current}", b"0.24.0-hotfix"))
    _check(code, out, "InvalidVersion")


def test_changelog_entry_without_date(run_health):
    toml = b'current = "99.0.0"\n\n[[changelog]]\nversion = "99.0.0"\nnotes = "x"\n'
    code, out = run_health(toml)
    _check(code, out, "ValidationError")


def test_newer_version_is_still_announced(run_health):
    code, out = run_health(OK_TOML.replace(b"{current}", b"99.0.0"))
    assert "Update available: v99.0.0" in out
    assert "Update check failed" not in out
    assert "Required Dependencies:" in out
