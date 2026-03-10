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


RE_FEAT = re.compile(
    r" [\(\[\{]?(?:f(?:ea)?t(?:uring)?\.?|with\.) ([^\)\]\}]+)[\)\]\}]?",
    flags=re.IGNORECASE,
)
_RE_SPLIT = re.compile("|".join(re.escape(s) for s in SPLIT_CHARS))

LOSSY_EXTENSIONS = frozenset({".mp3", ".m4a", ".ogg", ".opus"})
SCENE_EXTENSIONS = frozenset({".nfo", ".sfv", ".md5"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".pdf", ".gif"})
