"""Masking secrets in rclone command lines and in what rclone prints (#501)."""

import pytest

from salmon.common.redaction import redact_command, redact_secrets, redact_tracker_text, redact_url, secret_values


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


PASSKEY = "abcdef0123456789abcdef0123456789"
ENCODED_PASSKEY = "".join(f"%{ord(char):02X}" for char in PASSKEY)


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            "https://tracker.example/torrents.php?action=download&id=12&authkey=test-authkey-0001&torrent_pass=t1",
            "https://tracker.example/torrents.php?action=download&id=12&authkey=[REDACTED]&torrent_pass=[REDACTED]",
        ),
        (
            "https://tracker.example/torrents.php?id=34&torrentid=56",
            "https://tracker.example/torrents.php?id=34&torrentid=56",
        ),
        (f"https://tracker.example/{PASSKEY}/announce", "https://tracker.example/[REDACTED]/announce"),
        ("https://user:test-password@tracker.example:8443/x", "https://tracker.example:8443/x"),
        (
            "https://store.example/album/the-album-deluxe-edition#token=t2",
            "https://store.example/album/the-album-deluxe-edition#[REDACTED]",
        ),
        (f"https://tracker.example/torrents.php?id={PASSKEY}", "https://tracker.example/torrents.php?id=[REDACTED]"),
        ("https://[::1]:8080/a?sig=s3", "https://[::1]:8080/a?sig=[REDACTED]"),
        ("https://tracker.example:bad/x", "[REDACTED]"),
        (
            "https://tracker.example/abcDEF0123_456789abc_def0123456/announce",
            "https://tracker.example/[REDACTED]/announce",
        ),
        (f"https://tracker.example/{ENCODED_PASSKEY}/announce", "https://tracker.example/[REDACTED]/announce"),
        (f"https://tracker.example/x?api_key%3D{PASSKEY}=ignored", "https://tracker.example/x?[REDACTED]=[REDACTED]"),
        (f"https://tracker.example/x?{PASSKEY}", "https://tracker.example/x?[REDACTED]=[REDACTED]"),
        (f"https://{PASSKEY}.tracker.example/x", "https://[REDACTED].tracker.example/x"),
        ("https://tracker.example/test-passkey-0001/announce", "https://tracker.example/[REDACTED]/announce"),
        ("https://tracker.example/announce.php/test-passkey-0001", "https://tracker.example/announce.php/[REDACTED]"),
        ("https://tracker.example/download/test-passkey-0001", "https://tracker.example/download/[REDACTED]"),
        ("https://test-passkey-0001.tracker.example/x", "https://[REDACTED].tracker.example/x"),
        ("https://192.0.2.10/x", "https://192.0.2.10/x"),
        ("https://tracker.example/download/abcdefgh", "https://tracker.example/download/abcdefgh"),
        ("https://tracker.example/x?id=test-pass-0001", "https://tracker.example/x?id=[REDACTED]"),
        ("https://tracker.example/x?test-pass-0001", "https://tracker.example/x?[REDACTED]=[REDACTED]"),
        ("https://[fe80::1%25eth0]/a", "https://[REDACTED]/a"),
        ("https://tracker.example/x?action=dl2x&page=3", "https://tracker.example/x?action=[REDACTED]&page=3"),
        ("https://store.example/release/the-album/654321", "https://store.example/release/the-album/654321"),
    ],
    ids=[
        "query-secrets",
        "ids-kept",
        "passkey-path",
        "userinfo",
        "fragment",
        "long-id",
        "ipv6",
        "unparseable",
        "underscore-path",
        "encoded-path",
        "secret-in-a-name",
        "token-as-a-name",
        "token-host",
        "short-passkey-before-announce",
        "short-passkey-after-announce",
        "letters-and-digits-anywhere",
        "short-token-host",
        "ipv4-host",
        "a-word",
        "short-secret-as-an-id",
        "short-secret-as-a-name",
        "ipv6-zone",
        "action-holding-a-digit",
        "a-slug-and-an-id",
    ],
)
def test_a_url_is_shown_without_its_secrets(url: str, shown: str) -> None:
    assert redact_url(url) == shown


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
