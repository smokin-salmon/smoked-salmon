"""Stop a release whose bumped version.toml would not tell clients the right thing (#1068).

Run by .github/workflows/docker-image.yml after "Update version files" and before the image is built, with the
release's tag in $VERSION. Every installed salmon decodes version.toml from master with
`salmon.release_notification.VersionData`, so this decodes the bumped file the same way, then checks that `current`
and the newest [[changelog]] entry are the tag. An optional argument names another file to check (for trying it).

salmon loads and validates a config when it is imported, so a throwaway one is built from the shipped template.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = ROOT / "src" / "salmon" / "data" / "version.toml"
TRACKERS = "".join(f"\n[tracker.{code}]\nsession = 'release-check'\n" for code in ("red", "ops", "dic"))
FOLDER_KEYS = ("download_directory", "dottorrents_dir")


def throwaway_config(tmp: Path) -> str | None:
    """Write a loadable config under tmp; return what is wrong with the template, or None."""
    text = (ROOT / "src" / "salmon" / "data" / "config.default.toml").read_text(encoding="utf-8")
    for key in FOLDER_KEYS:
        folder = tmp / key
        folder.mkdir()
        text, count = re.subn(rf"^{key} = '[^']*'", f"{key} = '{folder.as_posix()}'", text, flags=re.MULTILINE)
        if count != 1:
            return f"config.default.toml no longer has a single `{key} = '...'` line; update {__file__}"
    config_dir = tmp / "smoked-salmon"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(text + TRACKERS, encoding="utf-8")
    return None


def main() -> int:
    version = os.environ.get("VERSION", "")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else VERSION_FILE
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        print(f"The release tag must be X.Y.Z, got {version!r}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        if problem := throwaway_config(Path(tmp)):
            print(problem, file=sys.stderr)
            return 1
        os.environ["XDG_CONFIG_HOME"] = tmp
        os.environ.pop("SALMON_CONFIG_DIR", None)

        import msgspec

        from salmon.release_notification import VersionData

        try:
            data = msgspec.toml.decode(path.read_bytes(), type=VersionData)
        except msgspec.DecodeError as e:
            print(f"{path} does not decode as VersionData, so clients could not read it: {e}", file=sys.stderr)
            return 1

    problems = []
    if data.current != version:
        problems.append(f"current is {data.current!r}")
    newest = data.changelog[0].version if data.changelog else None
    if newest != version:
        problems.append(f"the first [[changelog]] entry is {newest!r}")
    if problems:
        print(f"{path} should be at {version}, but " + " and ".join(problems), file=sys.stderr)
        return 1
    print(f"{path} decodes, and current and the newest changelog entry are {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
