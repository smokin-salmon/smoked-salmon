"""Give the test run its own configuration.

``salmon`` loads and validates its config at import time, and exits when none
is found. Point it at a copy of the default config, with its directories moved
into a temporary location, before any test module imports the package. This
also keeps a developer's real config out of the test run.
"""

import os
import tempfile
from pathlib import Path

_DEFAULT_CONFIG = Path(__file__).parent.parent / "src" / "salmon" / "data" / "config.default.toml"

_root = Path(tempfile.mkdtemp(prefix="salmon-tests-"))
_music = _root / "music"
_torrents = _root / "torrents"
_music.mkdir()
_torrents.mkdir()

_config = _DEFAULT_CONFIG.read_text(encoding="utf-8")
for _old, _new in (
    ("download_directory = '.music'", f"download_directory = '{_music.as_posix()}'"),
    ("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{_torrents.as_posix()}'"),
):
    assert _old in _config, f"default config no longer contains {_old!r}; update tests/conftest.py"
    _config = _config.replace(_old, _new)

_config_dir = _root / "config" / "smoked-salmon"
_config_dir.mkdir(parents=True)
(_config_dir / "config.toml").write_text(_config, encoding="utf-8")

os.environ["XDG_CONFIG_HOME"] = str(_root / "config")
