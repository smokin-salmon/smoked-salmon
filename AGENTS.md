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
uv run pytest -n auto            # tests, one worker per CPU (pytest-xdist), as in CI
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run basedpyright              # type check (the version locked in the dev group, as in CI)
```

`ruff`, `pyright` and `pytest` are required checks on `master`; a PR cannot merge unless all three
pass.

While iterating, run the tests of the package you change: the files named after it (`tests/test_<package>_*.py`;
`images/` is `test_image*`) and the others that import it (pytest skips a file given twice). It is a quick subset,
not the whole picture: run the full suite once before opening the PR.

```bash
uv run pytest -n auto tests/test_sources_*.py \
  $(grep -lE '\bsalmon(\.sources\b| import .*\bsources\b)' tests/test_*.py)
```

Leave out `-n auto` for a file or two, where starting the workers costs more than it saves. A run that reports a
warning fails (`max_warnings = 0` in `pyproject.toml`): fix the code that warns.

The `salmon` package loads and validates its config at import time and exits if there is none.
`tests/conftest.py` handles this for the test run by pointing `XDG_CONFIG_HOME` at a temporary
config built from the short `src/salmon/data/config.default.toml`, with fake tracker tables added
(`tests/helpers/shipped_config.py`). If you import `salmon` outside pytest, you need a
config at `~/.config/smoked-salmon/config.toml` (Linux) or a `config.toml` at the repo root, and
every directory it names must exist.

`SALMON_CONFIG_DIR` (set to `/config` in the Docker image) selects `$SALMON_CONFIG_DIR/config.toml`
when that file exists. If it is absent, salmon uses the existing platform config when available;
otherwise, it checks the repo-root `config.toml`. `tests/conftest.py` creates the selected file, so
the test run never reads a developer's config.

## Layout

| Path | What lives there |
|---|---|
| `src/salmon/run.py`, `commands.py` | CLI entry point (`salmon`) and top-level commands |
| `src/salmon/interaction.py` | Every question salmon asks the user (`prompt`, `confirm`, `edit`, `show_spectrals`) and `assume_defaults` (`-yyy`) |
| `src/salmon/config/` | Config schema (`validations.py`, msgspec structs); validation runs in `__post_init__` |
| `src/salmon/data/config.default.toml` | The short config new users start from. It has no tracker, so it loads only once one is filled in |
| `src/salmon/data/config.reference.toml` | Documents every key, commented out with its default; a test keeps it in sync with the schema |
| `src/salmon/data/version.toml` | Version and changelog. **See [docs/ci-and-releases.md](docs/ci-and-releases.md) before touching it** |
| `src/salmon/trackers/` | Tracker API clients. `base.py` (`BaseGazelleApi`) does all tracker HTTP |
| `src/salmon/uploader/` | The upload flow (`__init__.py`; one tracker's turn in `tracker_run.py`, a torrent's upload in `upload.py`, its group and torrent descriptions in `description.py`, its conversions' in `conversion_upload.py`; the cover in `cover_upload.py`, the checks before upload in `upload_checks.py`, the metadata review in `metadata_review.py`), dupe/request checks, spectrals, seedbox, torrent clients |
| `src/salmon/sources/` | Shared HTTP scraper per store (Qobuz, Tidal, Apple Music, Bandcamp, ...) |
| `src/salmon/search/` | Store search, built on `sources/` |
| `src/salmon/tagger/` | Metadata gathering, review, retagging, renaming; `tagger/sources/` parses store metadata |
| `src/salmon/checks/` | Log, integrity, upconvert and MQA checks |
| `src/salmon/converter/` | Transcoding and downconversion (shells out to `flac`, `sox`, `lame`) |
| `src/salmon/images/` | Image host uploaders; one module per host, registered in `HOSTS` |
| `src/salmon/web/` | Small aiohttp server for viewing spectrals |
| `src/salmon/webui/`, `webui/` | `salmon web` (ADR 0004): the aiohttp server and its jobs (`jobs.py`, each in a thread of its own), the browser's side of `salmon.interaction`, and the Svelte front end, whose build is committed in `src/salmon/webui/static/` |
| `docs/adr/` | Decision records: read before re-proposing something they rule out |
| `docs/ci-and-releases.md` | CI, the Docker image and how a release is cut |

## Tracker safety rules

- **All tracker requests go through `BaseGazelleApi._request`.** It applies the shared rate
  limiter (5 requests in any 10.5 s: 10 s plus a margin for network jitter, one budget per
  tracker) and the tracker's kept-alive connection pool, both in `trackers/account.py`, and the
  retry policy (`BaseGazelleApi._send`). Do not open a new `aiohttp.ClientSession` to a tracker
  anywhere else (`tests/test_rule_tracker_sessions.py` fails on one, or on the pool reached past `_send`, in
  `salmon.trackers`). That
  state belongs to one event loop: while `salmon web` runs, `_request`
  called from a job's own loop runs on the server's loop instead (`run_on_request_loop`), so
  every job shares it. Anything new that touches a client's loop-bound state does the same.
- **Do not add request loops without a bound.** A feature that issues one request per item (per
  torrent, per group, per page) must cap the count or make it opt-in. Issue #432 is the example of
  what goes wrong: one check sends ~99 requests per upload.
- **Retries multiply traffic.** A retried request counts against the user's rate limit too. Only
  retry on errors that are genuinely transient.
- **Never re-send a request that changes state once it may have reached the tracker.** `_request`
  treats a POST as not idempotent: it retries it only when the tracker cannot have acted on it,
  and raises `UnknownOutcomeError` otherwise (#446). Pass `idempotent=True` only for a POST that
  sets a fixed state, and list it with why in `tests/test_rule_idempotent_posts.py`.
- **Never test against a live tracker from code or CI.** Use a local fake server; see
  `tests/test_trackers_session.py` for the pattern. The test suite refuses every non-loopback
  connection (`tests/conftest.py`): a test that misses its fake fails with `NetworkBlockedError`
  naming the address. A test that really needs the network opts out with `@pytest.mark.network`,
  and CI deselects it; never use that for a tracker.
- API-key requests must send no session cookie, and cookie requests no `Authorization` header.
  `_request` enforces this; keep it that way.

## Conventions

Several rules in this file have a test, named next to the rule (`tests/test_rule_*.py`,
`tests/test_interaction_calls.py`, `tests/test_complexity.py`). Each names its rule, and its failure says what to do.

- Python 3.13+, ruff line length 120, async throughout (`anyio`, `aiohttp`, `asyncclick`).
- Config: add new settings to the msgspec schema with a default, validate them in
  `__post_init__`, and document them, commented out with their default, in `config.reference.toml` (add a key to
  `config.default.toml` only if a new user is likely to change it). New
  settings must not change behaviour for existing configs unless that is the point of the change.
  Changes to existing keys follow [ADR 0005](docs/adr/0005-config-compatibility.md): renaming,
  moving or removing a key needs an entry in its old-key table, never a silent change; a changed
  default is a breaking change, with a release-notes line that keeps the old behaviour.
- Every bug fix comes with a test that fails without the fix. How to write a test, the shared helpers and the size
  guideline are in [tests/README.md](tests/README.md).
- **Never change or delete a user's album in place when it may be in `library_dirs`.** Steps that
  retag, rename, sanitize or delete work on the folder `staged_source` (`uploader/staging.py`)
  returns, which is a real copy for `--skip-flac-upload` and for library albums. A new command or
  step that writes into, or removes, a folder checks `cfg.directory.protects(path)` first (#531).
- **Ask the user only through `salmon.interaction`** (`await interaction.confirm(...)`), never asyncclick's
  `prompt`, `confirm` or `edit` or `input()`: a web job would hang on the server's terminal. A test walks `src/salmon`
  and fails on one (`tests/test_interaction_calls.py`). Whether a run answers with defaults (`-yyy`, or `yes_all`
  in the config) is `await interaction.assume_defaults()`; nothing writes `cfg` at runtime
  (`tests/test_rule_cfg_is_read_only.py`).
- Before writing a helper, look for one that already does the job, and use or extend it rather than writing a
  copy. A fix in a torrent's upload (`uploader/upload.py`, `uploader/__init__.py`) usually applies to its
  conversions' (`uploader/conversion_upload.py`) too, and the other way round.
- Put new feature logic in a module of its own, or one that fits it, and call it from the busy modules (any
  over about 1000 lines, such as `uploader/__init__.py` or `trackers/base.py`) rather than growing them. A fix
  stays where the bug is.
- Functions are held to a complexity of 20 (ruff's C901). The few already over it when the rule came in (#927)
  carry `# noqa: C901` and are listed in `tests/test_complexity.py`, each with a small margin: splitting one is
  welcome, as a change of its own. When that test or ruff fails on a PR, split the function rather than adding a
  `# noqa: C901` or raising its entry. Raise it only when the code belongs there, and say why in the PR description.
- Keep PRs to one logical change. Maintainers squash-merge.
- Image hosts that only display for one tracker's members (RED's) must never be usable through a
  setting shared by all trackers. See [ADR 0002](docs/adr/0002-per-tracker-cover-host.md).

## CI, the Docker image and releases

Read [docs/ci-and-releases.md](docs/ci-and-releases.md) before touching the `Dockerfile`, anything under
`.github/`, `.trivyignore` or `src/salmon/data/version.toml`. What holds for every PR:

- **A PR must never change `current` or add a `[[changelog]]` entry** in `version.toml`
  (`tests/test_rule_version_toml.py` fails when one changes without the others). A release is cut by publishing
  a GitHub release, never by bumping versions by hand: its workflow makes that commit, which is what tells every
  user a new version exists.
- Everything CI and the image run is pinned: each `uses:` to a full commit SHA with its version in a comment,
  images by tag and digest, Python packages and the dev tools by `uv.lock`, and uv in CI by the Dockerfile's uv
  image tag (`tests/test_rule_pinned.py`).
- A `pull_request` workflow declares its permissions, none of them `write`, logs in to no registry, and each
  checkout in it sets `persist-credentials: false` (`tests/test_rule_pull_request_workflows.py`). No script
  interpolates a value an outsider writes: it goes through `env:` and is read as `"$VALUE"`
  (`tests/test_rule_workflow_untrusted_input.py`).
- An image change gets a smoke check in `.github/docker-smoke.sh` that fails before it.

## Scratch work

Implementation plans and other throwaway notes go in `docs/plans/`, which is gitignored. Anything
that should outlive the PR belongs in the PR description, the issue, or an ADR in `docs/adr/`.
