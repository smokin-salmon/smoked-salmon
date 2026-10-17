"""Match local files to a metadata source's tracklist by content.

Pairing them rank by rank (the Nth file in sorted order gets the Nth scraped track) breaks on promo folders,
which often arrive unnumbered: sorted order is then alphabetical and unrelated to the release order, so every
file receives another track's title, artists and ISRC.

The same pairing is made twice, at two layers that don't share state: in ``tagger.combine``, to merge scraped
fields onto the local track dicts, and in ``tagger.retagger``, to decide which file each track's tags are written
to. Both call in here so they can't disagree.
"""

import re

from unidecode import unidecode

from salmon.common import re_strip

# Seconds two tracks may differ by and still be taken for the same recording.
DURATION_TOLERANCE = 2


def norm_title(title):
    """Normalise a title the way the merge in combine.py compares them."""
    return re_strip(unidecode(title)) if title else ""


def title_words(title):
    """The title's words, so a local title is found in a longer scraped one only as whole words."""
    return tuple(re.findall(r"[a-z0-9]+", unidecode(title).lower())) if title else ()


def contains_words(words, within):
    """Whether `words` appear, in order and next to each other, in `within`."""
    size = len(words)
    return size > 0 and any(within[start : start + size] == words for start in range(len(within) - size + 1))


def norm_isrc(isrc):
    return re.sub(r"[^A-Z0-9]", "", isrc.upper()) if isrc else ""


def pair_by_content(locals_, metas):
    """Work out which local track belongs to each scraped track.

    Both arguments are sequences of mappings with ``title``, ``isrc`` and ``duration`` keys; a missing or empty
    one is skipped as a signal.

    Passes run in confidence order: ISRC, exact title, a local title found as whole words in the scraped one
    ("Django" in "Django (Original Mix)", but not "Work" in "Homework"), then duration. A pass only pairs a
    scraped track and a local track when each is the other's only candidate left, so an ambiguous signal defers
    to a later pass rather than guessing. Whatever is still unpaired is paired by position, in order, which is
    what happened before.

    Returns ``(pairing, reasons)``, both of length ``len(metas)``. ``pairing[i]`` is the index into ``locals_``
    of the track belonging to ``metas[i]``, or ``None`` when no local track is left for it; ``reasons[i]`` is
    the pass that paired it ("isrc", "title", "partial title", "duration" or "position").
    """
    pairing: list[int | None] = [None] * len(metas)
    reasons: list[str | None] = [None] * len(metas)
    unclaimed = list(range(len(locals_)))

    def claim(mi, li, why):
        pairing[mi] = li
        reasons[mi] = why
        unclaimed.remove(li)

    def pending():
        return [mi for mi in range(len(metas)) if pairing[mi] is None]

    def run_pass(why, want_of, matches):
        wants = {mi: want for mi in pending() if (want := want_of(metas[mi]))}
        for mi, want in wants.items():
            hits = [li for li in unclaimed if matches(locals_[li], want)]
            if len(hits) != 1:
                continue
            # The local track must not fit another scraped track still waiting in this pass either: a file
            # titled "Django" is not claimed by "Django (Original Mix)" while "Django (Dub)" also wants it.
            rivals = [mj for mj, other in wants.items() if pairing[mj] is None and matches(locals_[hits[0]], other)]
            if rivals == [mi]:
                claim(mi, hits[0], why)

    # ISRC is the only authoritative key, so it goes first.
    run_pass("isrc", lambda m: norm_isrc(m.get("isrc")), lambda t, want: norm_isrc(t.get("isrc")) == want)

    run_pass("title", lambda m: norm_title(m.get("title")), lambda t, want: norm_title(t.get("title")) == want)

    run_pass(
        "partial title",
        lambda m: title_words(m.get("title")),
        lambda t, want: contains_words(title_words(t.get("title")), want),
    )

    # Duration last, as the tiebreaker between near-identical titles (an Original Mix and an Extended Mix).
    run_pass(
        "duration",
        lambda m: m.get("duration"),
        lambda t, want: bool(t.get("duration")) and abs(t["duration"] - want) <= DURATION_TOLERANCE,
    )

    for mi in pending():
        if not unclaimed:
            break
        claim(mi, unclaimed[0], "position")

    return pairing, reasons
