"""The shipped config.default.toml, made loadable for the tests: its folders and tracker cookies filled in."""

from pathlib import Path

DEFAULT_CONFIG = Path(__file__).parent.parent / "src" / "salmon" / "data" / "config.default.toml"

_PLACEHOLDER = "session = 'get-from-site-cookie'"
_TRACKERS = [(code, f"[tracker.{code}]") for code in ("red", "ops", "dic")]


def shipped_config(music: Path, torrents: Path) -> str:
    """The template's text with these folders, and a test cookie for each tracker.

    The template's placeholder cookie would leave its tracker out (salmon.config.compat), and the tests need all
    three.
    """
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    for old, new in (
        ("download_directory = '.music'", f"download_directory = '{music.as_posix()}'"),
        ("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{torrents.as_posix()}'"),
        *((f"{header}\n{_PLACEHOLDER}", f"{header}\nsession = 'test-session-{code}'") for code, header in _TRACKERS),
    ):
        assert old in text, f"default config no longer contains {old!r}; update tests/shipped_config.py"
        text = text.replace(old, new)
    return text
