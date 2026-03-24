from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SEMVER_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
VALID_RELEASE_BUMPS = frozenset({"patch", "minor", "major"})


@dataclass(frozen=True)
class ForkReleaseVersion:
    base_version: str
    release_bump: str
    next_version: str
    release_tag: str
    reason: str


def _parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError(
            f"Expected a stable upstream version in pyproject.toml, got {version!r}. "
            "Keep pyproject.toml synced to the upstream base version."
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def bump_base_version(version: str, release_bump: str) -> str:
    if release_bump not in VALID_RELEASE_BUMPS:
        raise ValueError(f"Unsupported release bump {release_bump!r}. Expected one of {sorted(VALID_RELEASE_BUMPS)}.")

    major, minor, patch = _parse_semver(version)
    if release_bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if release_bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major + 1}.0.0"


def read_pyproject_version(pyproject_path: Path) -> str:
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    version = data["project"]["version"]
    if not isinstance(version, str):
        raise ValueError(f"Expected project.version to be a string in {pyproject_path}.")
    return version


def read_release_bump(policy_path: Path) -> tuple[str, str]:
    data = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    release_bump = data["release_bump"]
    if not isinstance(release_bump, str):
        raise ValueError(f"Expected release_bump to be a string in {policy_path}.")

    reason = data.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        raise ValueError(f"Expected reason to be a string in {policy_path}.")

    return release_bump, reason


def compute_fork_release_version(pyproject_path: Path, policy_path: Path, run_number: int | str) -> ForkReleaseVersion:
    base_version = read_pyproject_version(pyproject_path)
    release_bump, reason = read_release_bump(policy_path)
    next_version = bump_base_version(base_version, release_bump)
    return ForkReleaseVersion(
        base_version=base_version,
        release_bump=release_bump,
        next_version=next_version,
        release_tag=f"{next_version}-personal-fork.{run_number}",
        reason=reason,
    )
