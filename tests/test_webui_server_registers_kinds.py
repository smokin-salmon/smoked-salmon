"""`salmon web` registers its job kinds through server.py's import of salmon.webui.kinds.

server.py is the only module in src/salmon that imports kinds, which registers the job kinds as it loads, and only
its use of two of kinds' constants keeps ruff's F401 off that import. Were they to move, `ruff check --fix` would
delete the import and `salmon web` would start with no job kinds, while the web tests stayed green: they import
kinds themselves. So this checks the import is still there.
"""

import ast
from pathlib import Path

SERVER = Path(__file__).parent.parent / "src" / "salmon" / "webui" / "server.py"


def _imports_kinds(node: ast.stmt) -> bool:
    if isinstance(node, ast.ImportFrom):
        return node.module == "salmon.webui" and any(alias.name == "kinds" for alias in node.names)
    if isinstance(node, ast.Import):
        return any(alias.name == "salmon.webui.kinds" for alias in node.names)
    return False


def test_the_server_imports_kinds_so_the_job_kinds_are_registered():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    assert any(_imports_kinds(node) for node in tree.body), (
        "src/salmon/webui/server.py no longer imports salmon.webui.kinds at module level, so `salmon web` would "
        "start with no job kinds: import it there again (or register the kinds where the server starts, and "
        "update this test)"
    )
