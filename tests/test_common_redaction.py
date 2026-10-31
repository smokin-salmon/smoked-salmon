"""Masking secrets in rclone command lines and in what rclone prints (#501)."""

import pytest

from salmon.common.redaction import (
    redact_command,
    redact_secrets,
    redact_tracker_headers,
    redact_tracker_text,
    secret_values,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("key_pem=hunter2", "key_pem=[REDACTED]"),
        ("--session hunter2", "--session [REDACTED]"),
        ("--sftp-key-pem=hunter2", "--sftp-key-pem=[REDACTED]"),
        ("token=hunter2", "token=[REDACTED]"),
        ("sftp://dean:hunter2@box/x", "sftp://[REDACTED]@box/x"),
        ("copy /music sbox:storage/red --transfers 4", "copy /music sbox:storage/red --transfers 4"),
        # rclone connection strings may quote a value that has spaces in it, such as a PEM key.
        (":sftp,key_pem='-----BEGIN KEY----- hunter2 -----END KEY-----',user=x:", ":sftp,key_pem=[REDACTED],user=x:"),
        ('--sftp-key-pem "-----BEGIN KEY----- hunter2"', "--sftp-key-pem [REDACTED]"),
        # rclone doubles a quote inside a quoted value.
        (":sftp,pass='hunter''2',user=x:", ":sftp,pass=[REDACTED],user=x:"),
    ],
)
def test_redact_masks_suffixed_option_names_and_sessions(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


def test_redact_command_masks_a_flag_value() -> None:
    shown = redact_command(["rclone", "copy", "a", "b", "--sftp-pass", "UNIQUEPASS"])
    assert "UNIQUEPASS" not in shown
    assert shown.endswith("--sftp-pass '[REDACTED]'")


def test_redact_command_masks_an_equals_form_secret_whole() -> None:
    shown = redact_command(["rclone", "copy", "a", "b", "--sftp-key-pem=BEGIN KEY PRIVATEPART DATA"])
    assert "PRIVATEPART" not in shown
    assert "--sftp-key-pem=[REDACTED]" in shown


def test_redact_command_masks_a_quoted_value_with_spaces_whole() -> None:
    shown = redact_command(["rclone", "copy", "a", "b", "--ftp-pass", "it's hunter2 really"])
    assert "hunter2" not in shown
    assert "really" not in shown


def test_redact_command_shows_only_allowlisted_flag_values() -> None:
    shown = redact_command(
        ["rclone", "copy", "a", "b", "--transfers", "4", "--http-headers", '"Authorization","hunter2"', "--bwlimit=8M"]
    )
    assert "--transfers 4" in shown
    assert "--bwlimit=8M" in shown
    assert "hunter2" not in shown
    assert "Authorization" not in shown


def test_redact_command_masks_the_path_override_but_keeps_the_rest_of_the_default_config_extra_args() -> None:
    # The path override is often a home folder naming the seedbox account.
    args = ["rclone", "copy", "a", "b:c", "--checksum", "-P", "--sftp-path-override", "@/volume3"]
    assert (
        redact_command(args, secret_values(args)) == "rclone copy a b:c --checksum -P --sftp-path-override '[REDACTED]'"
    )


def test_a_url_password_in_an_argument_is_masked() -> None:
    args = ["rclone", "copy", "a", "b", "--webdav-url", "https://dean:UNIQUEURLPASS@dav.example/remote.php"]
    assert "UNIQUEURLPASS" not in redact_command(args)
    assert "UNIQUEURLPASS" not in redact_secrets(" ".join(args))


def test_a_short_known_secret_is_masked_as_a_whole_word() -> None:
    assert redact_secrets("authentication failed for ab", known=["ab"]) == "authentication failed for [REDACTED]"
    assert redact_secrets("about tabs", known=["ab"]) == "about tabs"


def test_a_pem_value_starting_with_dashes_is_masked_and_collected() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY----- UNIQUEKEYMATERIAL -----END OPENSSH PRIVATE KEY-----"
    args = ["rclone", "copy", "a", "b", "--sftp-key-pem", pem]
    assert "UNIQUEKEYMATERIAL" not in redact_command(args)
    assert pem in secret_values(args)


def test_a_dumped_auth_header_is_masked() -> None:
    dumped = "2026/09/26 DEBUG : HTTP REQUEST\nAuthorization: Bearer UNIQUETOKEN\nUser-Agent: rclone/v1.72.0"
    shown = redact_secrets(dumped)
    assert "UNIQUETOKEN" not in shown
    assert "User-Agent: rclone/v1.72.0" in shown


def test_a_secret_value_that_looks_like_a_flag_is_still_masked() -> None:
    args = ["rclone", "copy", "a", "b", "--sftp-pass", "-UNIQUEMATERIAL", "--progress", "--transfers", "4"]
    shown = redact_command(args)
    assert "UNIQUEMATERIAL" not in shown
    # A known switch takes no value, so what follows it is shown as usual.
    assert "--progress --transfers 4" in shown
    assert "-UNIQUEMATERIAL" in secret_values(args)


@pytest.mark.parametrize(
    ("args", "remote", "secret"),
    [
        (["--sftp-pass", "UNIQUEPASS"], "sbox", "UNIQUEPASS"),
        (["--sftp-pass=UNIQUEPASS"], "sbox", "UNIQUEPASS"),
        (["--http-headers", "Authorization,UNIQUETOKEN"], "web", "UNIQUETOKEN"),
        ([], ":sftp,host=box,pass=UNIQUEPASS:", "UNIQUEPASS"),
        ([], ":http,headers='Authorization,UNIQUETOKEN':", "UNIQUETOKEN"),
        ([], ":http,url='https://user:UNIQUEMATERIAL@example.com':", "UNIQUEMATERIAL"),
        (["--http-url", "https://user:UNIQUEARGPASS@example.com"], "web", "UNIQUEARGPASS"),
    ],
    ids=[
        "flag value",
        "equals form",
        "comma list",
        "connection-string field",
        "connection-string header list",
        "connection-string url",
        "url argument",
    ],
)
def test_embedded_credentials_are_collected_for_echoed_errors(args, remote, secret) -> None:
    assert secret in secret_values(args, remote)
    assert secret not in redact_secrets(f"401 for {secret}", secret_values(args, remote))


def test_a_masked_known_value_keeps_what_follows_it() -> None:
    # The remote's path after a connection string's password is what a support thread needs.
    echo = 'Failed to create file system for ":sftp,host=box,pass=hunter2:/music/Album": 401'
    assert redact_secrets(echo, known=["hunter2"]) == (
        'Failed to create file system for ":sftp,host=box,pass=[REDACTED]:/music/Album": 401'
    )
    assert redact_command(["rclone", "copy", "a", ":sftp,pass=hunter2:/music/Album"], known=["hunter2"]) == (
        "rclone copy a ':sftp,pass=[REDACTED]:/music/Album'"
    )


def test_a_value_only_partly_known_is_still_masked_whole() -> None:
    assert redact_secrets("pass=hunter2xyz rest", known=["hunter2"]) == "pass=[REDACTED] rest"


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("{'authkey': 'FAKEKEYSIX", "{'authkey': '[REDACTED]"),
        ('{"authkey": "FAKEKEY\nnext line', '{"authkey": "[REDACTED]\nnext line'),
        ("session: 'open\nthe next line's words stay", "session: '[REDACTED]\nthe next line's words stay"),
        ("https://tracker.example/FAKE01/announce", "https://tracker.example/[REDACTED]/announce"),
        ("https://tracker.example/FAKEPASSKEY0123456789/announce", "https://tracker.example/[REDACTED]/announce"),
    ],
)
def test_redact_tracker_text_masks_a_cut_off_value_and_an_announce_passkey_of_any_length(text, shown) -> None:
    assert redact_tracker_text(text) == shown


def test_redact_tracker_text_masks_an_input_a_cut_off_value_runs_into() -> None:
    shown = redact_tracker_text("{'authkey': 'FAKEKEYEIGHT <input value='FAKEKEYNINE' name='auth'>")

    assert "FAKEKEY" not in shown


def test_a_flag_value_a_known_secret_masked_keeps_what_follows_it() -> None:
    assert redact_secrets("--sftp-pass=hunter2:/music/Album failed", known=["hunter2"]) == (
        "--sftp-pass=[REDACTED]:/music/Album failed"
    )


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("{'authkey': 'FAKEKEYONE', 'id': 5}", "{'authkey': '[REDACTED]', 'id': 5}"),
        ("{'passkey': \"FAKE'KEYTWO\"}", "{'passkey': \"[REDACTED]\"}"),
        ('var authkey = "FAKEKEYTHREE";', 'var authkey = "[REDACTED]";'),
        ("{auth: 'FAKEKEYFOUR'}", "{auth: '[REDACTED]'}"),
        ('{"authkey": "FAKE\\"KEYFIVE"}', '{"authkey": "[REDACTED]"}'),
        ("{'authkey': 'FAKEKEYSIX", "{'authkey': '[REDACTED]"),
        # With no space before the quote, the query pattern must leave it to the assignment pattern.
        ('authkey="FAKEKEYSEVEN"', 'authkey="[REDACTED]"'),
        ("passkey='FAKEKEYEIGHT'", "passkey='[REDACTED]'"),
    ],
)
def test_redact_tracker_text_masks_a_quoted_value_in_json_a_repr_or_a_script(text, shown) -> None:
    assert redact_tracker_text(text) == shown


@pytest.mark.parametrize(
    "text", ["author = 'A Name'", '"authors": "A Name, B Name"', "https://tracker.example/torrents/download"]
)
def test_redact_tracker_text_leaves_a_longer_name_or_another_path_alone(text) -> None:
    assert redact_tracker_text(text) == text


def test_response_headers_mask_cookie_values_credentials_and_known_secrets() -> None:
    headers = [
        ("Set-Cookie", "session=new-session-0123; path=/; HttpOnly"),
        ("Set-Cookie", "keeplogged=keep-0123; path=/"),
        ("Set-Cookie", "cf_clearance=clear-0123; path=/"),
        ("X-Api-Token", "token-0123"),
        ("Location", "/torrents.php?id=1&authkey=key-0123"),
        ("Server", "nginx known-secret-0123"),
    ]

    shown = redact_tracker_headers(headers, ["known-secret-0123"])

    # A newly issued cookie is no known secret, yet its value is masked; the name and attributes stay.
    assert shown["Set-Cookie"].startswith("session=[REDACTED]")
    assert ", keeplogged=[REDACTED]" in shown["Set-Cookie"]
    assert "path=/" in shown["Set-Cookie"]
    assert "new-session-0123" not in shown["Set-Cookie"] and "keep-0123" not in shown["Set-Cookie"]
    # A cookie that is no known secret: only the header rule masks it.
    assert ", cf_clearance=[REDACTED]; path=/" in shown["Set-Cookie"]
    assert shown["X-Api-Token"] == "[REDACTED]"
    assert shown["Location"] == "/torrents.php?id=1&authkey=[REDACTED]"
    assert shown["Server"] == "nginx [REDACTED]"


def test_a_response_header_naming_a_secret_is_masked() -> None:
    assert redact_tracker_headers([("X-Client-Secret", "newly-issued-0123")]) == {"X-Client-Secret": "[REDACTED]"}


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        pytest.param("<input name=auth value=MARK-UNQUOTED-01>", "MARK-UNQUOTED-01", id="input unquoted"),
        pytest.param('<input name = "authkey" value = "MARK-SPACED-02">', "MARK-SPACED-02", id="input spaced"),
        pytest.param("ajax.php?apikey=MARK-APIKEY-03&action=index", "MARK-APIKEY-03", id="apikey query"),
        pytest.param("x.php?authorization=MARK-AUTHZ-04", "MARK-AUTHZ-04", id="authorization query"),
        pytest.param("https://user:MARK-USERINFO-05@tracker.test/x", "MARK-USERINFO-05", id="url userinfo"),
        pytest.param('<input name=auth value="MARK-GT-07>TAIL">', "MARK-GT-07", id="input value holding >"),
        pytest.param("x.php?authorization=MARK-SEMI-08;MARK-SEMI-09&id=1", "MARK-SEMI-09", id="query value holding ;"),
        pytest.param("keeplogged=MARK-KEEP-10; path=/", "MARK-KEEP-10", id="keeplogged cookie"),
        pytest.param('{"keeplogged": "MARK-KEEP-11"}', "MARK-KEEP-11", id="keeplogged in json"),
    ],
)
def test_redact_tracker_text_masks_every_form_a_tracker_secret_takes(text: str, marker: str) -> None:
    assert marker not in redact_tracker_text(text)


def test_redact_tracker_text_keeps_a_cookies_delimiter_and_leaves_a_masked_value_alone() -> None:
    assert redact_tracker_text("session=MARK-COOKIE-06; path=/") == "session=[REDACTED]; path=/"
    assert redact_tracker_text("session=[REDACTED]; path=/") == "session=[REDACTED]; path=/"


def test_a_url_whose_query_holds_an_at_sign_has_no_userinfo_to_mask() -> None:
    url = "https://tracker.test?email=alice@example.test#x@y"
    assert redact_tracker_text(url) == url
