# AGENTS.md

Guidance for AI coding agents (and humans) working on smoked-salmon. Contribution workflow,
branch names and commit style are in [CONTRIBUTING.md](CONTRIBUTING.md); this file covers what an
agent needs to work safely in the code.

smoked-salmon is a CLI that checks, tags and uploads music releases to Gazelle-based private
trackers (RED, OPS, DIC). Users run it against their own tracker accounts, so **a bug that sends
bad or excessive requests can get a user's account warned or their IP banned.** Treat tracker
traffic as the most sensitive part of the code.

## Commands

```bash
uv sync                          # install, including dev dependencies
uv run pytest                    # tests
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run basedpyright              # type check (uv run --with basedpyright basedpyright if not installed)
```

`ruff`, `pyright` and `pytest` are required checks on `master`; a PR cannot merge unless all three
pass.

The `salmon` package loads and validates its config at import time and exits if there is none.
`tests/conftest.py` handles this for the test run by pointing `XDG_CONFIG_HOME` at a temporary
copy of `src/salmon/data/config.default.toml`. If you import `salmon` outside pytest, you need a
config at `~/.config/smoked-salmon/config.toml` (Linux) or a `config.toml` at the repo root, and
every directory it names must exist.

## Layout

| Path | What lives there |
|---|---|
| `src/salmon/run.py`, `commands.py` | CLI entry point (`salmon`) and top-level commands |
| `src/salmon/config/` | Config schema (`validations.py`, msgspec structs); validation runs in `__post_init__` |
| `src/salmon/data/config.default.toml` | The config new users start from; keep it valid and in sync with the schema |
| `src/salmon/data/version.toml` | Version and changelog. **See Releases below before touching it** |
| `src/salmon/trackers/` | Tracker API clients. `base.py` (`BaseGazelleApi`) does all tracker HTTP |
| `src/salmon/uploader/` | The upload flow (`__init__.py`), dupe/request checks, spectrals, seedbox, torrent clients |
| `src/salmon/sources/` | Shared HTTP scraper per store (Qobuz, Tidal, Apple Music, Bandcamp, ...) |
| `src/salmon/search/` | Store search, built on `sources/` |
| `src/salmon/tagger/` | Metadata gathering, review, retagging, renaming; `tagger/sources/` parses store metadata |
| `src/salmon/checks/` | Log, integrity, upconvert and MQA checks |
| `src/salmon/converter/` | Transcoding and downconversion (shells out to `flac`, `sox`, `lame`) |
| `src/salmon/images/` | Image host uploaders; one module per host, registered in `HOSTS` |
| `src/salmon/web/` | Small aiohttp server for viewing spectrals |
| `docs/adr/` | Decision records: read before re-proposing something they rule out |

## Tracker safety rules

- **All tracker requests go through `BaseGazelleApi._request`.** It applies the shared rate
  limiter (5 requests per 10 s), the kept-alive connection pool and the retry policy. Do not open
  a new `aiohttp.ClientSession` to a tracker anywhere else.
- **Do not add request loops without a bound.** A feature that issues one request per item (per
  torrent, per group, per page) must cap the count or make it opt-in. Issue #432 is the example of
  what goes wrong: one check sends ~99 requests per upload.
- **Retries multiply traffic.** A retried request counts against the user's rate limit too. Only
  retry on errors that are genuinely transient.
- **Never test against a live tracker from code or CI.** Use a local fake server; see
  `tests/test_trackers_session.py` for the pattern.
- API-key requests must send no session cookie, and cookie requests no `Authorization` header.
  `_request` enforces this; keep it that way.

## Conventions

- Python 3.11+, ruff line length 120, async throughout (`anyio`, `aiohttp`, `asyncclick`).
- Config: add new settings to the msgspec schema with a default, validate them in
  `__post_init__`, and document them (commented out if optional) in `config.default.toml`. New
  settings must not change behaviour for existing configs unless that is the point of the change.
- Every bug fix comes with a test that fails without the fix.
- Keep PRs to one logical change. Maintainers squash-merge.
- Image hosts that only display for one tracker's members (RED's) must never be usable through a
  setting shared by all trackers. See [ADR 0002](docs/adr/0002-per-tracker-cover-host.md).

## Releases

`release_notification.py` fetches `src/salmon/data/version.toml` **from `master` on GitHub** to
tell every user a new version exists. Changing `current` there on `master` is therefore the
release announcement itself. Only bump it in a dedicated release commit, together with
`version` in `pyproject.toml` and a new `[[changelog]]` entry.

## Scratch work

Implementation plans and other throwaway notes go in `docs/plans/`, which is gitignored. Anything
that should outlive the PR belongs in the PR description, the issue, or an ADR in `docs/adr/`.
