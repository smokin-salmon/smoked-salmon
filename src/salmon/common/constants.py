import re

SPLIT_CHARS = (
    r" \ ",
    "/",
    "; ",
    " & ",
    ", ",
)

COPYRIGHT_SEARCHES = (
    r".* \d{4} (.*)$",
    r"marketed by (.+?) under",
    r"(?:, )?under(?: exclusive)? licen(?:s|c)e to ([^,]+)",
    r"d/b/a (.+)",
)

COPYRIGHT_SUBS = (
    r".*(℗|©|\([pc]\))+",
    r"^(19|20)\d{2}",
    r"(, )?a division of.+",
    r"(, )?a .+company.+",
    r"all rights reserved.*",
    r"(,? )?llc",
    r"(,? )ltd",
    r"distributed by.+",
    r" inc.+$",
    r", a division of.+",
    r" +for the.+",
    r"[,\.]$",
    r"^ *, *",
    r"^Copyright ",
    r"(- )?(an )?imprint of.+",
    r"\d+ records dk2?",
)


_NO_BRACKET = r"[^()\[\]{}]"
# A bracket group, two deep: "(UK)", "(B (C))".
_BRACKETS = rf"[\(\[\{{](?:{_NO_BRACKET}|[\(\[\{{]{_NO_BRACKET}*[\)\]\}}])*[\)\]\}}]"
# The guests: a name and at most one bracket group of its own, "A (UK)" (#910). A group that doesn't close, or guests
# that start with a bracket, are read as before, up to the first closing bracket.
_GUESTS = rf"{_NO_BRACKET}+(?:{_BRACKETS}|[\(\[\{{][^\)\]\}}]*)?|[^\)\]\}}]+"
RE_FEAT = re.compile(rf" [\(\[\{{]?(?:f(?:ea)?t(?:uring)?\.?|with\.) ({_GUESTS})[\)\]\}}]?", flags=re.IGNORECASE)
# A join phrase between credited artists that makes those after it guests: " feat. ", " ft. ", " featuring ".
RE_FEAT_JOIN = re.compile(r"(?:feat|featuring|ft)\.?", flags=re.IGNORECASE)
# Only a whole (…) or […] ending the title's line, after its text, is a useless version: a word of the title stays.
RE_USELESS_VERSION = re.compile(
    r"(?<=\S)[ \t]*(?:(?P<paren>\()|\[)[ \t]*(?P<version>"
    r"Original(?: Mix)?|Remastered|Clean|"
    r"(?:Expanded|Deluxe|Anniversary|Limited|Collector'?s|Ultimate|Reissue|Bonus|Special)[ \t]+Edition|"
    r"Album[^()\[\]\n]*?(?:Edition|Mix)|feat(?:uring\b|\.|\b)[^()\[\]\n]+"
    r")[ \t]*(?(paren)\)|\])[ \t]*$",
    flags=re.IGNORECASE,
)
_RE_SPLIT = re.compile("|".join(re.escape(s) for s in SPLIT_CHARS))

AUDIO_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a"})
LOSSY_EXTENSIONS = frozenset({".mp3", ".m4a", ".ogg", ".opus"})
SCENE_EXTENSIONS = frozenset({".nfo", ".sfv", ".md5"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".pdf", ".gif"})
