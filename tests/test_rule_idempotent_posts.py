"""A request that changes state is not re-sent once it may have reached the tracker (AGENTS.md; #446, #448).

`_request` treats a POST as not idempotent: it retries it only when the tracker cannot have acted on it, and raises
`UnknownOutcomeError` otherwise. A caller passes `idempotent=True` only for a POST that sets a fixed state, so that
sending it twice leaves the tracker as sending it once. Each such call is listed below with why; a new one is
checked against that rule, then added. A listed function holds one such call: a second is a new one.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "salmon"
# Where `idempotent` falls among `_request`'s positional parameters (method, url, params, data, timeout_secs,
# prefer_api_key, idempotent).
POSITION = 6
# (file under src/salmon, enclosing function): why sending it twice changes nothing.
ALLOWED = {
    ("trackers/base.py", "BaseGazelleApi.append_to_torrent_description"): (
        "posts every field of the edit form as read, with the description computed before the request"
    ),
}


def idempotent_calls(source: str) -> list[tuple[int, str]]:
    """The (line, enclosing function) of each call passing `idempotent` anything but False or None.

    By keyword to any call, or by position to `_request`.
    """
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = [*scope, node.name]
        if isinstance(node, ast.Call):
            values = [keyword.value for keyword in node.keywords if keyword.arg == "idempotent"]
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name == "_request" and len(node.args) > POSITION:
                values.append(node.args[POSITION])
            for value in values:
                if not (isinstance(value, ast.Constant) and value.value in (False, None)):
                    found.append((node.lineno, ".".join(scope) or "the module"))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source), [])
    return found


def _all_calls() -> dict[tuple[str, str], list[int]]:
    calls: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for line, where in idempotent_calls(path.read_text(encoding="utf-8")):
            calls.setdefault((path.relative_to(SRC).as_posix(), where), []).append(line)
    return calls


def test_every_idempotent_request_is_one_listed() -> None:
    calls = _all_calls()
    new = [
        f"src/salmon/{file}:{line}: {where}"
        for (file, where), lines in calls.items()
        for line in (lines if (file, where) not in ALLOWED else lines[1:])
    ]
    assert new == [], (
        "idempotent=True lets _request send these again after the tracker may have acted on them. Pass it only if "
        "sending the request twice leaves the tracker as sending it once (it sets a fixed state), then add the "
        "function to ALLOWED in tests/test_rule_idempotent_posts.py with why:\n" + "\n".join(new)
    )


def test_every_listed_request_still_exists() -> None:
    gone = sorted(set(ALLOWED) - set(_all_calls()))
    assert gone == [], f"remove these from ALLOWED, they no longer pass idempotent=True: {gone}"


def test_the_walk_finds_idempotent_calls() -> None:
    source = """
class Api:
    async def edit(self, flag):
        await self._request("POST", url, idempotent=True)
        await self._request("POST", url, idempotent=False)
        await self._request("POST", url, idempotent=flag)
        await self._request("POST", url, None, data, 10, False, True)
        await self._request("POST", url, None, data, 10, False, False)
"""
    assert idempotent_calls(source) == [(4, "Api.edit"), (6, "Api.edit"), (7, "Api.edit")]
