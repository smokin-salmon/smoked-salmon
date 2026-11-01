import re
import shlex
import string
from collections.abc import Iterable
from urllib.parse import parse_qsl, quote, unquote, urlsplit

# Up to the authority's end: a "@" later in the query or fragment is no userinfo.
_URL_USERINFO = re.compile(r"(://)[^/?#\s@]+@")
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


_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
# What requests leaves unescaped when it re-quotes a URL before sending it; with a bad escape anywhere in the
# URL ("%ZZ"), it escapes every "%" instead.
_URI_SAFE = "!#$%&'()*+,/:;=?@[]~"
_URI_SAFE_NO_PERCENT = _URI_SAFE.replace("%", "")
# Path segments a client's own endpoint is made of; any other segment could be a proxy's key, whatever its shape.
_CLIENT_PATH_WORDS = frozenset(
    {"api", "v2", "json", "proxy", "plugins", "rpc", "rpc.php", "rpc2", "httprpc", "action.php", "rutorrent"}
) | {"deluge", "transmission", "qbittorrent"}
# A proxy's random key is longer; a shorter segment or value is a word, an id or an account name, and masking it
# alone would mask that word everywhere salmon web prints.
_MIN_LONE_SECRET = 12


def _requoted(text: str) -> str:
    """As requests re-quotes a URL and urllib3 repeats it: unreserved escapes decoded, the rest uppercased."""

    def escape(match: re.Match[str]) -> str:
        char = chr(int(match[0][1:], 16))
        return char if char in _UNRESERVED else match[0].upper()

    return quote(_ESCAPE.sub(escape, text), safe=_URI_SAFE)


def _echoed_forms(secret: str) -> list[str]:
    """A URL part as written, decoded, and re-escaped the ways an HTTP client does before an error repeats it."""
    upper = _ESCAPE.sub(lambda match: match[0].upper(), secret)
    return [secret, unquote(secret), upper, _requoted(secret), quote(secret, safe=_URI_SAFE_NO_PERCENT)]


def redact_client_url(url: str) -> str:
    """A torrent client URL to show: its scheme, host and port, with any userinfo, path, query and fragment masked."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[REDACTED]"
    _, at, host = parts.netloc.rpartition("@")
    return (
        f"{parts.scheme}://{'[REDACTED]@' if at else ''}{host}"
        + ("/[REDACTED]" if parts.path.strip("/") else "")
        + ("?[REDACTED]" if parts.query else "")
        + ("#[REDACTED]" if parts.fragment else "")
    )


def url_secrets(url: str) -> list[str]:
    """A URL's password, path, query and fragment, and a long path segment or query value alone, as errors echo them."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return [url]
    path = parts.path.rstrip("/")
    keys = (
        segment
        for segment in path.split("/")
        if unquote(segment).casefold() not in _CLIENT_PATH_WORDS and len(unquote(segment)) >= _MIN_LONE_SECRET
    )
    values = (value for _, value in parse_qsl(parts.query, keep_blank_values=True) if len(value) >= _MIN_LONE_SECRET)
    found = [parts.password or "", path, *keys, parts.query, *values, parts.fragment]
    return list(dict.fromkeys(form for secret in found if secret for form in _echoed_forms(secret)))


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
_TRACKER_KEYS = r"(?:auth|authkey|passkey|torrent_pass|session|keeplogged|api_?key|authorization)"
# "authkey": "..." in JSON (or a Python repr of it), authkey = "..." in a page's script. A value ends at its closing
# quote, or at its line's end when an excerpt cut it off; a backslash escapes the next character.
_TRACKER_ASSIGNMENT = re.compile(
    rf"""(["']?\b{_TRACKER_KEYS}["']?\s*[:=]\s*)(["'])(?:(?!\2)[^\\\n]|\\.)*(\2?)""", re.IGNORECASE
)
# ?authkey=...&torrent_pass=... in a link or a redirect, &amp;-escaped in a page, or a cookie's name=value in text.
# A value ends at "; ", a cookie's attribute separator; a ";" with no space after stays in it, as in a URL query.
# At least one character: a quoted value (authkey="...") is _TRACKER_ASSIGNMENT's, which a match here would hide.
_TRACKER_QUERY = re.compile(rf"""\b({_TRACKER_KEYS}=)(?:[^&;#\s"'<>]|;(?!\s))+""", re.IGNORECASE)
# A cookie's name=value before its "; " attributes: its value may hold "&", which would end a query's.
_TRACKER_COOKIE = re.compile(rf"""\b({_TRACKER_KEYS}=)(?:[^;\s"'<>]|;(?!\s))+(?=;\s)""", re.IGNORECASE)
# The passkey in an announce URL, whatever its length: https://tracker/<passkey>/announce.
_ANNOUNCE_PASSKEY = re.compile(r"/[A-Za-z0-9]+(?=/announce)")
# A form field carrying one, <input name="auth" value="...">, its attributes in any order.
# Quote-aware: a ">" inside a quoted attribute does not end the tag.
_INPUT_TAG = re.compile(r"""<input\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
_SECRET_FIELD_NAME = re.compile(rf"""\bname\s*=\s*(["']?){_TRACKER_KEYS}\1(?=[\s/>])""", re.IGNORECASE)
_FIELD_VALUE = re.compile(r"""(\bvalue\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s"'>]+)""", re.IGNORECASE)
# A response header naming a credential. A cookie keeps its name and attributes, only its value is masked.
_SECRET_HEADER_NAME = re.compile(r"auth|token|key|session|pass|secret", re.IGNORECASE)
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
    text = _TRACKER_COOKIE.sub(r"\1[REDACTED]", text)
    text = _TRACKER_QUERY.sub(r"\1[REDACTED]", text)
    text = _ANNOUNCE_PASSKEY.sub("/[REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    # Last: a value with no closing quote runs to its line's end, past text another pattern would have masked.
    return _TRACKER_ASSIGNMENT.sub(lambda m: f"{m[1]}{m[2]}[REDACTED]{m[3]}", text)


def redact_tracker_headers(headers: Iterable[tuple[str, str]], known: Iterable[str | None] = ()) -> dict[str, str]:
    """A tracker's response headers for the log, with credentials and cookie values masked."""
    known = list(known)
    shown: dict[str, str] = {}
    for name, value in headers:
        # The name first: one that also says "cookie" has a bare value the cookie mask would not match.
        if _SECRET_HEADER_NAME.search(name):
            value = "[REDACTED]"
        elif "cookie" in name.lower():
            value = _COOKIE_VALUE.sub(r"\1[REDACTED]", value)
        value = redact_tracker_text(value, known)
        # A header sent more than once (Set-Cookie) is shown once, its values joined as HTTP folds them.
        shown[name] = f"{shown[name]}, {value}" if name in shown else value
    return shown
