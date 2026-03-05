import re

from salmon.common.constants import (
    _RE_SPLIT,
    COPYRIGHT_SEARCHES,
    COPYRIGHT_SUBS,
)


def re_strip(*strs, filter_nonscrape=True):
    """Returns a joined string with non-alphanumerical characters stripped out."""
    str_ = " ".join(re.sub(r"[/\-\\,]", " ", (s or "").lower()) for s in strs)
    while "  " in str_:
        str_ = str_.replace("  ", " ")
    if filter_nonscrape:
        return re.sub(r"[\.\(\)]", "", str_)
    return str_


def re_split(stri):
    """
    Return a list of strings split based on characters commonly utilized
    as separators stored in a constant.
    """
    return [s.strip() for s in _RE_SPLIT.split(stri) if s.strip()]


def parse_copyright(copyright):
    """
    Filter out a bunch of shit from the copyright fields provided on Apple Music
    and Tidal pages. Their copyright info does not always accurately represent
    the label, but it's the best we can do.
    """
    if not copyright:
        return ""
    for search in COPYRIGHT_SEARCHES:
        res = re.search(search, copyright, flags=re.IGNORECASE)
        if res:
            copyright = res[1]
    for sub in COPYRIGHT_SUBS:
        copyright = re.sub(sub, "", copyright, flags=re.IGNORECASE).strip()
    # In case labels are being combined with /, take only the first one.
    if "/" in copyright:
        copyright = copyright.split("/")[0].strip()
    return copyright or None
