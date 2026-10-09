"""The one filter everything salmon web sends out goes through (ADR 0004, section 3).

Log lines, questions, results, errors: every event is filtered where it is made, before it is kept or sent, with
``redact_tracker_text`` and every secret the config holds, plus each tracker account's authkey and passkey once
salmon knows them. What a tracker sends back may repeat them (an upload page's download links carry the passkey),
and so may an error that quotes it. The idea is chodeus's (ad4f8034), applied here once for everything that leaves.
"""

from collections.abc import Iterable
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


def config_secrets(config: Cfg) -> list[str]:
    """Every secret the config holds: tracker sessions and API keys, image host keys, seedbox and torrent client
    passwords, metadata source credentials, proxy passwords and the web token."""
    found: list[str | None] = []
    for tracker in (config.tracker.red, config.tracker.ops, config.tracker.dic):
        if tracker is not None:
            found += [*session_cookie_forms(tracker.session), tracker.api_key]
    image = config.image
    found += [image.ptscreens_key, image.oeimg_key, image.imgbb_key, image.ra_key]
    for seedbox in config.seedbox:
        found += seedbox_secrets(seedbox)
    metadata = config.metadata
    found += [
        metadata.discogs_token,
        metadata.qobuz.user_auth_token,
        metadata.tidal.client_secret,
        metadata.tidal.token,
        metadata.beatport.password,
        config.upload.ai_review.api_key,
        config.web.token,
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
