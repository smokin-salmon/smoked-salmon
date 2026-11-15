"""Only the release workflow sets the version and adds a changelog entry (docs/ci-and-releases.md, Releases; #464).

`release_notification.py` reads `src/salmon/data/version.toml` from master on GitHub to tell every user a new
version is out, so a PR that changed `current` or added a `[[changelog]]` entry would announce a release that does
not exist. The release workflow sets `current`, the newest `[[changelog]]` entry and `pyproject.toml`'s `version`
together, so they always agree on master; a PR that changes one of them alone does not. No exception.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import get_type_hints

import msgspec

from salmon.release_notification import ChangelogEntry, VersionData

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_the_changelog_and_the_package_agree() -> None:
    data = tomllib.loads((ROOT / "src" / "salmon" / "data" / "version.toml").read_text(encoding="utf-8"))
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    newest = data["changelog"][0]["version"]
    assert data["current"] == newest == package, (
        f"version.toml current = {data['current']!r}, its newest [[changelog]] entry = {newest!r}, pyproject.toml "
        f"version = {package!r}: only the release workflow changes these "
        "(docs/ci-and-releases.md, Releases). Revert the change to them; "
        "release notes go in the GitHub release's description."
    )


# Every installed salmon decodes master's version.toml into these (release_notification.py). A field renamed or removed
# there makes older clients fail to read the file, so the update check goes blind for them. Adding a field is fine.
CLIENT_FIELDS = {
    "VersionData": {"current": str, "changelog": list[ChangelogEntry]},
    "ChangelogEntry": {"version": str, "notes": str, "date": str},
}


def test_the_version_file_keeps_the_fields_older_clients_read() -> None:
    for struct in (VersionData, ChangelogEntry):
        have = get_type_hints(struct)
        for name, expected in CLIENT_FIELDS[struct.__name__].items():
            assert have.get(name) == expected, (
                f"{struct.__name__}.{name} is now {have.get(name)!r}, it was {expected!r}: older clients decode "
                "version.toml with this and would stop reading it. Keep the field as it is (adding one is fine)."
            )


def test_the_committed_version_file_decodes_as_clients_do() -> None:
    path = ROOT / "src" / "salmon" / "data" / "version.toml"
    data = msgspec.toml.decode(path.read_bytes(), type=VersionData)
    assert data.changelog, "version.toml has no [[changelog]] entry"


CHECK_SCRIPT = ROOT / ".github" / "scripts" / "check-release-version.py"


def run_check(version: str, version_file: Path) -> subprocess.CompletedProcess[str]:
    """The release workflow's check, as it runs there: $VERSION is the tag, in a process of its own."""
    env = {**os.environ, "VERSION": version}
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), str(version_file)], env=env, capture_output=True, text=True, check=False
    )


def test_the_release_check_accepts_a_file_at_the_tag(tmp_path: Path) -> None:
    path = tmp_path / "version.toml"
    path.write_text('current = "0.25.0"\n[[changelog]]\nversion = "0.25.0"\ndate = "2026-01-01"\nnotes = "n"\n')
    assert run_check("0.25.0", path).returncode == 0


def test_the_release_check_refuses_what_clients_could_not_use(tmp_path: Path) -> None:
    good = 'current = "0.25.0"\n[[changelog]]\nversion = "0.25.0"\ndate = "2026-01-01"\nnotes = "n"\n'
    cases = {
        "wrong current": ("0.26.0", good),
        "changelog behind": ("0.25.0", good.replace('version = "0.25.0"', 'version = "0.24.0"')),
        "does not decode": ("0.25.0", good.replace('date = "2026-01-01"', "date = 5")),
        "tag with a v": ("v0.25.0", good),
        "tag with a suffix": ("0.25.0-rc1", good),
    }
    for name, (tag, text) in cases.items():
        path = tmp_path / "version.toml"
        path.write_text(text)
        assert run_check(tag, path).returncode == 1, name
