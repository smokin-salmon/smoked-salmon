"""Nothing in salmon asks the user except through `salmon.interaction` (#630).

A question put to asyncclick or to `input()` directly would hang a web job on the server's own terminal, and no
error would say so. The walk below fails on one, however the function was imported.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "salmon"
ASKING = {"prompt", "confirm", "edit"}
CLICK_MODULES = {"asyncclick", "click"}
# Where a direct question is allowed: the terminal's implementation, and the first-run config creation,
# which runs before any interface exists and never runs in the web.
ALLOWED_FILES = {"interaction.py"}
ALLOWED_FUNCTIONS = {("config/__init__.py", "setup_config")}


def _is_click_module(name: str | None) -> bool:
    return bool(name) and name.split(".")[0] in CLICK_MODULES  # pyright: ignore[reportOptionalMemberAccess]


def direct_questions(source: str) -> list[tuple[int, str]]:
    """The (line, what) of each reference to a function that asks the user, other than through the interface."""
    tree = ast.parse(source)
    modules: set[str] = set()  # names bound to asyncclick itself
    functions: dict[str, str] = {}  # names bound to one of its asking functions, or to prompt_async
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_click_module(alias.name):
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if _is_click_module(node.module) and alias.name in ASKING:
                    functions[alias.asname or alias.name] = alias.name
                    found.append((node.lineno, f"imports {alias.name} from {node.module}"))
                elif _is_click_module(node.module) and alias.name == "*":
                    found.append((node.lineno, f"imports everything from {node.module}"))
                elif alias.name == "prompt_async":
                    functions[alias.asname or alias.name] = "prompt_async"
                    found.append((node.lineno, "imports prompt_async"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in modules and node.attr in ASKING:
                found.append((node.lineno, f"{node.value.id}.{node.attr}"))
            elif node.attr == "prompt_async":
                found.append((node.lineno, f"{ast.unparse(node)}"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in functions:
                found.append((node.lineno, f"{node.id} ({functions[node.id]})"))
            elif node.id == "input":
                found.append((node.lineno, "input()"))
            elif node.id == "prompt_async":
                found.append((node.lineno, "prompt_async"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"getattr", "vars"}:
            # getattr(click, "prompt") and the like.
            args = node.args
            if (
                node.func.id == "getattr"
                and len(args) >= 2
                and isinstance(args[0], ast.Name)
                and args[0].id in modules
                and isinstance(args[1], ast.Constant)
                and args[1].value in ASKING
            ):
                found.append((node.lineno, f"getattr({args[0].id}, {args[1].value!r})"))
    return sorted(set(found))


def _outside_allowed_functions(rel: str, source: str) -> list[tuple[int, str]]:
    allowed_lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (rel, node.name) in ALLOWED_FUNCTIONS:
            allowed_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [(line, what) for line, what in direct_questions(source) if line not in allowed_lines]


def direct_questions_under(root: Path) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWED_FILES:
            continue
        questions = _outside_allowed_functions(rel, path.read_text(encoding="utf-8"))
        if questions:
            found[rel] = questions
    return found


def test_nothing_asks_the_user_except_through_the_interaction_layer() -> None:
    assert direct_questions_under(SRC) == {}


@pytest.mark.parametrize(
    "source",
    [
        "import asyncclick as click\nclick.confirm('x')\n",
        "import asyncclick\nasyncclick.prompt('x')\n",
        "import asyncclick as c\nc.edit('x')\n",
        "from asyncclick import confirm\nconfirm('x')\n",
        "from asyncclick import prompt as ask\nask('x')\n",
        "from asyncclick.termui import edit\n",
        "import asyncclick as click\nask = click.confirm\n",
        "import asyncclick as click\nrun(click.prompt)\n",
        "import asyncclick as click\ngetattr(click, 'confirm')('x')\n",
        "x = input('x')\n",
        "from salmon.common import prompt_async\n",
        "from salmon.common import prompt_async as wait\nwait('x')\n",
        "import salmon.common\nsalmon.common.prompt_async('x')\n",
        "def f():\n    import asyncclick as click\n    return click.confirm('x')\n",
    ],
)
def test_the_walk_catches_a_direct_question_however_it_was_imported(source: str) -> None:
    assert direct_questions(source)


@pytest.mark.parametrize(
    "source",
    [
        "import asyncclick as click\nclick.echo('x')\nclick.style('x')\nclick.secho('x')\n",
        "from salmon import interaction\nawait_it = interaction.confirm('x')\n",
        "import asyncclick as click\n\nclass Thing:\n    def prompt(self): ...\n\nThing().prompt()\n",
        "def confirm(x): ...\nconfirm('x')\n",
    ],
)
def test_the_walk_leaves_alone_what_asks_nothing(source: str) -> None:
    assert direct_questions(source) == []


def test_the_walk_allows_setup_config_and_nothing_else_in_its_module() -> None:
    source = (
        "import asyncclick as click\n"
        "def setup_config():\n"
        "    return click.confirm('x')\n"
        "def other():\n"
        "    return click.confirm('y')\n"
    )
    assert _outside_allowed_functions("config/__init__.py", source) == [(5, "click.confirm")]
    assert _outside_allowed_functions("elsewhere.py", source) == [(3, "click.confirm"), (5, "click.confirm")]
