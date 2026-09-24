"""``salmon --help`` must not import the heavy libraries that only some commands need (#367)."""

import json
import subprocess
import sys

# Each of these adds from 30 ms (av) to 300 ms (openai) to every salmon start.
HEAVY_MODULES = ("openai", "numpy", "av", "aiohttp.web", "jinja2")

# Runs --help through the real command group, as salmon.run.main() does, but without main()'s
# release notification, which would fetch version.toml from GitHub.
SCRIPT = f"""
import json
import sys

import salmon.run
from salmon.common import commandgroup

try:
    commandgroup(["--help"], prog_name="salmon")
except SystemExit:
    pass

print(json.dumps({{name: name in sys.modules for name in {HEAVY_MODULES!r} + ("salmon.uploader",)}}))
"""


def test_help_does_not_import_heavy_modules() -> None:
    # A fresh interpreter: this test process has usually imported everything already.
    # It inherits the test config through XDG_CONFIG_HOME (see conftest.py).
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "Usage: salmon" in result.stdout
    loaded = json.loads(result.stdout.splitlines()[-1])
    # The command modules themselves are imported: --help lists their commands.
    assert loaded.pop("salmon.uploader") is True
    assert [name for name, is_loaded in loaded.items() if is_loaded] == []
