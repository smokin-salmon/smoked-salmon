# MusicBrainz recordings

What MusicBrainz's JSON API and the Cover Art Archive sent, kept so the tests
parse real answers without a network (#1065). `tests/helpers/musicbrainz.py`
serves them from a local server.

| Path | What it is |
| --- | --- |
| `release/<mbid>.json` | `/ws/2/release/<mbid>?fmt=json`, includes below |
| `search/<slug>.json` | `/ws/2/release/?query=<query>&limit=10&fmt=json` |
| `caa/<mbid>.json` | the Cover Art Archive's `/release/<mbid>` image list |

The artist-credit casebook (`tests/fixtures/artist_credits/`) names its
release here by MBID. A search's slug is its query in lower case, each run of
other characters than letters and digits turned into one `-`.

## How they were fetched

By hand, once, never from a test or CI. One request at a time, at most one a
second, with the User-Agent
`smoked-salmon-fixtures/<version> ( https://github.com/smokin-salmon/smoked-salmon/ )`,
as MusicBrainz asks.

Includes for a release:
`artists labels recordings release-groups media artist-credits artist-rels
recording-level-rels`.

These answers were first recorded in both of MusicBrainz's formats, XML and
JSON, fetched one straight after the other. The casebook's `today`, the search
results and the covers came out the same from the JSON as from the XML before
salmon moved to the JSON; the XML was then dropped.

## Trimmed

Each answer keeps only the fields salmon's parsers read:

| Part | Kept |
| --- | --- |
| release | id, title, date, barcode, release event dates, the cover's `front` flag |
| release group | id, title, first release date, primary and secondary types |
| label info | label name, catalogue number |
| medium | position, format |
| track | id, position, number, title, artist credit |
| recording | id, title, first release date, artist credit, remixer relations |
| artist credit | join phrase, credited name, artist id and name |
| search result | id, title, artist credit, label info, track count, format |

To add or refresh a release, fetch it and trim it the same way. If the
casebook's `today` changes, say why in the commit.
