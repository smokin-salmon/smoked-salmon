# Artist-credit casebook (#869)

One file per album, all widely released. `tests/test_artist_credit_casebook.py` runs them.

## A case

Each `<album>.toml` holds, in this order:

- `why`: what the album tests. `edition`: which release each store's data is.
- `today.<store>` (that store's parse) and `today.merge` (every store over the files, as `salmon up` merges them):
  the release's artists as "name (role)" and the track count, and for the merge also `credits`, each track's as
  "disc.track: name (role), ...", so a change on one track is seen. **The test enforces these.** For a store that
  states its credits (`states_credits`), `today.<store>` is the undecided scrape the merge receives.
  With `output = true`, also `today.output` and `today.output-guests-in-title`: what is written from the merge with
  `guests_in_track_title` off and on, as ALBUMARTIST and per track "ARTIST | TITLE" (and "| REMIXER ..." when one is
  written). With `[[picard_track]]`, also `today.merge-picard` and (with `output = true`) `today.output-picard`: the
  merge and the output with `guests_in_track_title` off, over the Picard-tagged files. Each is recorded only where it
  differs from `today.merge` or `today.output`; where it is not recorded, the test holds it to that stage.
- `right`: what is settled, and how we know (`how_we_know`). `release_main` (in order), `performers` (main or
  conductor, any order), `not_main` (never main: a composer, a placeholder, a guest, a remixer), `remixers`, `name_forms` (other spellings of a right name). `open` is what is
  not settled yet. Names and roles are judged, never the separators that join them.
- `known_wrong`: where today is wrong, in words.
- `[[track]]`: the files' tags, built from one store's data the way a download from that store is tagged (the
  comment above them names the store and the shape). Nobody's real files.
- `[salmon_tags]` (optional): each track's ARTIST and TITLE as salmon itself writes them, the other tags as in
  `[[track]]`. A control for the tags' shape: `today.output-salmon-tags` is the output from those files.
- `[[picard_track]]` (optional, after `[[track]]`): the files tagged a second way, as Picard 3.0.5's default
  settings tag a CD rip from the case's MusicBrainz release (the comment above them names the settings), limited to
  the tags salmon reads. A case has them where they differ from `[[track]]`. The release is `stores.musicbrainz`,
  or `picard_release` (an MBID, with a comment saying why) where that is another edition from the files: Picard is
  tagged from the release that matches them, and `[stores]` still records what the stores answer. No stage reads
  them yet (#1133).
- `[stores]`: what each store sent (`deezer`, `musicbrainz`, `discogs`, `qobuz`, and `bandcamp` where the album is
  there), as the response's text, trimmed to the fields salmon's parser reads (the parse is the same as from the full
  response). Qobuz's also keeps the credits Qobuz sends beyond what the parser reads: the album's `artists[]`,
  each track's `performer` and performers line, and `composer` on a classical release, with `genres_list` as Qobuz
  sends it. MusicBrainz is the exception: `musicbrainz` holds the release's MBID, and its answer is in
  [`tests/fixtures/stores/musicbrainz/`](../stores/musicbrainz/README.md) with the other MusicBrainz recordings.
  `[sources]` says what each store's entry keeps.

## Add a case

1. Make `<album>.toml` with `why`, `files_from` (the store the tags come from), `bandcamp_url` if there is
   Bandcamp data, and an empty `[right]` and `[today]` (the test needs both tables; `[right]` may hold only `open`).
2. Add the `[[track]]` tags and each store's response under `[stores]` as a `'''` string (for MusicBrainz, the MBID,
   with the answers recorded as that folder's README says), trimmed: take out what the parser doesn't read (Qobuz's
   keeps more: see `[stores]` above) and check the parse is unchanged. A full Discogs response can be tens of KB, so
   untrimmed cases would grow the repo quickly.
3. Run the test and paste each stage's value from its failure into `[today.<stage>]`.

## The scoreboard

`pytest -s tests/test_artist_credit_casebook.py::test_scoreboard` prints each case and stage as right, open
(nothing settled) or what is wrong ("a right main missing", "an extra main", ...), then "right on N of M" by what is
settled, once for the parse and merge stages and once for the output stages (an output mostly repeats its merge's
defects, so it is counted apart), and once each for the Picard merge and output stages, over every case with
`[[picard_track]]` whether its Picard stage is recorded or the same as the Deezer one, and the count of each kind. Right means right on what `[right]` settles; guests are not judged yet. It
never fails on a wrong credit. An output stage is judged on ALBUMARTIST and each track's ARTIST before its "(feat. ...)": the one
release main there, and no `not_main` name.

## When a fix changes a credit

The case's `test_today` fails and names the stage and what salmon now gives, ready to paste. If the change is meant,
set that `[today.<stage>]` to it in the same PR, and update `known_wrong`. The diff then shows which albums the fix
moved, and the scoreboard's count shows whether it moved them towards `right`.

A fix that moves `today.merge` or `today.output` but not its Picard stage asks you to set `[today.merge-picard]` or
`[today.output-picard]` to the old value; one that makes them the same again asks you to remove it.

## Constructed cases

`constructed-*.toml` hold made-up titles and credits, no store data: `constructed = true` and `[[title_track]]`
rows (a title and its credits as (name, role)), run through the output stages only. They pin a title shape whose
right answer is still open.
