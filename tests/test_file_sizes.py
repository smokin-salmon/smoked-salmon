"""No module in src/salmon grows past its size without the PR saying so.

A module has at most LIMIT lines. The ones that were already longer are listed in BASELINE with their size: each
may grow by MARGIN lines over its entry, so a small fix goes through, and no further. A PR that needs more either
moves code out of the module or raises its entry and says why in its description, so the growth is a decision a
reviewer sees rather than drift.

The baseline only tightens: once a module is MARGIN lines under its entry, the entry must come down to its new
size, and a module back under LIMIT leaves the list.
"""

from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "salmon"
LIMIT = 600
MARGIN = 25

# Path under src/salmon: line count when it was last set. Keep it sorted.
BASELINE = {
    "checks/integrity.py": 635,
    "config/validations.py": 606,
    "cross_upload.py": 1496,
    "tagger/ai_review.py": 1288,
    "tagger/retagger.py": 995,
    "tagger/review.py": 697,
    "trackers/base.py": 1706,
    "uploader/__init__.py": 1938,
    "uploader/dupe_checker.py": 1155,
    "uploader/spectrals.py": 1536,
    "uploader/upload.py": 916,
    "webui/jobs.py": 778,
    "webui/kinds.py": 1022,
    "webui/server.py": 675,
}


def module_sizes(root: Path) -> dict[str, int]:
    """The line count of each Python module under root, by its path relative to root."""
    return {
        path.relative_to(root).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(root.rglob("*.py"))
    }


def size_problems(sizes: dict[str, int], baseline: dict[str, int], limit: int, margin: int) -> list[str]:
    """What is wrong with sizes against baseline, one line per module, empty when nothing is."""
    problems = []
    for name, lines in sizes.items():
        allowed = baseline.get(name)
        if allowed is None:
            if lines > limit:
                problems.append(
                    f"{name} has {lines} lines, over the {limit}-line limit: split it, or add it to BASELINE "
                    "and say why in the PR description"
                )
        elif lines > allowed + margin:
            problems.append(
                f"{name} has {lines} lines, {lines - allowed} over its baseline of {allowed}: move code out of it, "
                f"or raise its entry to {lines} and say why in the PR description"
            )
        elif lines <= limit:
            problems.append(f"{name} has {lines} lines, back under the {limit}-line limit: remove it from BASELINE")
        elif lines < allowed - margin:
            problems.append(f"{name} has {lines} lines, well under its baseline of {allowed}: lower it to {lines}")
    problems.extend(
        f"{name} is in BASELINE but no longer exists: remove its entry" for name in baseline if name not in sizes
    )
    return problems


def test_module_sizes_stay_within_their_baseline():
    problems = size_problems(module_sizes(SRC), BASELINE, LIMIT, MARGIN)
    assert not problems, "\n".join(problems)


def test_baseline_is_sorted():
    assert list(BASELINE) == sorted(BASELINE)


def test_a_new_module_over_the_limit_fails():
    assert size_problems({"new.py": 601}, {}, 600, 25) == [
        "new.py has 601 lines, over the 600-line limit: split it, or add it to BASELINE and say why in the PR "
        "description"
    ]
    assert size_problems({"new.py": 600}, {}, 600, 25) == []


def test_a_listed_module_may_grow_by_the_margin_and_no_more():
    assert size_problems({"big.py": 1025}, {"big.py": 1000}, 600, 25) == []
    assert size_problems({"big.py": 1026}, {"big.py": 1000}, 600, 25) == [
        "big.py has 1026 lines, 26 over its baseline of 1000: move code out of it, or raise its entry to 1026 "
        "and say why in the PR description"
    ]


def test_the_baseline_tightens_when_a_module_shrinks():
    assert size_problems({"big.py": 975}, {"big.py": 1000}, 600, 25) == []
    assert size_problems({"big.py": 974}, {"big.py": 1000}, 600, 25) == [
        "big.py has 974 lines, well under its baseline of 1000: lower it to 974"
    ]
    assert size_problems({"big.py": 600}, {"big.py": 610}, 600, 25) == [
        "big.py has 600 lines, back under the 600-line limit: remove it from BASELINE"
    ]


def test_a_removed_module_leaves_the_baseline():
    assert size_problems({}, {"gone.py": 900}, 600, 25) == [
        "gone.py is in BASELINE but no longer exists: remove its entry"
    ]
