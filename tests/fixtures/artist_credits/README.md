# Artist-credit casebook (#869)

One file per album, all widely released. `tests/test_artist_credit_casebook.py` runs them.

## A case

Each `<album>.toml` holds, in this order:

- `why`: what the album tests. `edition`: which release each store's data is.
- `today.<store>` (that store's parse) and `today.merge` (every store over the files, as `salmon up` merges them):
  the release's artists as "name (role)" and the track count. **The test enforces these.**
  With `output = true`, also `today.output` and `today.output-guests-in-title`: what is written from the merge with
  `guests_in_track_title` off and on, as ALBUMARTIST and per track "ARTIST | TITLE" (and "| REMIXER ..." when one is
  written).
- `right`: what is settled, and how we know (`how_we_know`). `release_main` (in order), `performers` (main or
  conductor, any order), `not_main` (never main: a composer, a placeholder, a guest, a remixer), `remixers`, `name_forms` (other spellings of a right name). `open` is what is
  not settled yet. Names and roles are judged, never the separators that join them.
- `known_wrong`: where today is wrong, in words.
- `[[track]]`: the files' tags, built from one store's data the way a download from that store is tagged (the
  comment above them names the store and the shape). Nobody's real files.
- `[salmon_tags]` (optional): each track's ARTIST and TITLE as salmon itself writes them, the other tags as in
  `[[track]]`. A control for the tags' shape: `today.output-salmon-tags` is the output from those files.
- `[stores]`: what each store sent (`deezer`, `musicbrainz`, `discogs`, `qobuz`, and `bandcamp` where the album is
  there), as the response's text, trimmed to the fields salmon's parser reads (the parse is the same as from the full
  response).

## Add a case

1. Make `<album>.toml` with `why`, `files_from` (the store the tags come from), `bandcamp_url` if there is
   Bandcamp data, and an empty `[right]` and `[today]` (the test needs both tables; `[right]` may hold only `open`).
2. Add the `[[track]]` tags and each store's response under `[stores]` as a `'''` string, trimmed: take out what the
   parser doesn't read and check the parse is unchanged. A full Discogs response can be tens of KB, so untrimmed
   cases would grow the repo quickly.
3. Run the test and paste each stage's value from its failure into `[today.<stage>]`.

## The scoreboard

`pytest -s tests/test_artist_credit_casebook.py::test_scoreboard` prints each case and stage as right, open
(nothing settled) or what is wrong ("a right main missing", "an extra main", ...), then "right on N of M" by what is
settled, once for the parse and merge stages and once for the output stages (an output mostly repeats its merge's
defects, so it is counted apart), and the count of each kind. Right means right on what `[right]` settles; guests are not judged yet. It
never fails on a wrong credit. An output stage is judged on ALBUMARTIST and each track's ARTIST before its "(feat. ...)": the one
release main there, and no `not_main` name.

## When a fix changes a credit

The case's `test_today` fails and names the stage and what salmon now gives, ready to paste. If the change is meant,
set that `[today.<stage>]` to it in the same PR, and update `known_wrong`. The diff then shows which albums the fix
moved, and the scoreboard's count shows whether it moved them towards `right`.

## Constructed cases

`constructed-*.toml` hold made-up titles and credits, no store data: `constructed = true` and `[[title_track]]`
rows (a title and its credits as (name, role)), run through the output stages only. They pin a title shape whose
right answer is still open.
