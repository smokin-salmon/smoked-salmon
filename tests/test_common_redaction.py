"""Masking secrets in rclone command lines and in what rclone prints (#501)."""

import pytest

from salmon.common.redaction import redact_command, redact_secrets, secret_values


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


def test_redact_command_keeps_the_default_config_extra_args() -> None:
    # The extra_args example in config.default.toml has nothing to hide.
    args = ["rclone", "copy", "a", "b:c", "--checksum", "-P", "--sftp-path-override", "@/volume3"]
    assert redact_command(args, secret_values(args)) == "rclone copy a b:c --checksum -P --sftp-path-override @/volume3"


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
