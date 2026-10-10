"""The one filter everything salmon web sends out goes through (ADR 0004, section 3).

Log lines, questions, results, errors: every event is filtered where it is made, before it is kept or sent, with
``redact_tracker_text`` and every secret the config holds, plus each tracker account's authkey and passkey once
salmon knows them. What a tracker sends back may repeat them (an upload page's download links carry the passkey),
and so may an error that quotes it. The idea is chodeus's (ad4f8034), applied here once for everything that leaves.
"""

import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from salmon.common.redaction import redact_tracker_text
from salmon.config.validations import Cfg
from salmon.trackers.base import learned_secrets, session_cookie_forms
from salmon.uploader.seedbox import seedbox_secrets


def _url_password(url: str | None) -> list[str]:
    if not url:
        return []
    try:
        password = urlparse(url).password
    except ValueError:
        return []
    return [password, unquote(password)] if password else []


@cache
def _template() -> dict[str, Any]:
    """The default config template, parsed once: the placeholders a new user starts with."""
    path = Path(__file__).resolve().parent.parent / "data" / "config.default.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _placeholder(*path: str) -> str | None:
    """What the template sets the setting at `path` to, if it sets it."""
    node: Any = _template()
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, str) else None


def _own(value: str | None, *path: str) -> str | None:
    """`value`, unless it is the template's placeholder for this same setting: that is no secret, and masking it
    would hide the word wherever it appears (`api_key`, `password`). Another setting's placeholder stays one."""
    return None if value is not None and value == _placeholder(*path) else value


def config_secrets(config: Cfg) -> list[str]:
    """Every secret the config holds: tracker sessions and API keys, image host keys, seedbox and torrent client
    passwords, metadata source credentials, proxy passwords and the web token. The template's placeholders for a
    setting are left out."""
    found: list[str | None] = []
    trackers = {"red": config.tracker.red, "ops": config.tracker.ops, "dic": config.tracker.dic}
    for name, tracker in trackers.items():
        if tracker is not None:
            if _own(tracker.session, "tracker", name, "session"):
                found += session_cookie_forms(tracker.session)
            found.append(_own(tracker.api_key, "tracker", name, "api_key"))
    image = config.image
    found += [_own(getattr(image, key), "image", key) for key in ("ptscreens_key", "oeimg_key", "imgbb_key", "ra_key")]
    for seedbox in config.seedbox:
        found += seedbox_secrets(seedbox)
    metadata = config.metadata
    found += [
        _own(metadata.discogs_token, "metadata", "discogs_token"),
        _own(metadata.qobuz.user_auth_token, "metadata", "qobuz", "user_auth_token"),
        _own(metadata.tidal.client_secret, "metadata", "tidal", "client_secret"),
        _own(metadata.tidal.token, "metadata", "tidal", "token"),
        _own(metadata.beatport.password, "metadata", "beatport", "password"),
        _own(config.upload.ai_review.api_key, "upload", "ai_review", "api_key"),
        _own(config.web.token, "web", "token"),
    ]
    services = config.proxy.services
    for url in [config.proxy.url, *(getattr(services, name) for name in services.__struct_fields__)]:
        found += _url_password(url)
    return list(dict.fromkeys(secret for secret in found if secret))


class Redactor:
    """Masks every known secret in what salmon web sends out."""

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        self._secrets = [secret for secret in secrets if secret]

    def text(self, text: str) -> str:
        # The authkeys and passkeys are looked up each time: an account may authenticate at any point.
        return redact_tracker_text(text, [*self._secrets, *learned_secrets()])

    def value(self, value: Any) -> Any:
        """`value` with every string in it masked: a string, or the dicts and lists of an event."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.value(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value
