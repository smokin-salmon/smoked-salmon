"""No function in src/salmon or tests/ grows past the complexity limit, or past its allowance, unless the PR says why.

Ruff holds functions to a complexity of 20 (C901, `max-complexity` in pyproject.toml). The few that were already
over it carry `# noqa: C901`, which would let them grow without limit, so ALLOWED gives each its score: it may
rise by MARGIN over its entry, so a fix that adds a branch or two goes through, and no further. A PR that needs
more splits the function, or raises its entry and says why in its description. A new `# noqa: C901` fails too: a
function over the limit must be listed here.

The list only tightens: once a function is more than MARGIN under its entry, the entry must come down, and
a function back under the limit loses both its entry and its `# noqa` (ruff's RUF100 fails on the noqa left).
Entries are by module and name, so two functions of one name over the limit in one module fail rather than share
one.
"""

import json
import subprocess
import tomllib
from pathlib import Path

from ruff.__main__ import find_ruff_bin

ROOT = Path(__file__).parent.parent
MARGIN = 5

# (path in the repo, function name): its complexity when the entry was last set. Keep it sorted.
ALLOWED = {
    ("src/salmon/tagger/ai_review.py", "review_metadata_with_ai"): 22,
    ("src/salmon/tagger/combine.py", "combine_metadatas"): 34,
    ("src/salmon/tagger/combine.py", "combine_tracks"): 22,
    ("src/salmon/tagger/retagger.py", "rename_files"): 21,
    ("src/salmon/trackers/base.py", "_send"): 33,
    ("src/salmon/uploader/__init__.py", "_upload_staged"): 49,
    # The fake tracker server: one branch per endpoint it answers.
    ("tests/helpers/cross_upload.py", "_handle"): 23,
}


def max_complexity() -> int:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"]


def ruff_findings(*options: str) -> list[dict]:
    """Ruff's findings on src/salmon and tests/, with options added, as its JSON gives them."""
    result = subprocess.run(
        [
            find_ruff_bin(),
            "check",
            "src/salmon",
            "tests",
            "--no-cache",
            "--output-format",
            "json",
            "--exit-zero",
            *options,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def module(finding: dict) -> str:
    return Path(finding["filename"]).relative_to(ROOT).as_posix()


def complexities() -> list[tuple[tuple[str, str], int]]:
    """The complexity of each function in src/salmon and tests/ over the limit, whatever its `# noqa` says."""
    scores = []
    for finding in ruff_findings("--select", "C901", "--ignore-noqa"):
        # "`name` is too complex (34 > 20)"
        name = finding["message"].split("`")[1]
        score = int(finding["message"].rsplit("(", 1)[1].split()[0])
        scores.append(((module(finding), name), score))
    return scores


def complexity_problems(
    scores: list[tuple[tuple[str, str], int]], allowed: dict[tuple[str, str], int], margin: int
) -> list[str]:
    """What is wrong with scores (the functions over the limit) against allowed, one line each."""
    problems = []
    seen = set()
    for (path, name), score in scores:
        if (path, name) in seen:
            problems.append(
                f"{path}: more than one {name} is over the limit, and they would share one entry: rename or split one"
            )
            continue
        seen.add((path, name))
        entry = allowed.get((path, name))
        if entry is None:
            problems.append(
                f"{path}: {name} has a complexity of {score}, over the limit: split it, or add it to ALLOWED "
                "and say why in the PR description"
            )
        elif score > entry + margin:
            problems.append(
                f"{path}: {name} has a complexity of {score}, {score - entry} over its entry of {entry}: split it, "
                f"or raise its entry to {score} and say why in the PR description"
            )
        elif score < entry - margin:
            problems.append(f"{path}: {name} has a complexity of {score}, well under its entry of {entry}: lower it")
    problems.extend(
        f"{path}: {name} is back under the limit or gone: remove its entry, and its `# noqa: C901`"
        for path, name in allowed
        if (path, name) not in seen
    )
    return problems


def test_functions_stay_within_their_allowed_complexity():
    problems = complexity_problems(complexities(), ALLOWED, MARGIN)
    assert not problems, "\n".join(problems)


def test_the_list_is_sorted_and_over_the_limit():
    assert list(ALLOWED) == sorted(ALLOWED)
    assert all(score > max_complexity() for score in ALLOWED.values())


def test_a_new_function_over_the_limit_fails_even_with_a_noqa():
    assert complexity_problems([(("new.py", "f"), 21)], {}, 5) == [
        "new.py: f has a complexity of 21, over the limit: split it, or add it to ALLOWED and say why in the PR "
        "description"
    ]


def test_a_listed_function_may_grow_by_the_margin_and_no_more():
    assert complexity_problems([(("big.py", "f"), 35)], {("big.py", "f"): 30}, 5) == []
    assert complexity_problems([(("big.py", "f"), 36)], {("big.py", "f"): 30}, 5) == [
        "big.py: f has a complexity of 36, 6 over its entry of 30: split it, or raise its entry to 36 and say why "
        "in the PR description"
    ]


def test_the_list_tightens_when_a_function_gets_simpler():
    assert complexity_problems([(("big.py", "f"), 25)], {("big.py", "f"): 30}, 5) == []
    assert complexity_problems([(("big.py", "f"), 24)], {("big.py", "f"): 30}, 5) == [
        "big.py: f has a complexity of 24, well under its entry of 30: lower it"
    ]
    assert complexity_problems([], {("big.py", "f"): 22}, 5) == [
        "big.py: f is back under the limit or gone: remove its entry, and its `# noqa: C901`"
    ]


def test_two_functions_of_one_name_over_the_limit_in_one_module_fail():
    scores = [(("big.py", "f"), 30), (("big.py", "f"), 30)]
    assert complexity_problems(scores, {("big.py", "f"): 30}, 5) == [
        "big.py: more than one f is over the limit, and they would share one entry: rename or split one"
    ]
