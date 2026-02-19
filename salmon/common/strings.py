import re
import unicodedata

from salmon.common.regexes import re_strip
from salmon.constants import GENRE_LIST
from salmon.errors import GenreNotInWhitelist


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


def truncate(string, length):
    if len(string) < length:
        return string
    return f"{string[: length - 3]}..."
