"""Nothing writes `cfg` at runtime (AGENTS.md, Conventions; #634).

`salmon.cfg` is the user's config as loaded, shared by everything a process runs: `salmon web` runs several jobs
at once, and a run that changed a setting for itself (`-yyy` once set `yes_all`) would change it for the others.
What a run decides goes through its own state, e.g. `await interaction.assume_defaults()`. The walk refuses, on
anything under `cfg` (however it was imported) in `src/salmon`: an assignment (plain, augmented, unpacked, or the
target of `for` or `with`), `del`, `setattr` / `delattr` (`object.__setattr__` too), and a call of a method that
changes a list, dict or set in place (`append`, `update`...). A module-level alias (`tracker_cfg = cfg.tracker`,
annotated or not) counts as `cfg`. It also refuses binding `salmon.cfg` itself (`salmon.cfg = other`,
`setattr(salmon, "cfg", other)`), which every module reading `salmon.cfg` would then see; the one place that sets
it, while salmon is still importing, is listed below for one binding: a second in that function is new. What it
cannot see: a part of the config bound to another name inside a function, or an alias imported from another module.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "salmon"
# The methods of list, dict and set that change them in place.
MUTATING = {
    "append", "extend", "insert", "remove", "pop", "popitem", "clear", "update", "setdefault", "add", "discard",
    "sort", "reverse", "difference_update", "intersection_update", "symmetric_difference_update", "__setitem__",
    "__delitem__", "__setattr__", "__delattr__",
}  # fmt: skip
# (file under src/salmon, enclosing function, what it binds): why it is not a write at runtime.
ALLOWED = {
    ("config/first_run.py", "check_connection", "salmon.cfg"): (
        "sets cfg the first time, for a new user's config, while salmon is still importing and nothing has read it"
    ),
}


def _cfg_names(tree: ast.Module) -> set[str]:
    """The names `salmon.cfg` is bound to in the module, plus `salmon.cfg` itself."""
    names = {"salmon.cfg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "salmon":
            names |= {alias.asname or alias.name for alias in node.names if alias.name == "cfg"}
        elif isinstance(node, ast.Import):
            names |= {f"{alias.asname}.cfg" for alias in node.names if alias.name == "salmon" and alias.asname}
    # Module-level aliases of a part of it, in order: `tracker_cfg = cfg.tracker`, annotated or not.
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            value, targets = statement.value, statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            value, targets = statement.value, [statement.target]
        else:
            continue
        if _dotted(value) in names or _under(value, names):
            names |= {target.id for target in targets if isinstance(target, ast.Name)}
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


def config_writes(source: str) -> list[tuple[int, str, str]]:
    """The (line, enclosing function, what) of each write to the config, or binding of `salmon.cfg` itself."""
    tree = ast.parse(source)
    names = _cfg_names(tree)
    # The names the salmon module itself is bound to, for `setattr(salmon, "cfg", other)`.
    modules = {name.removesuffix(".cfg") for name in names}

    def is_config(node: ast.expr) -> bool:
        return _dotted(node) in names or _under(node, names)

    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = [*scope, node.name]
        where = ".".join(scope) or "the module"
        if isinstance(node, ast.Call):
            func = node.func
            setter = isinstance(func, ast.Name) and func.id in ("setattr", "delattr")
            setter |= isinstance(func, ast.Attribute) and _dotted(func) in ("object.__setattr__", "object.__delattr__")
            if setter and node.args:
                rebinds = len(node.args) > 1 and _dotted(node.args[0]) in modules
                rebinds &= isinstance(node.args[1], ast.Constant) and node.args[1].value == "cfg"
                if is_config(node.args[0]) or rebinds:
                    found.append((node.lineno, where, ast.unparse(node)))
            elif isinstance(func, ast.Attribute) and func.attr in MUTATING and is_config(func.value):
                found.append((node.lineno, where, ast.unparse(node)))
        # An attribute bound as `salmon.cfg` rebinds the config itself; a plain name (`cfg = ...`) is local.
        found.extend(
            (each.lineno, where, ast.unparse(each))
            for target in _targets(node)
            for each in _unpacked(target)
            if _under(each, names) or (isinstance(each, ast.Attribute) and _dotted(each) in names)
        )
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, [])
    return sorted(found)


def _all_writes() -> list[tuple[str, int, str, str]]:
    return [
        (path.relative_to(SRC).as_posix(), line, where, what)
        for path in sorted(SRC.rglob("*.py"))
        for line, where, what in config_writes(path.read_text(encoding="utf-8"))
    ]


def test_nothing_in_salmon_writes_the_config() -> None:
    found: list[str] = []
    listed: set[tuple[str, str, str]] = set()
    for file, line, where, what in _all_writes():
        # A listed function holds one such binding: the first is the one listed, a second is new.
        if (file, where, what) in ALLOWED and (file, where, what) not in listed:
            listed.add((file, where, what))
        else:
            found.append(f"src/salmon/{file}:{line}: {what} (in {where})")
    assert found == [], (
        "cfg is the config as loaded, shared by every job of the process: keep what a run decides in the run's own "
        "state (salmon.interaction.assume_defaults() for -yyy) instead of writing it to cfg:\n" + "\n".join(found)
    )


def test_every_listed_binding_still_exists() -> None:
    gone = sorted(set(ALLOWED) - {(file, where, what) for file, _, where, what in _all_writes()})
    assert gone == [], f"remove these from ALLOWED in tests/test_rule_cfg_is_read_only.py, they are gone: {gone}"


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
red_cfg: object = config.tracker.red
red_cfg.x = 1
config.upload.formats.difference_update({"MP3"})
def load(new):
    salmon.cfg = new
    config = new
    setattr(s, "cfg", new)
    setattr(s, "other", new)
"""
    found = config_writes(source)
    assert [line for line, _, _ in found] == [4, 5, 6, 7, 8, 10, 10, 11, 12, 15, 16, 18, 20, 21, 23, 25]
    assert found[-2:] == [(23, "load", "salmon.cfg"), (25, "load", "setattr(s, 'cfg', new)")]
