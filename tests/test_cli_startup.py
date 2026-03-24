import json
import subprocess
import sys
from pathlib import Path


def test_importing_salmon_run_defers_command_modules() -> None:
    src_dir = Path(__file__).resolve().parents[1] / "src"
    script = f"""
import json
import sys

sys.path.insert(0, {str(src_dir)!r})

import salmon.run

loaded = sorted(
    name
    for name in (
        "salmon.commands",
        "salmon.checks",
        "salmon.converter",
        "salmon.images",
        "salmon.search",
        "salmon.tagger",
        "salmon.uploader",
    )
    if name in sys.modules
)
print(json.dumps(loaded))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == []
