"""Nothing writes `cfg` at runtime (AGENTS.md, Conventions; #634).

`salmon.cfg` is the user's config as loaded, shared by everything a process runs: `salmon web` runs several jobs
at once, and a run that changed a setting for itself (`-yyy` once set `yes_all`) would change it for the others.
What a run decides goes through its own state, e.g. `await interaction.assume_defaults()`. The walk refuses, on
anything under `cfg` (however it was imported) in `src/salmon`: an assignment (plain, augmented, unpacked, or the
target of `for` or `with`), `del`, `setattr` / `delattr` (`object.__setattr__` too), and a call of a method that
changes a list, dict or set in place (`append`, `update`...). A module-level alias (`tracker_cfg = cfg.tracker`)
counts as `cfg`. No exception. What it cannot see: a part of the config bound to another name inside a function,
or an alias imported from another module.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "salmon"
# The methods of list, dict and set that change them in place.
MUTATING = {
    "append", "extend", "insert", "remove", "pop", "popitem", "clear", "update", "setdefault", "add", "discard",
    "sort", "reverse", "__setitem__", "__delitem__", "__setattr__", "__delattr__",
}  # fmt: skip


def _cfg_names(tree: ast.Module) -> set[str]:
    """The names `salmon.cfg` is bound to in the module, plus `salmon.cfg` itself."""
    names = {"salmon.cfg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "salmon":
            names |= {alias.asname or alias.name for alias in node.names if alias.name == "cfg"}
        elif isinstance(node, ast.Import):
            names |= {f"{alias.asname}.cfg" for alias in node.names if alias.name == "salmon" and alias.asname}
    # Module-level aliases of a part of it, in order: `tracker_cfg = cfg.tracker`.
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and (_dotted(statement.value) in names or _under(statement.value, names)):
            names |= {target.id for target in statement.targets if isinstance(target, ast.Name)}
    return names


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return None if inner is None else f"{inner}.{node.attr}"
    return None


def _under(node: ast.expr, names: set[str]) -> bool:
    """Whether `node` is an attribute or item of the config, as `cfg.upload.yes_all` or `cfg.tracker["RED"]`."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
        if _dotted(node) in names:
            return True
    return False


def _unpacked(target: ast.expr) -> list[ast.expr]:
    """The targets an assignment to `target` binds: itself, or those of a tuple or list, starred or nested."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return [each for element in target.elts for each in _unpacked(element)]
    if isinstance(target, ast.Starred):
        return _unpacked(target.value)
    return [target]


def _targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, (ast.Assign, ast.Delete)):
        return node.targets
    if isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor, ast.comprehension)):
        return [node.target]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.optional_vars for item in node.items if item.optional_vars is not None]
    return []


def config_writes(source: str) -> list[tuple[int, str]]:
    """The (line, what) of each write to the config."""
    tree = ast.parse(source)
    names = _cfg_names(tree)

    def is_config(node: ast.expr) -> bool:
        return _dotted(node) in names or _under(node, names)

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            setter = isinstance(func, ast.Name) and func.id in ("setattr", "delattr")
            setter |= isinstance(func, ast.Attribute) and _dotted(func) in ("object.__setattr__", "object.__delattr__")
            if setter and node.args:
                if is_config(node.args[0]):
                    found.append((node.lineno, ast.unparse(node)))
            elif isinstance(func, ast.Attribute) and func.attr in MUTATING and is_config(func.value):
                found.append((node.lineno, ast.unparse(node)))
            continue
        found += [
            (each.lineno, ast.unparse(each))
            for target in _targets(node)
            for each in _unpacked(target)
            if _under(each, names)
        ]
    return sorted(found)


def test_nothing_in_salmon_writes_the_config() -> None:
    found = [
        f"src/salmon/{path.relative_to(SRC).as_posix()}:{line}: {target}"
        for path in sorted(SRC.rglob("*.py"))
        for line, target in config_writes(path.read_text(encoding="utf-8"))
    ]
    assert found == [], (
        "cfg is the config as loaded, shared by every job of the process: keep what a run decides in the run's own "
        "state (salmon.interaction.assume_defaults() for -yyy) instead of writing it to cfg:\n" + "\n".join(found)
    )


def test_the_walk_finds_config_writes() -> None:
    source = """
import salmon
from salmon import cfg as config
config.upload.yes_all = True
config.tracker["RED"].x += 1
setattr(config.upload, "yes_all", True)
salmon.cfg.upload = None
del config.upload.description
others.upload = 1
[config.upload.yes_all, *config.upload.formats] = True, 1
for config.upload.x in range(2): pass
config.upload.formats.append("MP3")
config.tracker.get("RED")
import salmon as s
s.cfg.upload.yes_all = True
object.__setattr__(config.upload, "yes_all", True)
upload_cfg = config.upload
upload_cfg.yes_all = True
"""
    assert [line for line, _ in config_writes(source)] == [4, 5, 6, 7, 8, 10, 10, 11, 12, 15, 16, 18]
