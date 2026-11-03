"""No function in src/salmon grows more tangled without the PR saying so.

Ruff holds functions to a complexity of 20 (C901, `max-complexity` in pyproject.toml). The few that were already
over it carry `# noqa: C901`, which would let them grow without limit, so ALLOWED gives each its score: it may
rise by MARGIN over its entry, so a fix that adds a branch or two goes through, and no further. A PR that needs
more splits the function, or raises its entry and says why in its description. A new `# noqa: C901` fails too: a
function over the limit must be listed here.

The list only tightens: once a function is MARGIN under its entry, the entry must come down to its new score, and
a function back under the limit loses both its entry and its `# noqa`.
"""

import json
import subprocess
import tomllib
from pathlib import Path

from ruff.__main__ import find_ruff_bin

ROOT = Path(__file__).parent.parent
MARGIN = 5

# (path under src/salmon, function name): its complexity when the entry was last set. Keep it sorted.
ALLOWED = {
    ("tagger/ai_review.py", "review_metadata_with_ai"): 22,
    ("tagger/combine.py", "combine_metadatas"): 34,
    ("tagger/combine.py", "combine_tracks"): 22,
    ("tagger/retagger.py", "rename_files"): 21,
    ("trackers/base.py", "_send"): 33,
    ("uploader/__init__.py", "_upload_staged"): 49,
}


def max_complexity() -> int:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"]


def complexities() -> dict[tuple[str, str], int]:
    """The complexity of each function in src/salmon over the limit, whatever its `# noqa` says."""
    result = subprocess.run(
        [find_ruff_bin(), "check", "src/salmon", "--select", "C901", "--ignore-noqa", "--no-cache"]
        + ["--output-format", "json", "--exit-zero"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    scores = {}
    for finding in json.loads(result.stdout):
        # "`name` is too complex (34 > 20)"
        name = finding["message"].split("`")[1]
        score = int(finding["message"].rsplit("(", 1)[1].split()[0])
        path = Path(finding["filename"]).relative_to(ROOT / "src" / "salmon").as_posix()
        scores[(path, name)] = score
    return scores


def complexity_problems(
    scores: dict[tuple[str, str], int], allowed: dict[tuple[str, str], int], margin: int
) -> list[str]:
    """What is wrong with scores (the functions over the limit) against allowed, one line each."""
    problems = []
    for (path, name), score in scores.items():
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
        if (path, name) not in scores
    )
    return problems


def test_functions_stay_within_their_allowed_complexity():
    problems = complexity_problems(complexities(), ALLOWED, MARGIN)
    assert not problems, "\n".join(problems)


def test_the_list_is_sorted_and_over_the_limit():
    assert list(ALLOWED) == sorted(ALLOWED)
    assert all(score > max_complexity() for score in ALLOWED.values())


def test_a_new_function_over_the_limit_fails_even_with_a_noqa():
    assert complexity_problems({("new.py", "f"): 21}, {}, 5) == [
        "new.py: f has a complexity of 21, over the limit: split it, or add it to ALLOWED and say why in the PR "
        "description"
    ]


def test_a_listed_function_may_grow_by_the_margin_and_no_more():
    assert complexity_problems({("big.py", "f"): 35}, {("big.py", "f"): 30}, 5) == []
    assert complexity_problems({("big.py", "f"): 36}, {("big.py", "f"): 30}, 5) == [
        "big.py: f has a complexity of 36, 6 over its entry of 30: split it, or raise its entry to 36 and say why "
        "in the PR description"
    ]


def test_the_list_tightens_when_a_function_gets_simpler():
    assert complexity_problems({("big.py", "f"): 25}, {("big.py", "f"): 30}, 5) == []
    assert complexity_problems({("big.py", "f"): 24}, {("big.py", "f"): 30}, 5) == [
        "big.py: f has a complexity of 24, well under its entry of 30: lower it"
    ]
    assert complexity_problems({}, {("big.py", "f"): 22}, 5) == [
        "big.py: f is back under the limit or gone: remove its entry, and its `# noqa: C901`"
    ]
