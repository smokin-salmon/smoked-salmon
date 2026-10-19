"""The shipped config, made loadable for the tests: its folders and tracker cookies filled in."""

from pathlib import Path

DATA = Path(__file__).parent.parent / "src" / "salmon" / "data"
DEFAULT_CONFIG = DATA / "config.default.toml"
REFERENCE_CONFIG = DATA / "config.reference.toml"

TRACKER_CODES = ("red", "ops", "dic")


def fake_trackers() -> str:
    """One table per tracker, with a fake cookie: the short template has none, and the tests need all three."""
    return "".join(f"\n[tracker.{code}]\nsession = 'test-session-{code}'\n" for code in TRACKER_CODES)


def shipped_config(music: Path, torrents: Path) -> str:
    """The short template's text with these folders, and a test cookie for each tracker appended."""
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    for old, new in (
        ("download_directory = '.music'", f"download_directory = '{music.as_posix()}'"),
        ("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{torrents.as_posix()}'"),
    ):
        assert old in text, f"default config no longer contains {old!r}; update tests/shipped_config.py"
        text = text.replace(old, new)
    return text + fake_trackers()
