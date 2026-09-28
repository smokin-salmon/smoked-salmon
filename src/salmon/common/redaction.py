import re
import shlex
from collections.abc import Iterable

_URL_USERINFO = re.compile(r"(://)[^/\s@]+@")
_USERINFO_PASSWORD = re.compile(r"://[^/\s@:]*:([^/\s@']+)@")
_SECRET_WORDS = r"(?:pass|password|token|secret|key|session|header|cookie|auth)"
# A quoted value is masked whole, doubled quotes included: rclone quotes values with spaces (a PEM key).
_SECRET_VALUE = r"""(?:'(?:[^']|'')*'|"(?:[^"]|"")*"|\S+)"""
# A value a known secret already masked whole is left alone, so what follows it (a remote's path) stays.
_MASKED_VALUE = r"""\[REDACTED\](?=[\s,:'"]|$)"""
_SECRET_FLAG = re.compile(
    rf"(--?[\w-]*{_SECRET_WORDS}[\w-]*(?:=|[ \t]+))(?!{_MASKED_VALUE}){_SECRET_VALUE}", re.IGNORECASE
)
_SECRET_ASSIGNMENT = re.compile(rf"\b([\w-]*{_SECRET_WORDS}[\w-]*)=(?!{_MASKED_VALUE}){_SECRET_VALUE}", re.IGNORECASE)
# Flags whose values are harmless to show; every other flag's value in a command is masked, since no
# list of secret flag names ever covers them all (rclone takes credentials in headers, keys, tokens ...).
_SHOWN_FLAGS = frozenset(
    {
        "--transfers",
        "--checkers",
        "--bwlimit",
        "--config",
        "--sftp-path-override",
        "--timeout",
        "--contimeout",
        "--retries",
        "--low-level-retries",
        "--log-level",
        "--buffer-size",
        "--multi-thread-streams",
    }
)
# Switches that take no value. Any other flag takes the next argument as its value, whatever that looks
# like ("-abc" can be a password), so an unlisted switch at worst hides the argument after it.
_SWITCHES = frozenset(
    {
        "-v",
        "-vv",
        "-q",
        "-P",
        "--progress",
        "--quiet",
        "--dry-run",
        "--checksum",
        "--size-only",
        "--update",
        "--inplace",
        "--ignore-existing",
        "--no-traverse",
        "--no-check-certificate",
        "--stats-one-line",
    }
)
# A flag is -x or --name; a value may itself start with dashes (a PEM key's -----BEGIN).
_FLAG = re.compile(r"--?[A-Za-z]")
# A header line such as rclone's --dump auth prints ("Authorization: Bearer ..."): the value is masked.
_SECRET_HEADER = re.compile(rf"(?im)^([^\S\n]*[\w-]*{_SECRET_WORDS}[\w-]*[^\S\n]*:)[^\n]*")
# A secret field of an rclone connection string (":sftp,pass=x,user=y:"); a bare value ends at , or :.
_CONNECTION_SECRET = re.compile(
    rf"""(?:^|[,:])[\w-]*{_SECRET_WORDS}[\w-]*=('(?:[^']|'')*'|"(?:[^"]|"")*"|[^,:\s]+)""", re.IGNORECASE
)


def _mask_known(text: str, known: Iterable[str | None]) -> str:
    """Mask each known secret value, longest first."""
    for secret in sorted((s for s in known if s), key=len, reverse=True):
        # A short one only as a whole word, so "ab" doesn't eat "about".
        pattern = re.escape(secret) if len(secret) >= 3 else rf"(?<!\w){re.escape(secret)}(?!\w)"
        text = re.sub(pattern, "[REDACTED]", text)
    return text


def redact_secrets(text: str, known: Iterable[str | None] = ()) -> str:
    """Mask known secret values, URL userinfo and password/token flags before text reaches a log."""
    # Known values first: an error can repeat one in a shape no pattern expects.
    text = _mask_known(text, known)
    text = _SECRET_HEADER.sub(r"\1 [REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _SECRET_FLAG.sub(r"\1[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", text)


def _hidden_flag(arg: str) -> bool:
    """A flag whose value isn't on the shown list."""
    return _FLAG.match(arg) is not None and arg not in _SHOWN_FLAGS


def _takes_hidden_value(arg: str) -> bool:
    """A hidden flag given as "--flag value", whose next argument is its value."""
    return _hidden_flag(arg) and "=" not in arg and arg not in _SWITCHES


def secret_values(args: Iterable[str], connection: str = "") -> list[str]:
    """Values given to a command that aren't known harmless, plus connection-string secrets, to mask wherever echoed."""
    args = list(args)
    found = [value for flag, value in zip(args, args[1:], strict=False) if _takes_hidden_value(flag)]
    for arg in args:
        name, equals, value = arg.partition("=")
        if equals and _hidden_flag(name):
            found.append(value)
    for match in _CONNECTION_SECRET.finditer(connection):
        value = match.group(1)
        if value[:1] in "'\"" and len(value) > 1:
            value = value[1:-1].replace(value[0] * 2, value[0])
        found.append(value)
    # A URL anywhere (an argument, a connection-string url= field) can carry user:password@.
    found += [match.group(1) for text in [*args, connection] for match in _USERINFO_PASSWORD.finditer(text)]
    # rclone takes some values as comma-separated lists (--http-headers Name,Value), echoed one part at a time.
    found += [part.strip(" '\"") for value in found if "," in value for part in value.split(",")]
    return found


def redact_command(args: list[str], known: Iterable[str | None] = ()) -> str:
    """A command line for the log: only allowlisted flags keep their values, replaced before joining."""
    known = list(known)
    shown: list[str] = []
    hide_next = False
    for arg in args:
        name, equals, _ = arg.partition("=")
        if hide_next:
            shown.append("[REDACTED]")
            hide_next = False
            continue
        if equals and _hidden_flag(name):
            shown.append(f"{name}=[REDACTED]")
        else:
            shown.append(redact_secrets(arg, known))
        hide_next = _takes_hidden_value(arg)
    return shlex.join(shown)


# What a Gazelle tracker sends back carries its own secrets: the authkey (auth, authkey), the passkey
# (passkey, torrent_pass), the session cookie and an api key.
_TRACKER_KEYS = r"(?:auth|authkey|passkey|torrent_pass|session|api_?key|authorization)"
# "authkey": "..." in JSON (or a Python repr of it), authkey = "..." in a page's script.
_TRACKER_ASSIGNMENT = re.compile(rf"""(["']?\b{_TRACKER_KEYS}["']?\s*[:=]\s*)(["'])(?:(?!\2).)*\2""", re.IGNORECASE)
# ?authkey=...&torrent_pass=... in a link or a redirect, &amp;-escaped in a page.
_TRACKER_QUERY = re.compile(rf"""([?&;]{_TRACKER_KEYS}=)[^&#\s"'<>]*""", re.IGNORECASE)
# The passkey in an announce URL: https://tracker/<passkey>/announce.
_ANNOUNCE_PASSKEY = re.compile(r"/[A-Za-z0-9]{16,}(?=/announce)")
# A form field carrying one, <input name="auth" value="...">, its attributes in any order.
_INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_SECRET_FIELD_NAME = re.compile(rf"""\bname\s*=\s*(["']?){_TRACKER_KEYS}\1(?=[\s/>])""", re.IGNORECASE)
_FIELD_VALUE = re.compile(r"""(\bvalue\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s"'>]+)""", re.IGNORECASE)
# A response header naming a credential. A cookie keeps its name and attributes, only its value is masked.
_SECRET_HEADER_NAME = re.compile(r"auth|token|key|session|pass", re.IGNORECASE)
_COOKIE_VALUE = re.compile(r"^(\s*[^=;\s]+=)[^;]*")


def _mask_secret_field(match: re.Match[str]) -> str:
    tag = match[0]
    if not _SECRET_FIELD_NAME.search(tag):
        return tag
    return _FIELD_VALUE.sub(r'\1"[REDACTED]"', tag)


def redact_tracker_text(text: str, known: Iterable[str | None] = ()) -> str:
    """Mask a tracker's secrets in a page, an API answer or a URL before it reaches the terminal."""
    text = _mask_known(text, known)
    text = _INPUT_TAG.sub(_mask_secret_field, text)
    text = _TRACKER_ASSIGNMENT.sub(lambda m: f"{m[1]}{m[2]}[REDACTED]{m[2]}", text)
    text = _TRACKER_QUERY.sub(r"\1[REDACTED]", text)
    text = _ANNOUNCE_PASSKEY.sub("/[REDACTED]", text)
    return _URL_USERINFO.sub(r"\1[REDACTED]@", text)


def redact_tracker_headers(headers: Iterable[tuple[str, str]], known: Iterable[str | None] = ()) -> dict[str, str]:
    """A tracker's response headers for the log, with credentials and cookie values masked."""
    known = list(known)
    shown: dict[str, str] = {}
    for name, value in headers:
        if "cookie" in name.lower():
            value = _COOKIE_VALUE.sub(r"\1[REDACTED]", value)
        elif _SECRET_HEADER_NAME.search(name):
            value = "[REDACTED]"
        value = redact_tracker_text(value, known)
        # A header sent more than once (Set-Cookie) is shown once, its values joined as HTTP folds them.
        shown[name] = f"{shown[name]}, {value}" if name in shown else value
    return shown
