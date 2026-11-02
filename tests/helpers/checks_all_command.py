"""Helpers of tests/test_checks_all_command.py that other test modules use too."""

from pathlib import Path

# A stand-in for flac's test mode: a file that is a FLAC stream decodes, anything else fails as flac fails.
FAKE_FLAC = """\
import sys
path = sys.argv[-1]
with open(path, "rb") as handle:
    good = handle.read(4) == b"fLaC"
sys.stderr.write(f"{path}: ok\\n" if good else f"{path}: ERROR while decoding data\\n")
sys.exit(0 if good else 1)
"""


def _snapshot(path: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns) for p in path.rglob("*") if p.is_file()
    ) + [("", 0, path.stat().st_mtime_ns)]
