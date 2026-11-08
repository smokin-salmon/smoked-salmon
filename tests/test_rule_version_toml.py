"""Only the release workflow sets the version and adds a changelog entry (AGENTS.md, Releases; #464).

`release_notification.py` reads `src/salmon/data/version.toml` from master on GitHub to tell every user a new
version is out, so a PR that changed `current` or added a `[[changelog]]` entry would announce a release that does
not exist. The release workflow sets `current`, the newest `[[changelog]]` entry and `pyproject.toml`'s `version`
together, so they always agree on master; a PR that changes one of them alone does not. No exception.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_the_changelog_and_the_package_agree() -> None:
    data = tomllib.loads((ROOT / "src" / "salmon" / "data" / "version.toml").read_text(encoding="utf-8"))
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    newest = data["changelog"][0]["version"]
    assert data["current"] == newest == package, (
        f"version.toml current = {data['current']!r}, its newest [[changelog]] entry = {newest!r}, pyproject.toml "
        f"version = {package!r}: only the release workflow changes these (AGENTS.md, Releases). Revert the change "
        "to them; release notes go in the GitHub release's description."
    )
