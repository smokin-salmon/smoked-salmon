# MusicBrainz recordings

What MusicBrainz and the Cover Art Archive sent, kept so the tests parse real
answers without a network. Each answer is recorded in both of MusicBrainz's
formats, XML and JSON, fetched one straight after the other, so the two
describe the same data (#1065).

| Path | What it is |
| --- | --- |
| `release/<mbid>.xml`, `.json` | `/ws/2/release/<mbid>` with salmon's includes |
| `search/<slug>.xml`, `.json` | `/ws/2/release/?query=<query>&limit=10` |
| `caa/<mbid>.json` | the Cover Art Archive's `/release/<mbid>` image list |

The artist-credit casebook (`tests/fixtures/artist_credits/`) names its
release here by MBID.

## How they were fetched

By hand, once, never from a test or CI. One request at a time, at most one a
second, with the User-Agent
`smoked-salmon-fixtures/<version> ( https://github.com/smokin-salmon/smoked-salmon/ )`,
as MusicBrainz asks.

Includes for a release:
`artists labels recordings release-groups media artist-credits artist-rels
recording-level-rels`. The JSON is the same URL with `&fmt=json`.

## Trimmed

Each answer keeps only the fields salmon's parsers read, and the trimmed XML
parses to the same release as the full answer. The JSON keeps the matching
fields:

| Part | Kept |
| --- | --- |
| release | id, title, date, barcode, release event dates, the cover's `front` flag |
| release group | id, title, first release date, type (XML), primary and secondary types |
| label info | label name, catalogue number |
| medium | position, format |
| track | id, position, number, title, artist credit |
| recording | id, title, first release date, artist credit, remixer relations |
| artist credit | join phrase, credited name, artist id and name |
| search result | id, title, artist credit, label info, track count, format |

To add or refresh a release, fetch both formats together and trim them the
same way. If the casebook's `today` changes, say why in the commit.
