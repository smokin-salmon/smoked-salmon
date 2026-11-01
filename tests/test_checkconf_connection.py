"""What ``salmon checkconf -t`` prints and sends for a tracker, against a local fake (#652).

The expected output is what the command printed before its tracker part moved to ``trackers.connection``. The
tracker is a fake on 127.0.0.1; the network guard in conftest.py stands.
"""

import re
from collections.abc import Callable

import pytest
from helpers.checkconf_connection import FakeTracker, Sent, point_at, run_checkconf

import salmon.trackers
from salmon import cfg
from salmon.trackers import base


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture
def red(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    """RED alone in the config, with the API key given (or none)."""
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])

    def configure(api_key: str | None) -> None:
        assert cfg.tracker.red is not None
        monkeypatch.setattr(cfg.tracker.red, "api_key", api_key)

    return configure


def shown(output: str, fake: FakeTracker) -> str:
    """The output, with what changes from run to run fixed: the config file line, the fake's port, the headers."""
    output = re.sub(r"\AConfig file: .*\n", "", output)
    output = output.replace(fake.url, "http://127.0.0.1:PORT")
    return re.sub(r"(\[DEBUG\] response headers: ).*", r"\1HEADERS", output)


def request_dump(*, by_key: bool, status: int, body: str) -> str:
    return (
        "[DEBUG] GET http://127.0.0.1:PORT/ajax.php\n"
        '[DEBUG] params: {"action":"index"}\n'
        f"[DEBUG] use_api_key: {by_key}\n"
        f"[DEBUG] status: {status}\n"
        "[DEBUG] response headers: HEADERS\n"
        f"[DEBUG] response body: {body}\n"
    )


INDEX_BODY = '{"status": "success", "response": {"authkey": "[REDACTED]", "passkey": "[REDACTED]"}}'
REFUSED_BODY = '{"status": "failure", "error": "bad credentials"}'
HEADER = "\n[ Testing Tracker: RED ]\n\n[ Testing Session Cookie ]\n"
LINE = "--------------------------------------------------\n"
COOKIE_OK = request_dump(by_key=False, status=200, body=INDEX_BODY) + "  ✔ Session cookie OK\n"
COOKIE_REFUSED = (
    request_dump(by_key=False, status=403, body=REFUSED_BODY)
    + 'Request to RED failed (403): "bad credentials"\n'
    + '  ✖ Session cookie check failed: "bad credentials"\n'
)
KEY_OK = request_dump(by_key=True, status=200, body=INDEX_BODY) + "  ✔ API authentication OK\n"
KEY_REFUSED = (
    request_dump(by_key=True, status=403, body=REFUSED_BODY)
    + 'Request to RED failed (403): "bad credentials"\n'
    + '  ✖ API authentication failed: "bad credentials"\n'
)


@pytest.mark.parametrize(
    ("key", "fake", "output", "sent"),
    [
        (
            "an-api-key",
            FakeTracker(),
            HEADER + COOKIE_OK + KEY_OK + "\n✔ Successfully checked RED\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
        (
            None,
            FakeTracker(),
            HEADER + COOKIE_OK + "\n✔ Successfully checked RED\n" + LINE,
            [("index", "cookie")],
        ),
        (
            "an-api-key",
            FakeTracker(key_ok=False),
            HEADER + COOKIE_OK + KEY_REFUSED + "\n✖ Error testing RED (API key)\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
        (
            "an-api-key",
            FakeTracker(cookie_ok=False),
            HEADER + COOKIE_REFUSED + KEY_OK + "\n✖ Error testing RED (session cookie)\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
        (
            "an-api-key",
            FakeTracker(cookie_ok=False, key_ok=False),
            HEADER + COOKIE_REFUSED + KEY_REFUSED + "\n✖ Error testing RED (session cookie, API key)\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
    ],
    ids=["both ok", "no key", "key refused", "cookie refused", "both refused"],
)
def test_checkconf_prints_what_it_printed_before(
    red: Callable[[str | None], None],
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
    fake: FakeTracker,
    output: str,
    sent: list[Sent],
) -> None:
    red(key)
    point_at(monkeypatch, {"RED": fake})
    # checkconf prints the request dumps whatever the config says.
    monkeypatch.setattr(cfg.tracker, "debug_connection", False)

    result = run_checkconf(fake, "-t", "RED")

    assert result.exit_code == 0
    assert shown(result.output, fake) == output
    assert fake.sent == sent


@pytest.mark.parametrize("configured", [False, True])
def test_checkconf_leaves_the_debug_setting_as_the_config_has_it(
    red: Callable[[str | None], None], monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    red("an-api-key")
    fake = FakeTracker()
    point_at(monkeypatch, {"RED": fake})
    monkeypatch.setattr(cfg.tracker, "debug_connection", configured)

    result = run_checkconf(fake, "-t", "RED")

    assert "[DEBUG] GET" in result.output
    assert cfg.tracker.debug_connection is configured
