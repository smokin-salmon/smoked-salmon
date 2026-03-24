from pathlib import Path

from fork_versioning import bump_base_version, compute_fork_release_version


def test_bump_base_version_patch() -> None:
    assert bump_base_version("0.10.1", "patch") == "0.10.2"


def test_bump_base_version_minor() -> None:
    assert bump_base_version("0.10.1", "minor") == "0.11.0"


def test_bump_base_version_rejects_non_stable_versions() -> None:
    try:
        bump_base_version("0.10.1-personal-fork.7", "patch")
    except ValueError as exc:
        assert "stable upstream version" in str(exc)
    else:
        raise AssertionError("Expected a ValueError for a non-stable upstream version.")


def test_compute_fork_release_version_uses_policy_file(tmp_path: Path) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text('[project]\nversion = "0.10.2"\n', encoding="utf-8")

    policy_path = tmp_path / "fork-versioning.toml"
    policy_path.write_text(
        'release_bump = "minor"\nreason = "Fork master currently carries feature work."\n',
        encoding="utf-8",
    )

    release = compute_fork_release_version(pyproject_path, policy_path, 17)

    assert release.base_version == "0.10.2"
    assert release.release_bump == "minor"
    assert release.next_version == "0.11.0"
    assert release.release_tag == "0.11.0-personal-fork.17"
    assert release.reason == "Fork master currently carries feature work."
