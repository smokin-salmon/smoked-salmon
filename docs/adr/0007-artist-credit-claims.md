# 0007. Artist credits are claims from each source, decided once per release by one function that gives its reason

- **Status:** Accepted
- **Date:** 2026-11-15
- **Links:** #869, #852, #854, #922, #947

## Context

When salmon merges a release's credits from the files and the stores, it keeps no record of which
source said what. So it can't weigh one source against another, and it can't say why it chose. Most of
the wrong credits in the casebook (`tests/fixtures/artist_credits/`, step 1 of #869) come back to that.

How the merge works today. Salmon credits an artist on a track as a name and a role, such as
`("Antonio Carlos Jobim", "guest")`, with the eight roles of `ARTIST_IMPORTANCES`. The files' tags are
read first and become the *base*. Each store is then merged into it, one at a time: the store of the
URL the user chose first, then the rest in `PREFERENCES` order. A store's credit is added to a track
unless the base already has that name in that role. After the last store, `filter_artists` keeps one
main or guest per name on each track, the first in the list: the files' when the files credit that name,
else the first store's that does. In `descgen` and salmon web's description page there are no files, and
the first store becomes the base.

Three root causes show up in the casebook:

- **Strings parsed back.** Sources are reduced to names and roles early, and later stages guess at the
  text again. A track's title is still read for a remixer whenever its store credits none (#1079
  narrowed the guess to "(X Remix)"), and a store's version is folded into its title, so the merge
  can't tell "Dark Fantasy" from "Dark Fantasy (Album Version (Explicit))" (#1077).
- **Decisions without sources.** On *Getz/Gilberto* the files make Jobim main and Qobuz makes him a
  guest. The files win because they come first, and nothing says the two disagreed.
- **Identity by spelling.** On *Ágætis byrjun*, Qobuz writes "Sigur Rós" on the album and "Sigur Ros" in
  each track's performers, so the band becomes its own guest.

Five rules choose a role today, each in its own place: the first-wins choice at the end of the merge;
the merge's title reading for remixers; Discogs' `parse_credits`, where an artist with another role on
a track loses main unless they also arranged it; the files' classical rule, where a name in
COMPOSER or CONDUCTOR loses its ARTIST credit; and `clean_metadata`'s, where a track's main who is
also its guest or remixer loses main, unless they are its only main.

What constrains the design:

- **Not a conversion of the existing code in this work** (#869). The metadata is a plain dict, read by
  key about 250 times, and its JSON is visible to users: `salmon meta -o`, salmon web's
  `/api/metadata`, the manual metadata editor, the folder template's keys.
- **The evidence is narrow** (decision 6): 29 releases, with no Tidal, Beatport or Apple Music data.
- **Agreed on #869:** `Role` as a Literal with an unmapped role as `None`; a credit kept as the source
  stated it, with the track's `version` beside its title; one precedence function that returns each
  credit with its reason; then the stores, then the files.

Changes from #869's plan, each for a reason given below:

- Step 2 replaces one of the rules, the merge's first-wins choice. Three others leave with the step
  that makes them unnecessary (decision 4); `clean_metadata`'s stays (decision 3).
- The stores' order is Qobuz, Deezer, Discogs, MusicBrainz, Apple Music. Deezer is added because it is
  second in `PREFERENCES`, ahead of Qobuz, and like Qobuz it folds a track's version into the title
  (#1077). MusicBrainz is moved up
  because its parser is to be rewritten anyway (#1065).
- Until its own PR, a store's roles count as guesses (decision 2).
- `resolve_credits` lives in `salmon/common/credits.py` with the types, not in `tagger/sources/base.py`.

Options considered:

- **Structs for the release and track now.** They would check each value as it is built, but they reach
  every dict read and each JSON boundary, and the credit work would wait behind them.
- **Typing today's rules as they are.** The rules stay spread out, and still can't say why they chose.
- **Deciding as each store is merged.** By the second store the base already holds the first store's
  credits, so the sources are lost again.
- **Every rule replaced in step 2.** That needs a Discogs-only branch inside the shared function, and
  every parser changed in one PR.

## Decision

### 1. TypedDicts for the release and track, structs for what is new

`salmon/common/credits.py` holds the types and `resolve_credits`. Nothing that exists today changes at
runtime.

- `Role` is a Literal of the eight roles. A source's role that maps to none of them is `None`, and
  `resolve_credits` leaves that claim out: it gives no credit and disputes none.
- `Credit = tuple[str, Role]`, the pair the code already uses.
- `ReleaseMetadata` and `TrackMetadata` are TypedDicts of the keys salmon builds today, with
  `NotRequired` for keys only some sources set. A test checks them against `generate_track` and the
  files' `create_track_list`. A module adopts them when a PR touches it anyway.
- `Claim` and `Reason` are frozen msgspec structs, as `interaction` (0006) uses.

### 2. A claim is what one source said

```python
class Claim(msgspec.Struct, frozen=True):
    source: Source  # a METASOURCES name, or "files"
    name: str
    credited_as: str  # the name exactly as this source printed it
    role: Role | None  # None: the source's role maps to none of the eight
    assumed: bool  # the role is a guess, not data the source gave
```

A role is *assumed* when salmon guessed it, and *stated* when the source gave it as data. A plain ARTIST
value as main and a remixer read from a title are guesses. An assumed claim always has a role, checked
when it is built.

Two exceptions hold during the migration, so that step 2 changes no credit:

- **A store's claims count as assumed until its own PR** reads its credits properly. Until then its
  roles are salmon's reading of its strings, and some are guesses (Tidal infers main from the album). A
  store with no PR yet stays that way until it has one.
- **The files' plain ARTIST counts as stated until step 4**, as the merge treats it today.

`credited_as` comes from Discogs' credited name (ANV), which the merge already keeps as
`artist_variations`. The other stores print one name, so it is the same as `name`.

### 3. One function decides, and says why

`resolve_credits` takes the release's claims and returns each track's credits, each with a `Reason`: the
rule that decided, the sources it came from, and the sources that disagreed.

- **The limit:** main and guest are one performing credit per name per track (#852). Other roles are
  added together.
- **Across roles:** a track's main who is also its guest or remixer loses main, unless they are the
  track's only main (an artist remixing their own track). Salmon does this today in `clean_metadata`,
  after the merge, whichever source gave each credit, and it stays there until a case needs the
  sources weighed. On *The Remix*, the files' plain ARTIST lists LoveGame's remixer Chew Fu beside
  the singers; Discogs credits him as "Chew-Fu Phat", so the rule doesn't see one name. Once Qobuz
  states him as remixer under the files' spelling, his main goes: the case moves in step 3.
- **The tie-breaks, in order:** a stated role beats an assumed one; then the base, then the stores in
  the merge's order.
- **Order:** a track's main artists keep the order the release gives them, from a store's album
  artists or the files' ALBUMARTIST (step 4), else the base's order.

A new tie-break is a new line in this list, never a score. Reasons never go into tags.

**Salmon prints the disputes** after the merge, the way it prints how it paired local tracks with a
store's. Names the same stores dispute share a line, so it is a few lines per release, not one per
track. For example, *Getz/Gilberto* after step 2:

```
Kept the files' main where stores say guest:
  João Gilberto, João Gilberto Quintet (guest at Qobuz)
  Antonio Carlos Jobim (guest at Deezer, MusicBrainz, Qobuz)
  Astrud Gilberto (guest at Deezer, Qobuz)
```

With `-yyy` it prints and asks nothing. Like #922's warnings, it says what salmon kept and goes on. The
user can change a credit in the review, as today.

**Version beside the title.** A store that gives a track's version apart keeps it apart (`version` in
`TrackMetadata`, added by the first store PR that fills it). The title is written from its parts in one
place, following the `strip_useless_versions` setting, and a remixer is read from the version, not the whole title. The
two title cases left open by #947 ("Heroic Feat", and "Song (feat. B)" with guest C) are decided in that
same place, from the guests `resolve_credits` gives.

### 4. Where it runs, and what stays

- **Step 2:** as the merge pairs each store's tracks with the base's, it records which source gave each
  credit. Matching stays as it is. After the last store, each name first takes the spelling salmon
  already picks from all its spellings on the release, so choosing a role never changes a spelling:
  the helper `generate_artists` already uses (`_generate_artist_pool_lower_case`, which #1088 renames
  `artist_spellings`), keeping per accent-folded name the spelling with fewer capitals.
  Then `resolve_credits` runs, in place of the first-wins choice. `generate_artists` still runs where
  it does today, after the merge's cleanup and after review edits; on credits already decided it has
  nothing left to choose.
- **Step 3**, one store per PR: each reads its store's credits as stated claims, and `scrape_release`
  stops running `filter_artists` for that store. The title reading for remixers leaves each store as
  its version is kept apart. `parse_credits` leaves with the Discogs PR.
- **Step 4:** the files' plain ARTIST becomes assumed, ALBUMARTIST gives the order, and the classical
  rule becomes part of the files' claims.
- **What stays, on purpose:** a review edit has no sources to weigh, so it doesn't go through
  `resolve_credits` (#854). A rule about reading one source stays in that source's parser, such as
  Tidal's inference. The name repairs stay where they are.

### 5. What each step proves

- **Before step 2's code, the casebook records each track's credits,** not only the release's.
- **Step 2 moves no album.**
- **Each later PR** moves its own cases, updates their `today` and `known_wrong`, and says why. While the
  files are stated, a store's fix to a main or guest shows in the merge only where the files say
  nothing. Across roles it can show sooner: a store's stated remixer drops the files' main through
  `clean_metadata` (decision 3, LoveGame). The files wait until the stores are fixed, because stating
  today's stores first would let their known errors win.

### 6. The rules follow the cases, and are expected to change

The casebook is small, and narrower than salmon's users. It holds 29 releases. Each has data from
Deezer, Qobuz, MusicBrainz and Discogs, two have Bandcamp's, and none has Tidal's, Beatport's or Apple
Music's. Every case's files are built from Deezer's data, in a few downloaders' tag shapes. The rules in
this record are what those cases support, not a complete model of artist credits.

- **A case that breaks a rule is expected, not a failure of the design.** The PR that adds it changes the
  rule, says why in the case, and amends this record, as 0005 was amended.
- **Prefer the smaller rule until a case asks for more.** A field, a tie-break or a role waits for the
  first case that needs it.
- **Widening the casebook is part of the work:** files from other stores and downloaders, and the stores
  it doesn't cover yet, each added with the PR that reads them.

## Consequences

- During the migration, credits exist in two forms: claims inside the merge, name and role pairs
  everywhere else.
- The files' known errors stay until step 4. When a file's ARTIST lists the guests beside the main
  artist (the shape of the *Funk Wav Bounces* case), every guest stays main, and the print says so.
- Uploads where the sources disagree print a few more lines.
- The TypedDicts catch little until code adopts them: JSON from a store is "anything" to the type
  checker.
- **Where this is going, not decided here:** the release and track as structs, as their own proposal
  after the credit steps, with a check at each edge first that warns and doesn't block; `Credit` as a
  NamedTuple with `.name` and `.role`, which still equals a plain pair; `Claim.artist_id` and the
  source's own role words, each added by the first PR that fills and reads it; one key for an artist's
  name, with the Qobuz PR (*Ágætis byrjun*).
- **Ruled out:** a role read from a title once a source gives it as data; a rule choosing between
  sources outside `resolve_credits`; reasons in tags; a numeric confidence.
