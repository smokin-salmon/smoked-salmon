import subprocess
from importlib import import_module

import anyio

# salmon.checks.__init__ defines a click command also named `integrity`, which shadows the
# submodule on the package object, so importlib is used to get the module itself.
integrity = import_module("salmon.checks.integrity")

# The real warning flac -wt prints when a file's STREAMINFO MD5 is unset (from #353). flac still
# exits 0 in this case, so the check must fail on the warning text, not on the return code alone.
MD5_UNSET_STDERR = b"track01.flac: WARNING, MD5 signature unset in STREAMINFO\ntrack01.flac: testing... ok\n"


def test_unset_streaminfo_md5_fails_the_check(monkeypatch) -> None:
    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 0, stdout=b"", stderr=MD5_UNSET_STDERR)

    monkeypatch.setattr(integrity.anyio, "run_process", fake_run_process)

    passed, output = anyio.run(integrity._check_flac_integrity, "track01.flac")

    assert passed is False
    assert "MD5 signature unset in STREAMINFO" in output
    assert "\u2014" not in output


def test_clean_flac_output_still_passes(monkeypatch) -> None:
    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 0, stdout=b"track01.flac: testing... ok\n", stderr=b"")

    monkeypatch.setattr(integrity.anyio, "run_process", fake_run_process)

    passed, output = anyio.run(integrity._check_flac_integrity, "track01.flac")

    assert passed is True
    assert "MD5" not in output
