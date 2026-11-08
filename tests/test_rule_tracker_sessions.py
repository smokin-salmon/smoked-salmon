"""Every tracker request goes through `BaseGazelleApi._request` (AGENTS.md, Tracker safety rules; #628, #641).

`_request` sends through `_send`, which takes the tracker account's rate limiter (5 requests in any 10.5 s, shared
by every client of the tracker) and its session: the kept-alive pool, or a session of one connection for a request
that is not idempotent. A session opened anywhere else in `salmon.trackers` would send past both. So in
`src/salmon/trackers/`:

- an HTTP session is made only in `BaseGazelleApi._new_session`, and no other HTTP client is imported;
- the tracker's sessions are reached only along `_request` -> `_send` -> `_session_for` -> `_http_session` /
  `_new_session`, and the pool they keep (`TrackerAccount.pool`, `BaseGazelleApi._session`) only where it is made
  and closed.

No exception. What this cannot see: a session to a tracker's URL opened outside `salmon.trackers`; review that.
"""

import ast
from pathlib import Path

TRACKERS = Path(__file__).resolve().parents[1] / "src" / "salmon" / "trackers"
# Who may make an HTTP session, or reach one of the tracker's: (enclosing function) for each name.
ALLOWED_CALLERS = {
    "ClientSession": {"BaseGazelleApi._new_session"},
    "_new_session": {"BaseGazelleApi._http_session", "BaseGazelleApi._session_for"},
    "_http_session": {"BaseGazelleApi._session_for"},
    "_session_for": {"BaseGazelleApi._send"},
    "_send": {"BaseGazelleApi._request"},
    "_session": {"BaseGazelleApi.__init__", "BaseGazelleApi._http_session", "BaseGazelleApi.close"},
    "pool": {"BaseGazelleApi._http_session", "BaseGazelleApi.close", "TrackerAccount.__init__", "_close_pools"},
}
SESSION_PATH = set(ALLOWED_CALLERS) - {"ClientSession"}
# What makes a session or sends a request on its own, by the name aiohttp exports it under.
AIOHTTP_MAKERS = {"ClientSession": "ClientSession", "request": "aiohttp.request"}
# The other HTTP clients (aiohttp's own one-off `aiohttp.request` is refused below).
REFUSED_IMPORTS = {"requests", "httpx", "urllib3", "urllib.request", "http.client", "pycurl"}


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def bypasses(source: str) -> list[tuple[int, str]]:
    """The (line, what) of each session made, or reached, outside the path above."""
    found: list[tuple[int, str]] = []
    # Names bound by `from aiohttp import ClientSession [as X]`, and what each makes.
    makers: dict[str, str] = {}

    def visit(node: ast.AST, scope: list[str]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = [*scope, node.name]
        where = ".".join(scope) or "the module"
        if isinstance(node, ast.Import):
            found.extend((node.lineno, f"import {a.name}") for a in node.names if a.name in REFUSED_IMPORTS)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if node.module in REFUSED_IMPORTS or f"{node.module}.{alias.name}" in REFUSED_IMPORTS:
                    found.append((node.lineno, f"from {node.module} import {alias.name}"))
                elif node.module.split(".")[0] == "aiohttp" and alias.name in AIOHTTP_MAKERS:
                    makers[alias.asname or alias.name] = AIOHTTP_MAKERS[alias.name]
        elif isinstance(node, ast.Call):
            func = node.func
            # A call only: an annotation such as `-> aiohttp.ClientSession` names the class without making one.
            if isinstance(func, ast.Attribute) and _name(func.value) == "aiohttp" and func.attr in AIOHTTP_MAKERS:
                made = AIOHTTP_MAKERS[func.attr]
            else:
                made = makers.get(func.id) if isinstance(func, ast.Name) else None
            if made == "ClientSession" and where in ALLOWED_CALLERS["ClientSession"]:
                made = None
            if made is not None:
                found.append((node.lineno, f"{made}() in {where}"))
        elif isinstance(node, ast.Attribute) and node.attr in SESSION_PATH and where not in ALLOWED_CALLERS[node.attr]:
            found.append((node.lineno, f"{node.attr} in {where}"))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source), [])
    return found


def test_the_walk_finds_a_bypass() -> None:
    source = """
import httpx
from aiohttp import request
from urllib import request as urlrequest
from aiohttp import ClientSession as Session
class BaseGazelleApi:
    def _new_session(self):
        return aiohttp.ClientSession()
    async def _send(self):
        async with self._session_for(True):
            pass
class RedApi(BaseGazelleApi):
    async def logs(self) -> aiohttp.ClientSession:
        await aiohttp.request("GET", "x")
        await request("GET", "x")
        async with Session() as session:
            await self._send("POST", "x")
        return self._http_session()
    async def torrent(self):
        return await self._account().pool.get("x"), self._session
"""
    assert bypasses(source) == [
        (2, "import httpx"),
        (4, "from urllib import request"),
        (14, "aiohttp.request() in RedApi.logs"),
        (15, "aiohttp.request() in RedApi.logs"),
        (16, "ClientSession() in RedApi.logs"),
        (17, "_send in RedApi.logs"),
        (18, "_http_session in RedApi.logs"),
        (20, "pool in RedApi.torrent"),
        (20, "_session in RedApi.torrent"),
    ]


def test_no_tracker_module_reaches_a_session_past_send() -> None:
    files = sorted(TRACKERS.glob("*.py"))
    assert any(path.name == "base.py" for path in files), f"no base.py under {TRACKERS}"
    found = [
        f"src/salmon/trackers/{path.name}:{line}: {what}"
        for path in files
        for line, what in bypasses(path.read_text(encoding="utf-8"))
    ]
    assert found == [], (
        "send tracker requests through BaseGazelleApi._request, which takes the account's rate limit and pool; "
        "do not open or reach an HTTP session of your own:\n" + "\n".join(found)
    )
