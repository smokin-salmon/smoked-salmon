"""A pull_request workflow runs a PR's code with nothing to push or publish with (.coderabbit.yaml, workflows; #579).

A workflow a pull request triggers runs code the PR author wrote. So it declares its token's permissions (rather
than taking the repository's default), none of them `write`, never logs in to a registry, and every checkout in it
sets `persist-credentials: false`, so the job's token is not left in `.git/config` for that code to read. The same
holds for the composite actions and reusable workflows of this repo it uses. No exception: a job that has to push
or publish runs on another event (the release and alpha workflows run on `release` and `push`).
"""

import re
from pathlib import Path

import pytest
from helpers.workflows import ROOT, WORKFLOWS, jobs, local_files, steps_using, triggers

PULL_REQUEST_EVENTS = {"pull_request", "pull_request_target"}
# `contents: write`, quoted or in a flow mapping, and `permissions: write-all`.
_WRITE = re.compile(r"""[\w-]+:\s*["']?write(?:-all)?["']?\s*(?:[,}#]|$)""")
_LOGIN = re.compile(
    r"login-action@|ecr-login@|\b(?:docker|podman|buildah|skopeo|helm|oras)\s+login\b|\bcrane\s+auth\s+login\b"
    r"|\bregistry-login\b"
)


def pull_request_workflows() -> list[Path]:
    return [path for path in WORKFLOWS if triggers(path) & PULL_REQUEST_EVENTS]


def _files_run(workflow: Path) -> list[Path]:
    """The workflow, and the actions and workflows of this repo it uses."""
    return [workflow, *local_files(workflow)]


def _matching(path: Path, pattern: re.Pattern[str]) -> list[str]:
    return [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]


def test_every_workflow_s_triggers_are_read() -> None:
    # Without this, an `on:` written in a way the reader misses would leave a workflow out of the tests below.
    unread = [path.name for path in WORKFLOWS if not triggers(path)]
    assert unread == [], f"no trigger read from the `on:` of {unread}: tests/helpers/workflows.py triggers() misses it"
    assert {"test.yml", "lint.yml", "docker-pr.yml"} <= {path.name for path in pull_request_workflows()}


@pytest.mark.parametrize(
    "on",
    [
        "on: [push, pull_request]",
        "on:\n  - push\n  - pull_request",
        "on:\n  push:\n    branches: [master]\n# PRs too\n  pull_request:\n    paths: [x]",
        '"on":\n  pull_request:',
    ],
)
def test_the_triggers_are_read_however_on_is_written(tmp_path: Path, on: str) -> None:
    workflow = tmp_path / "x.yml"
    workflow.write_text(f"name: x\n{on}\njobs:\n  a:\n    runs-on: x\n# b too\n  b:\n    runs-on: x\n")
    assert "pull_request" in triggers(workflow)
    assert [job.lines[0].strip() for job in jobs(workflow)] == ["a:", "b:"]


def test_a_pull_request_workflow_declares_its_permissions() -> None:
    found = []
    for path in pull_request_workflows():
        if re.search(r"^permissions:", path.read_text(encoding="utf-8"), re.MULTILINE):
            continue
        each = jobs(path)
        assert each, f"no job read under the `jobs:` of {path.name}"
        found += [job.where() for job in each if not any(re.match(r"^\s+permissions:", line) for line in job.lines)]
    assert found == [], (
        "give these pull_request jobs a `permissions:` block (read only, e.g. `contents: read`), so they do not take "
        "the repository's default token permissions:\n" + "\n".join(found)
    )


def test_a_pull_request_workflow_has_no_write_permission() -> None:
    found = [hit for path in pull_request_workflows() for used in _files_run(path) for hit in _matching(used, _WRITE)]
    assert found == [], (
        "a pull_request workflow runs the PR's code: give it read permissions only, and do what needs write in a "
        "workflow run on push or release:\n" + "\n".join(found)
    )


def test_a_pull_request_workflow_logs_in_to_no_registry() -> None:
    found = [hit for path in pull_request_workflows() for used in _files_run(path) for hit in _matching(used, _LOGIN)]
    assert found == [], (
        "a pull_request workflow runs the PR's code: log in to a registry only in a workflow run on push or "
        "release:\n" + "\n".join(found)
    )


def test_every_checkout_of_a_pull_request_workflow_keeps_no_credentials() -> None:
    checkouts = [
        step
        for path in pull_request_workflows()
        for used in _files_run(path)
        for step in steps_using(used, "actions/checkout")
    ]
    assert checkouts, "no actions/checkout step found in the pull_request workflows"
    found = [
        step.where()
        for step in checkouts
        if not any(re.match(r"^\s*persist-credentials:\s*false\b", line) for line in step.lines)
    ]
    assert found == [], (
        "add `with: persist-credentials: false` to these checkouts, so the job's token is not left in .git/config "
        "for the PR's code:\n" + "\n".join(found)
    )
