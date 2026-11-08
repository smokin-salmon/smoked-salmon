"""Read GitHub workflow files for the tests of the CI rules (tests/test_rule_pinned.py, test_rule_*workflow*.py).

Line-based, as no YAML parser is a dependency: enough for the block style these files are written in (one key per
line, block scalars for scripts), not for YAML in general. The tests also check they found what they look for (the
triggers of every workflow, the steps, the scripts), so a file written another way fails instead of passing unread.
"""

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted(path for path in (ROOT / ".github" / "workflows").glob("*") if path.suffix in (".yml", ".yaml"))
# Composite actions of this repo, which workflows use as `./.github/actions/<name>`.
LOCAL_ACTIONS = sorted((ROOT / ".github" / "actions").glob("*/action.y*ml"))

# A script: `run:` of a step, or `script:` of actions/github-script, whose expressions are pasted in the same way.
_SCRIPT = re.compile(r"^(?P<indent>\s*)(?:-\s+)?(?:run|script):\s*(?P<value>.*)$")
_STEP = re.compile(r"^(?P<indent>\s*)-\s")
_LOCAL_USES = re.compile(r"uses:\s*\./(\.github/(?:actions/[\w.-]+|workflows/[\w.-]+\.ya?ml))")


@dataclass(frozen=True)
class Block:
    """Lines of a file, from `first` (1-based), as written."""

    path: Path
    first: int
    lines: list[str]

    def where(self, offset: int = 0) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.first + offset}"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _children(lines: list[str], start: int) -> list[tuple[int, str]]:
    """The (index, line) of the lines nested under lines[start], up to the next line as little indented.

    A comment ends nothing, wherever it starts.
    """
    parent = _indent(lines[start])
    nested: list[tuple[int, str]] = []
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#") and _indent(lines[i]) <= parent:
            break
        nested.append((i, lines[i]))
    return nested


def scripts(path: Path) -> list[Block]:
    """Every `run:` (and github-script `script:`) script in the file: the value on its line, and the block under it."""
    lines = _lines(path)
    found: list[Block] = []
    for i, line in enumerate(lines):
        match = _SCRIPT.match(line)
        if match is None:
            continue
        value = match["value"]
        if not value.startswith(("|", ">")):
            found.append(Block(path, i + 1, [value]))
            continue
        stripped = line.lstrip()
        key_indent = _indent(line) + len(stripped) - len(stripped.removeprefix("-").lstrip())
        body: list[str] = []
        for later in lines[i + 1 :]:
            if later.strip() and _indent(later) <= key_indent:
                break
            body.append(later)
        found.append(Block(path, i + 2, body))
    return found


def steps_using(path: Path, action: str) -> list[Block]:
    """Every step whose `uses:` names `action` (e.g. `actions/checkout`), from its `-` line to the next step."""
    lines = _lines(path)
    found: list[Block] = []
    for i, line in enumerate(lines):
        if not re.search(rf"\buses:\s*{re.escape(action)}@", line):
            continue
        start = i
        while start > 0 and not (_STEP.match(lines[start]) and _indent(lines[start]) <= _indent(line)):
            start -= 1
        end = i + 1
        while end < len(lines) and not (lines[end].strip() and _indent(lines[end]) <= _indent(lines[start])):
            end += 1
        found.append(Block(path, start + 1, lines[start:end]))
    return found


def _top_level(lines: list[str], key: str) -> int | None:
    pattern = re.compile(rf"^[\"']?{key}[\"']?:")
    return next((i for i, line in enumerate(lines) if pattern.match(line)), None)


def triggers(path: Path) -> set[str]:
    """The events under the workflow's top-level `on:`: a list on its line, a block list, or one key per event."""
    lines = _lines(path)
    start = _top_level(lines, "on")
    if start is None:
        return set()
    inline = lines[start].split(":", 1)[1].split("#", 1)[0].strip()
    if inline:
        return set(re.findall(r"[a-z_]+", inline))
    nested = [line for _, line in _children(lines, start) if line.strip() and not line.strip().startswith("#")]
    if not nested:
        return set()
    level = _indent(nested[0])
    return {
        match[1] for line in nested if _indent(line) == level if (match := re.match(r"^\s*(?:-\s*)?([a-z_]+)", line))
    }


def jobs(path: Path) -> list[Block]:
    """Each job under the top-level `jobs:`, from its name's line to the next job's."""
    lines = _lines(path)
    start = _top_level(lines, "jobs")
    if start is None:
        return []
    nested = [(i, line) for i, line in _children(lines, start) if line.strip() and not line.strip().startswith("#")]
    if not nested:
        return []
    level = _indent(nested[0][1])
    heads = [i for i, line in nested if _indent(line) == level]
    ends = [*heads[1:], nested[-1][0] + 1]
    return [Block(path, head + 1, lines[head:end]) for head, end in zip(heads, ends, strict=True)]


def local_files(path: Path) -> list[Path]:
    """The composite actions and reusable workflows of this repo that the file uses, with theirs in turn."""
    seen: list[Path] = []
    pending = [path]
    while pending:
        for relative in _LOCAL_USES.findall(pending.pop().read_text(encoding="utf-8")):
            used = ROOT / relative
            if used.is_dir():
                used = next(iter(sorted(used.glob("action.y*ml"))), used / "action.yml")
            if used.is_file() and used not in seen and used != path:
                seen.append(used)
                pending.append(used)
    return seen
