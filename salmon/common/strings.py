import re
import unicodedata

from salmon.common.regexes import re_strip
from salmon.constants import GENRE_LIST
from salmon.errors import GenreNotInWhitelist


def make_searchstrs(artists, album, normalize=False):
    artists = [a for a, i in artists if i == "main"]
    album = album or ""
    album = re.sub(r" ?(- )? (EP|Single)", "", album)
    album = re.sub(r"\(?[Ff]eat(\.|uring)? [^\)]+\)?", "", album)

    if len(artists) > 3 or (artists and any("Various" in a for a in artists)):
        search = re_strip(album, filter_nonscrape=False)
    elif len(artists) == 1:
        search = re_strip(artists[0], album, filter_nonscrape=False)
    elif len(artists) <= 3:
        search = [re_strip(art, album, filter_nonscrape=False) for art in artists]
        return normalize_accents(*search) if normalize else search
    return [normalize_accents(search) if normalize else search]


def normalize_accents(*strs):
    return_strings = []
    for str_ in strs:
        nkfd_form = unicodedata.normalize("NFKD", str_)
        return_strings.append("".join(c for c in nkfd_form if not unicodedata.combining(c)))
    if not return_strings:
        return ""
    return return_strings if len(return_strings) > 1 else return_strings[0]


def less_uppers(one, two):
    """Return the string with less uppercase letters."""
    one_count = sum(1 for c in one if c.islower())
    two_count = sum(1 for c in two if c.islower())
    return one if one_count >= two_count else two


def strip_template_keys(template, key):
    """Strip all unused brackets from the folder name."""
    folder = re.sub(r" *[\[{\(]*{" + key + r"}[\]}\)]* *", " ", template).strip()
    return re.sub(r" *- *$", "", folder)


def fetch_genre(genre):
    normalized = normalize_accents(genre)
    key_search = re.sub(r"[^a-z]", "", normalized.lower().replace("&", "and"))
    try:
        return GENRE_LIST[key_search]
    except KeyError:
        raise GenreNotInWhitelist from None


def truncate(string, length):
    if len(string) < length:
        return string
    return f"{string[: length - 3]}..."
