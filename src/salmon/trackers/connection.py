"""The connection check of one tracker: what ``salmon checkconf -t`` and salmon web's connection check both run.

It sends at most two requests, the index call with the session cookie and then, when an API key is set and the
TLS certificate did not fail, the index call with the API key, through ``BaseGazelleApi._request`` like any other
request. The command and the job print from the same functions here; the command adds its CA certificate hint.

The shape of the check is from the fork's ``checks/connection.py`` (chodeus, 0b29d2d5, 9bfdddc3 and 75e5a3d9).
"""

from collections.abc import Callable
from typing import Literal

import asyncclick as click
import msgspec

from salmon.errors import TLSCertificateError
from salmon.trackers.base import BaseGazelleApi

SESSION_COOKIE = "session cookie"
API_KEY = "API key"
TLS_CERTIFICATE = "TLS certificate"


class ConnectionResult(msgspec.Struct, kw_only=True):
    """What the check found for one tracker.

    Attributes:
        tracker: The site code.
        session_cookie: ``ok``, ``failed`` (with ``session_cookie_error``), or ``not_checked`` when the TLS certificate
            failed first.
        api_key: ``ok``, ``failed`` (with ``api_key_error``), ``not_set``, or ``not_checked`` when the TLS
            certificate failed first.
        tls_error: Why the certificate of the site does not verify, if it does not.
    """

    tracker: str
    session_cookie: Literal["ok", "failed", "not_checked"]
    session_cookie_error: str | None = None
    api_key: Literal["ok", "failed", "not_set", "not_checked"]
    api_key_error: str | None = None
    tls_error: str | None = None

    @property
    def failed_checks(self) -> list[str]:
        """What failed, in the order it was checked."""
        failed: list[str] = []
        if self.session_cookie == "failed":
            failed.append(SESSION_COOKIE)
        if self.tls_error is not None:
            failed.append(TLS_CERTIFICATE)
        if self.api_key == "failed":
            failed.append(API_KEY)
        return failed

    @property
    def ok(self) -> bool:
        return not self.failed_checks


async def check_connection(
    code: str, api: BaseGazelleApi, on_step: Callable[[str, str | None], None] | None = None
) -> ConnectionResult:
    """Check the session cookie and the API key of one tracker.

    Never raises for a failed check: the failure is in the result.

    Args:
        code: The tracker's site code.
        api: The tracker's client.
        on_step: Called with ``SESSION_COOKIE`` or ``API_KEY`` and its error (None: it passed) as each check ends,
            before the next request goes out. Not called for a check that is not made (a certificate that failed
            first, or no API key).
    """
    index = f"{api.base_url}/ajax.php"
    session_cookie: Literal["ok", "failed", "not_checked"] = "ok"
    session_cookie_error: str | None = None
    tls_error: str | None = None
    # The session cookie check is independent of API key authentication.
    try:
        await api._request("GET", index, params={"action": "index"}, prefer_api_key=False)
    except TLSCertificateError as err:
        session_cookie, tls_error = "not_checked", str(err)
    except Exception as err:
        session_cookie, session_cookie_error = "failed", str(err)
    if tls_error is None and on_step is not None:
        on_step(SESSION_COOKIE, session_cookie_error)

    api_key: Literal["ok", "failed", "not_set", "not_checked"] = "ok"
    api_key_error: str | None = None
    if not api.api_key:
        api_key = "not_set"
    elif tls_error is not None:
        # The API key check goes to the same host, and would fail the same way.
        api_key = "not_checked"
    else:
        try:
            await api._request("GET", index, params={"action": "index"}, prefer_api_key=True)
        except Exception as err:
            api_key, api_key_error = "failed", str(err)
        if on_step is not None:
            on_step(API_KEY, api_key_error)
    return ConnectionResult(
        tracker=code,
        session_cookie=session_cookie,
        session_cookie_error=session_cookie_error,
        api_key=api_key,
        api_key_error=api_key_error,
        tls_error=tls_error,
    )


def print_tracker_header(code: str) -> None:
    click.secho(f"\n[ Testing Tracker: {code} ]", fg="cyan", bold=True)


def print_session_cookie_header() -> None:
    click.secho("\n[ Testing Session Cookie ]", fg="cyan", bold=True)


def print_step(step: str, error: str | None) -> None:
    """The line for one check that ended (see ``check_connection``'s ``on_step``)."""
    if step == SESSION_COOKIE:
        if error is None:
            click.secho("  ✔ Session cookie OK", fg="green")
        else:
            click.secho(f"  ✖ Session cookie check failed: {error}", fg="red", bold=True)
    elif error is None:
        click.secho("  ✔ API authentication OK", fg="green")
    else:
        click.secho(f"  ✖ API authentication failed: {error}", fg="red", bold=True)


def print_certificate_failure(result: ConnectionResult) -> None:
    """Why nothing was checked, for a result with a ``tls_error``."""
    click.secho(f"  ✖ {result.tls_error}", fg="red", bold=True)
    click.secho(
        "    This fails before anything is sent: your session cookie and API key are not the cause,"
        " and were not checked.",
        fg="red",
    )


def print_verdict(result: ConnectionResult) -> None:
    failed = result.failed_checks
    if failed:
        click.secho(f"\n✖ Error testing {result.tracker} ({', '.join(failed)})", fg="red", bold=True)
    else:
        click.secho(f"\n✔ Successfully checked {result.tracker}", fg="green", bold=True)
