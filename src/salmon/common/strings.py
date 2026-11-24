import html
import re
import unicodedata
from typing import NamedTuple

from unidecode import unidecode

from salmon import cfg
from salmon.common.constants import RE_FEAT
from salmon.common.regexes import re_split, re_strip
from salmon.constants import BLACKLISTED_CHARS, BLACKLISTED_FULLWIDTH_REPLACEMENTS, GENRE_LIST
from salmon.errors import GenreNotInWhitelist

_RE_NUMBERED_SUFFIX = re.compile(r" \(\d+\)$")
_RE_NUMBERED_SUFFIXES = re.compile(r" \(\d+\)(?= |$)")
# A release type ending a title: "EP", "E.P." or "Single" in any case, a whole word after a space, so "The Singles"
# and "Forever Single Mothers" keep theirs. Set apart, after a dash with a space before it (any dash, so "MAX-EP" stays
# a name) or in brackets, or bare: "Title - EP", "Title (Single)", "Title [EP]", "Title EP".
_RE_TYPE_MARKER = re.compile(
    r"(?:\s+(?P<dash>[-\u2010-\u2015])\s*|\s*(?P<bracket>[(\[])\s*|\s+)(?P<type>E\.?P\.?|Single)(?(bracket)\s*[)\]])\s*$",
    re.IGNORECASE,
)
# A credit in a title, in any case: "feat", "Feat.", "featuring" or "ft." as a whole word, or a bracket opening with
# "with" or "vs." ("Song (with B)"), with the names it holds.
_RE_CREDIT_WORD = re.compile(r"\b(?:feat|featuring|ft)\b", flags=re.IGNORECASE)
_RE_CREDIT_BRACKET = re.compile(r"[(\[]\s*(?:with|vs\.?)\s([^)\]]*)", flags=re.IGNORECASE)


def numeric_id(value: object) -> int | None:
    """An ID or a row, from a tracker's answer or typed: an int or a string of ASCII digits; None for anything else."""
    try:
        text = str(value) if isinstance(value, int | str) and not isinstance(value, bool) else ""
        return int(text) if text.isascii() and text.isdecimal() else None
    except ValueError:  # More digits than Python converts.
        return None


def strip_numbered_suffix(name: str) -> str:
    """Drop the trailing ` (N)` Discogs adds to tell artists of the same name apart."""
    return _RE_NUMBERED_SUFFIX.sub("", name)


def strip_numbered_suffixes(text: str) -> str:
    """Drop every ` (N)` that ends a word, for tag text that holds several credited names (`A (5) vs. B`)."""
    return _RE_NUMBERED_SUFFIXES.sub("", text)


def comparable(text: object) -> str:
    """Casefolded letters and digits of any script, accents and punctuation dropped, for loose equality of names."""
    # Trackers send HTML-escaped text ("Collector&#39;s"): unescaped first, or the entity's letters would count.
    plain = unicodedata.normalize("NFKD", html.unescape("" if text is None else str(text))).casefold()
    return "".join(char for char in plain if char.isalnum())


# What same_artist_key drops before `comparable`, one fold per row, each with the case that asked for it (#1131). A new
# spelling of one artist is a row here and a pair in tests/fixtures/name_pairs.toml. The rows run in order, on the name
# as `comparable` reads it (unescaped, accents dropped), between spaces or any dash.
_SEPARATOR = r"[\s\-\u2010-\u2015\u2212]+"
_ARTIST_FOLDS = (
    # The word "and" between two names, as "&" is dropped: "Dick Dale & His Del-Tones" (Pulp Fiction).
    (re.compile(rf"(?<=\S){_SEPARATOR}and{_SEPARATOR}(?=\S)", flags=re.IGNORECASE), " "),
    # A leading "The": "The Ahmad Jamal Trio" (MusicBrainz), "The-Dream".
    (re.compile(rf"^\W*the{_SEPARATOR}", flags=re.IGNORECASE), ""),
)


def same_artist_key(name: str) -> str:
    """The key two names share when they are one artist across a release: `comparable` after _ARTIST_FOLDS; when the
    folds leave no letters or digits, `comparable` alone ("The "), and the name itself when it has none ("!!!" is not
    "???")."""
    # An invisible character between two words ("The\u200bDream") parts them, as a space would.
    folded = "".join(
        " " if unicodedata.category(char) == "Cf" else char
        for char in unicodedata.normalize("NFKD", html.unescape(name))
        if not unicodedata.combining(char)
    )
    for pattern, replacement in _ARTIST_FOLDS:
        folded = pattern.sub(replacement, folded)
    return comparable(folded) or comparable(name) or name.strip()


_REPLACED_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})
_REMOVED_CATEGORIES = frozenset({"Cf", "Cc"})


def invisible_chars(name: str) -> list[str]:
    """The characters of a name that clean_name changes, each once, as "U+00A0 NO-BREAK SPACE".

    These are the Unicode spaces other than U+0020, the line and paragraph separators, and the format and
    control characters. A name holding none of them is left alone.
    """
    found: dict[str, str] = {}
    for char in name:
        category = unicodedata.category(char)
        if char != " " and (category in _REPLACED_CATEGORIES or category in _REMOVED_CATEGORIES):
            found.setdefault(char, f"U+{ord(char):04X} {unicodedata.name(char, 'CONTROL CHARACTER')}")
    return list(found.values())


def show_invisible(name: str) -> str:
    """A name with each character clean_name changes spelled out as <U+00A0>, so it can be seen."""
    return "".join(f"<U+{ord(c):04X}>" if invisible_chars(c) else c for c in name)


def has_visible_chars(name: str) -> bool:
    """Whether a name keeps a character once the spaces, format and control characters clean_name handles are gone."""
    return any(
        unicodedata.category(char) not in _REPLACED_CATEGORIES and unicodedata.category(char) not in _REMOVED_CATEGORIES
        for char in name
    )


_SUBTITLE_COLON = re.compile(r"\s*:\s+")


def substitute_forbidden_characters(name: str, *, subtitle_colon: bool = True) -> str:
    """*name* without the characters a tracker refuses in a path.

    With fullwidth_replacements on they become their full-width forms. Otherwise, with colon_as_dash on, a colon
    followed by whitespace (a subtitle: "Tin Cup: The Movie") becomes " - ". Every other forbidden character,
    and any colon left, becomes blacklisted_substitution. subtitle_colon=False leaves out the colon rule.
    """
    formatting = cfg.upload.formatting
    if formatting.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            name = name.replace(char, sub)
    elif subtitle_colon and formatting.colon_as_dash:
        name = _SUBTITLE_COLON.sub(" - ", name)
    return re.sub(BLACKLISTED_CHARS, formatting.blacklisted_substitution, name)


def clean_name(name: str) -> str:
    """A file or folder name (one path component) without invisible or odd Unicode characters.

    Unicode spaces other than U+0020 and the line and paragraph separators become a plain space; format and
    control characters (zero-width characters, the soft hyphen, byte order mark, direction marks, tab, newline)
    are removed. Runs of spaces collapse to one and the ends are stripped. A name that would end up empty is
    returned as it was.
    """
    chars = []
    for char in name:
        category = unicodedata.category(char)
        if category in _REMOVED_CATEGORIES:
            continue
        chars.append(" " if category in _REPLACED_CATEGORIES else char)
    cleaned = re.sub(r" {2,}", " ", "".join(chars)).strip(" ")
    return cleaned or name


def artist_keys(artists) -> set[str]:
    """comparable() forms a listing may name a release's artists by: each main artist, and all of them together.

    Args:
        artists: (name, importance) pairs. Without a main artist, every artist counts.
    """
    pairs = list(artists or [])
    names = [name for name, importance in pairs if importance == "main"] or [name for name, _ in pairs]
    return ({comparable(name) for name in names} | {comparable("".join(names))}) - {""}


def _credit_keys(part: str) -> set[str]:
    """comparable() forms a credit part may name an artist by: the whole part, and its pieces split on separators."""
    pieces = re.split(r" & |, | and ", part)
    return ({comparable(part)} | {comparable(piece) for piece in pieces}) - {""}


def composer_title_forms(title: object, composers) -> set[str]:
    """comparable() forms of a title: as it is, and without a leading "<composer>:" when that names a composer.

    A composer names the prefix by full name or by surname (the last word of the name). Any other prefix stays.
    """
    text = html.unescape("" if title is None else str(title))
    forms = {comparable(text)}
    prefix, colon, rest = text.partition(":")
    if colon:
        names = {comparable(name) for name in composers} | {
            comparable(name.split()[-1]) for name in composers if name.split()
        }
        if comparable(prefix) in names - {""}:
            forms.add(comparable(rest))
    return forms - {""}


def classical_artist_matches(credit: object, composers, performers) -> bool:
    """Whether a classical group's credit ("<composers> performed by <performers> under <conductors>") is ours.

    Its composer part must name one of our composers, and its performers part (up to " under ") one of our
    performers, each by full name. "Various Artists" names nobody; the conductor part is not compared.
    """
    composer_part, sep, rest = html.unescape("" if credit is None else str(credit)).partition(" performed by ")
    if not sep:
        return False
    performer_part = rest.split(" under ", 1)[0]
    nobody = {"", comparable("Various Artists"), comparable("Various")}
    our_composers = {comparable(name) for name in composers} - nobody
    our_performers = {comparable(name) for name in performers} - nobody
    return bool(_credit_keys(composer_part) & our_composers) and bool(_credit_keys(performer_part) & our_performers)


def join_artists(names) -> str:
    """The names as one credit, in the order given and without duplicates.

    ", " between them when there are more than two or any of them contains "&" ("Simon & Garfunkel, Paul Desmond"),
    otherwise " & ".
    """
    names = list(dict.fromkeys(names))
    sep = ", " if len(names) > 2 or any("&" in name for name in names) else " & "
    return sep.join(names)


def reaches_various_threshold(names) -> bool:
    """Whether there are ``various_artist_threshold`` or more names, not counting duplicates."""
    return len(set(names)) >= cfg.upload.formatting.various_artist_threshold


def names_no_artist(name: str) -> bool:
    """Whether a credit is "Various Artists" or "Various", by its whole name in any case: it names no artist."""
    return name.strip().casefold() in {"various artists", "various"}


def guests_from_title(title: str) -> list[str]:
    """The guests a title's featured credit names, unescaped ("Song (feat. A & B)": A and B).

    Salmon's reading of the title, not a store's credit: a guess.
    """
    feat = RE_FEAT.search(title)
    return [html.unescape(name) for name in re_split(feat[1])] if feat else []


def credit_in_title(title: str, guests=()) -> bool:
    """Whether a title already credits someone.

    A credit word always counts. A with or vs. bracket counts only when it names every one of ``guests``, so
    "Song (with Strings)" does not credit guest C. Given no guests to check, as a tracklist reading tags is, any such
    bracket counts.
    """
    if _RE_CREDIT_WORD.search(title):
        return True
    wanted = {comparable(name) for guest in guests for name in re_split(guest)}
    return any(
        wanted <= {comparable(name) for name in re_split(bracket[1])} for bracket in _RE_CREDIT_BRACKET.finditer(title)
    )


def artists_or_various(names) -> str:
    """``various_artist_word`` for ``various_artist_threshold`` or more names, otherwise ``join_artists(names)``."""
    names = list(names)
    return cfg.upload.formatting.various_artist_word if reaches_various_threshold(names) else join_artists(names)


class TypeMarker(NamedTuple):
    """A title without the release type ending it, that type ("EP", "Single", or None for none), and why."""

    title: str
    rls_type: str | None = None
    reason: str = "no type ends it"


def split_type_marker(title: object, bare: bool = True) -> TypeMarker:
    """Whether a release type ends the title, the one answer to it that every search and store reads (#969), with the
    reason.

    Set apart by a dash or brackets, it is always the type. Bare ("Title EP", "Forever Single") it may be a word of the
    name: bare=False keeps it. A title that is only the type ("EP", "(Single)") is a name and has none.
    """
    text = "" if title is None else str(title)
    match = _RE_TYPE_MARKER.search(text)
    if not match:
        return TypeMarker(text)
    if not text[: match.start()].strip():
        return TypeMarker(text, reason="the whole title, so a name")
    if match["dash"]:
        reason = "set apart by a dash"
    elif match["bracket"]:
        reason = "in brackets"
    elif bare:
        reason = "bare, read as the type"
    else:
        return TypeMarker(text, reason="bare, kept as a word of the name")
    return TypeMarker(text[: match.start()], "Single" if match["type"].lower() == "single" else "EP", reason)


def make_searchstrs(artists, album, normalize=False) -> list[str]:
    """Generate search strings from artists and album name.

    Args:
        artists: List of (artist_name, importance) tuples.
        album: Album name.
        normalize: Whether to normalize accents.

    Returns:
        List of search strings.
    """
    main_artists = [a for a, i in artists if i == "main"]
    album = album or ""
    # A bare type goes too: a wider search costs noise, a narrower one can miss a group.
    album = split_type_marker(album).title
    album = re.sub(r"\(?[Ff]eat(\.|uring)? [^\)]+\)?", "", album)

    search: str | list[str]
    # Each string is one tracker search, so this sets how many are sent. So the fixed "> 3", not
    # various_artist_threshold, which is about how a credit is written and can be set to anything. "Various" stays a
    # substring test, not names_no_artist: a wider search mostly costs noise, a narrower one can miss a group listed
    # under Various Artists.
    if len(main_artists) > 3 or (main_artists and any("Various" in a for a in main_artists)) or len(main_artists) == 0:
        search = re_strip(album, filter_nonscrape=False)
    elif len(main_artists) == 1:
        search = re_strip(main_artists[0], album, filter_nonscrape=False)
    else:
        # 2 or 3 main artists
        search_list = [re_strip(art, album, filter_nonscrape=False) for art in main_artists]
        if normalize:
            result = normalize_accents(*search_list)
            return result if isinstance(result, list) else [result]
        return search_list

    if normalize:
        result = normalize_accents(search)
        return [result] if isinstance(result, str) else result
    return [search] if isinstance(search, str) else search


def normalize_accents(*strs: str) -> str | list[str]:
    """Normalize accents in strings using NFKD form.

    Args:
        *strs: Variable number of strings to normalize.

    Returns:
        Single normalized string if one input, list if multiple, empty string if none.
    """
    normalized = ["".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)) for s in strs]
    if not normalized:
        return ""
    return normalized if len(normalized) > 1 else normalized[0]


def fetch_genre(genre: str) -> set[str]:
    """Fetch standardized genre from whitelist.

    Args:
        genre: The genre string to look up.

    Returns:
        Set of standardized genre strings.

    Raises:
        GenreNotInWhitelist: If genre is not in whitelist.
    """
    normalized = normalize_accents(genre)
    if isinstance(normalized, list):
        normalized = normalized[0] if normalized else ""
    key_search = re.sub(r"[^a-z]", "", normalized.lower().replace("&", "and"))
    try:
        return GENRE_LIST[key_search]
    except KeyError:
        raise GenreNotInWhitelist from None


# "\u2192" is Qobuz's hierarchy arrow. "&" separates only after one of these, as in Discogs's
# "Folk, World, & Country"; never alone, since the whitelist stores "Drum & Bass" whole.
_GENRE_SEPARATORS = re.compile(r"\s*[/;,\u2192]\s*(?:&\s*)?|\s+\\\s+")


def split_genre(genre: str) -> list[str]:
    """Split one genre string into the separate genres it names."""
    parts: list[str] = []
    start = 0
    depths = _bracket_depths(genre)
    for match in _GENRE_SEPARATORS.finditer(genre):
        if depths is not None and depths[match.start()] > 0:
            continue
        parts.append(genre[start : match.start()])
        start = match.end()
    parts.append(genre[start:])
    return [part.strip() for part in parts if part.strip()]


def _bracket_depths(text: str) -> list[int] | None:
    """The bracket depth before each character, or None when round or square brackets are unbalanced."""
    depths: list[int] = []
    stack: list[str] = []
    pairs = {")": "(", "]": "["}
    for char in text:
        depths.append(len(stack))
        if char in "([":
            stack.append(char)
        elif char in pairs and (not stack or stack.pop() != pairs[char]):
            return None
    return depths if not stack else None


def enclosed(text: str) -> bool:
    """Whether one pair of brackets encloses all of `text`: "(A (B))", not "(A) (B)"."""
    depths = _bracket_depths(text)
    return bool(text) and text[0] in "([" and depths is not None and all(depth > 0 for depth in depths[1:])


def unbracketed(version: str | None) -> str | None:
    """A version without the brackets a store may give it in ("(Mixed)"), its own kept ("Album Version (Explicit)");
    None for an empty one."""
    version = (version or "").strip()
    return (version[1:-1].strip() if enclosed(version) else version) or None


def restore_accents(title: str, accented: str) -> str:
    """`title` with the accented letters `accented` has where `title` has them plain, in `title`'s case: "Ny Batteri"
    and "Ný batterí" give "Ný Batterí". Its capitals and punctuation stay; any other difference leaves it as it is."""
    accented = unicodedata.normalize("NFC", accented)
    composed = unicodedata.normalize("NFC", title)
    if len(composed) != len(accented):
        return title
    letters = []
    for mine, theirs in zip(composed, accented, strict=True):
        if mine.lower() == theirs.lower() or not (mine.isalnum() or theirs.isalnum()):
            letters.append(mine)
        elif mine.lower() != mine.upper() and _plain_letter(theirs) == mine.lower():
            # A cased letter and its accents ("é" is "e" and an acute), never a look-alike from another script nor a
            # kana's voicing mark; in the title's case only where that is one letter too ("ǰ" has no one capital).
            cased = theirs.upper() if mine.isupper() else theirs.lower()
            letters.append(cased if len(cased) == 1 else mine)
        else:
            return title
    restored = "".join(letters)
    return title if restored == composed else restored


def _plain_letter(letter: str) -> str:
    """The plain letter an accented Latin one is: "é" is "e" and an acute, and "ø", "ł", "đ", which decompose to
    none, are "o", "l", "d", as unidecode reads them."""
    plain = unicodedata.normalize("NFD", letter)[0].lower()
    if (
        plain == letter.lower()
        and unicodedata.name(letter, "").startswith("LATIN")
        and len(folded := unidecode(letter)) == 1
    ):
        return folded.lower()
    return plain


def title_key(title: str) -> str:
    """A title as the pairing compares titles, by re_strip: lowercased and unaccented, dots and round brackets dropped,
    and slashes, dashes and commas read as spaces."""
    return re_strip(unidecode(title))


def tagify(genre: str) -> str:
    """One tracker tag: words joined by dots, "&" spelled out because it is not a tag character."""
    tag = re.sub(r"\s*&\s*", ".and.", genre)
    tag = re.sub(r"[-_ /]+", ".", tag)
    return re.sub(r"\.+", ".", tag).strip(".")
