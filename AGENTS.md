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

`SALMON_CONFIG_DIR` (set to `/config` in the Docker image) selects `$SALMON_CONFIG_DIR/config.toml`
when that file exists. If it is absent, salmon uses the existing platform config when available;
otherwise, it checks the repo-root `config.toml`. `tests/conftest.py` creates the selected file, so
the test run never reads a developer's config.

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
- **Never re-send a request that changes state once it may have reached the tracker.** `_request`
  treats a POST as not idempotent: it retries it only when the tracker cannot have acted on it,
  and raises `UnknownOutcomeError` otherwise (#446). Pass `idempotent=True` only for a POST that
  sets a fixed state.
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
- **Never change or delete a user's album in place when it may be in `library_dirs`.** Steps that
  retag, rename, sanitize or delete work on the folder `staged_source` (`uploader/staging.py`)
  returns, which is a real copy for `--skip-flac-upload` and for library albums. A new command or
  step that writes into, or removes, a folder checks `cfg.directory.protects(path)` first (#531).
- Keep PRs to one logical change. Maintainers squash-merge.
- Image hosts that only display for one tracker's members (RED's) must never be usable through a
  setting shared by all trackers. See [ADR 0002](docs/adr/0002-per-tracker-cover-host.md).

## Docker image

A PR that touches the `Dockerfile` builds the image for `linux/amd64` and `linux/arm64`
(`.github/workflows/docker-pr.yml`) and runs `.github/docker-smoke.sh` in it. An image change gets a
smoke check that fails before it. rclone is pinned by `ARG` (version and one SHA-256 per arch, from
the release's `SHA256SUMS`); Dependabot cannot bump it, so bump all three by hand.
`.github/workflows/rclone-check.yml` runs weekly and fails when rclone has a newer release, naming
it and where its `SHA256SUMS` are.

Everything CI and the image run is pinned: each `uses:` to a full commit SHA with its version in a
comment, the `Dockerfile`'s base and uv images by tag and digest, Python packages by `uv.lock`.
Dependabot (`.github/dependabot.yml`) proposes every update to them as a PR, except the job-local
`registry:3` service in the publishing workflows, whose digest is bumped by hand. Pin a new action or
image the same way, checking that the SHA is the commit its release tag points to.

Every workflow that builds the image scans it with `.github/actions/scan-image` (Trivy): fixable
HIGH and CRITICAL vulnerabilities are listed in the job summary, and a fixable CRITICAL fails the
job. `build-alpha.yml` and `docker-image.yml` build both arches into a registry local to the job,
scan that image, then copy the scanned digest to ghcr.io, so the published image is the one scanned,
never a rebuild. An allowlist entry goes in `.trivyignore` with a `# reason` line above it and an
`exp:YYYY-MM-DD` end date on it; the scan action refuses entries without both.

## Releases

A release is cut by publishing a GitHub release (tag `X.Y.Z`), never by bumping versions by hand.
Publishing it runs `.github/workflows/docker-image.yml`, which:

1. checks out `master` (not the tagged commit), sets `current` in `src/salmon/data/version.toml`,
   adds a `[[changelog]]` entry whose notes are the release's description, sets `version` in
   `pyproject.toml` and refreshes `uv.lock`, without committing yet;
2. builds the Docker image from that tree for both arches and scans it (see Docker image above);
3. commits the three files to `master` as github-actions and moves the tag onto that commit;
4. pushes the scanned image to ghcr.io as `X.Y.Z` and `latest`.

If the scan fails (step 2), nothing has left the job: the GitHub release exists, but `master`, the
tag and ghcr.io are unchanged and no user is told. Fix the image on `master` (a base image or package
bump), then re-run the failed run: it rebuilds from the new `master`. There is no need to delete the
release. If the run fails after step 3, `master` already announces a version that has no image;
re-run it as soon as possible: step 1 sees `current` already at `X.Y.Z` and skips the bump, and the
run builds, scans and pushes again.

The workflow checks out and pushes with the `BUMP_VERSION_TOKEN_ACTION` secret, a token whose
owner can bypass the `master` ruleset. If that token expires or is regenerated (which changes its
value), store the new value with `gh secret set BUMP_VERSION_TOKEN_ACTION`; otherwise the whole
workflow fails at checkout: no version bump, no Docker image. A failed run can be re-run once the
secret is fixed.

`release_notification.py` fetches `version.toml` **from `master` on GitHub** to tell every user a
new version exists, so that bump commit is the release announcement, and the release description
is the changelog users read. A PR must never change `current` or add a `[[changelog]]` entry.

## Scratch work

Implementation plans and other throwaway notes go in `docs/plans/`, which is gitignored. Anything
that should outlive the PR belongs in the PR description, the issue, or an ADR in `docs/adr/`.
