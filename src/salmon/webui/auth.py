"""The ``salmon web`` token and the login sessions it opens (ADR 0004, section 3).

Ported from chodeus's fork (995928d1, ab23ae24): the token from ``SALMON_WEB_TOKEN`` or the config, compared in
constant time as bytes, and traded once for an ``HttpOnly`` cookie. Unlike the fork, a token is always required, and
the cookie holds a random session id rather than the token itself, so logging out ends the session.
"""

import hashlib
import hmac
import os
import secrets
import time

import asyncclick as click

from salmon.config.validations import WEB_TOKEN_MIN_LENGTH
from salmon.webui.security import is_loopback

ENV_VAR = "SALMON_WEB_TOKEN"
COOKIE_NAME = "salmon_web_session"
SESSION_MAX_AGE = 30 * 24 * 3600
# Each login adds one; the oldest goes first beyond this.
MAX_SESSIONS = 100


def resolve_token(host: str, configured: str | None) -> tuple[str, bool]:
    """The token for a server bound to ``host``, and whether it was made for this start.

    ``SALMON_WEB_TOKEN`` beats the config's ``[web] token``. With neither, a loopback bind gets a new random token;
    any other bind refuses to start.

    Raises:
        click.ClickException: No token for a non-loopback bind, or ``SALMON_WEB_TOKEN`` is too short.
    """
    env = os.environ.get(ENV_VAR)
    if env:
        if len(env) < WEB_TOKEN_MIN_LENGTH:
            raise click.ClickException(f"{ENV_VAR} must be at least {WEB_TOKEN_MIN_LENGTH} characters.")
        return env, False
    if configured:
        return configured, False
    if is_loopback(host):
        return secrets.token_urlsafe(32), True
    raise click.ClickException(
        f"salmon web will not listen on {host} without a token: anyone who reaches it could upload with your "
        f"tracker accounts. Set {ENV_VAR} or [web] token (at least {WEB_TOKEN_MIN_LENGTH} characters), "
        "or listen on 127.0.0.1."
    )


def _encode(value: str) -> bytes:
    # Header values can hold surrogates (undecodable bytes); compare_digest raises on non-ASCII str.
    return value.encode("utf-8", "surrogatepass")


def _session_key(session_id: str) -> bytes:
    # Sessions are looked up by a digest of the id, so the lookup's timing says nothing about the ids.
    return hashlib.sha256(_encode(session_id)).digest()


class Auth:
    """The token, and the sessions opened with it. Sessions live in memory: a restart logs every browser out."""

    def __init__(self, token: str) -> None:
        self._token = _encode(token)
        self._sessions: dict[bytes, float] = {}

    def token_matches(self, supplied: str | None) -> bool:
        """Constant-time comparison with the token."""
        if not supplied:
            return False
        return hmac.compare_digest(_encode(supplied), self._token)

    def open_session(self) -> str:
        """A new session id for the login cookie."""
        self._prune()
        while len(self._sessions) >= MAX_SESSIONS:
            del self._sessions[next(iter(self._sessions))]
        session_id = secrets.token_urlsafe(32)
        self._sessions[_session_key(session_id)] = time.monotonic() + SESSION_MAX_AGE
        return session_id

    def session_valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        expires = self._sessions.get(_session_key(session_id))
        return expires is not None and expires > time.monotonic()

    def close_session(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(_session_key(session_id), None)

    def authenticated(self, authorization: str | None, session_id: str | None) -> bool:
        """Whether a request carries the token as ``Authorization: Bearer`` or a valid session cookie."""
        if authorization:
            scheme, _, credentials = authorization.partition(" ")
            if scheme.lower() == "bearer" and self.token_matches(credentials.strip()):
                return True
        return self.session_valid(session_id)

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [key for key, expires in self._sessions.items() if expires <= now]:
            del self._sessions[key]
