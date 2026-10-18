import html
import re
import unicodedata

from salmon.common.regexes import re_strip
from salmon.constants import GENRE_LIST
from salmon.errors import GenreNotInWhitelist

_RE_NUMBERED_SUFFIX = re.compile(r" \(\d+\)$")
_RE_NUMBERED_SUFFIXES = re.compile(r" \(\d+\)(?= |$)")


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
    album = re.sub(r" ?(- )? (EP|Single)", "", album)
    album = re.sub(r"\(?[Ff]eat(\.|uring)? [^\)]+\)?", "", album)

    search: str | list[str]
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


def less_uppers(one, two):
    """Return the string with less uppercase letters."""
    one_count = sum(1 for c in one if c.islower())
    two_count = sum(1 for c in two if c.islower())
    return one if one_count >= two_count else two


def strip_template_keys(template, key):
    """Strip all unused brackets from the folder name."""
    folder = re.sub(r" *[\[{\(]*{" + key + r"}[\]}\)]* *", " ", template).strip()
    return re.sub(r" *- *$", "", folder)


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
    return [part.strip() for part in _GENRE_SEPARATORS.split(genre) if part.strip()]


def tagify(genre: str) -> str:
    """One tracker tag: words joined by dots, "&" spelled out because it is not a tag character."""
    tag = re.sub(r"\s*&\s*", ".and.", genre)
    tag = re.sub(r"[-_ /]+", ".", tag)
    return re.sub(r"\.+", ".", tag).strip(".")


def truncate(string, length):
    if len(string) < length:
        return string
    return f"{string[: length - 3]}..."
