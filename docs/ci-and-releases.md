# CI, the Docker image and releases

[AGENTS.md](../AGENTS.md) points here: read this before touching the `Dockerfile`, anything under
`.github/`, `.trivyignore` or `src/salmon/data/version.toml`.

## Docker image

A PR that touches the `Dockerfile` builds the image for `linux/amd64` and `linux/arm64`
(`.github/workflows/docker-pr.yml`) and runs `.github/docker-smoke.sh` in it. An image change gets a
smoke check that fails before it. rclone is pinned by `ARG` (version and one SHA-256 per arch, from
the release's `SHA256SUMS`); Dependabot cannot bump it, so bump all three by hand.
`.github/workflows/rclone-check.yml` runs weekly and fails when rclone has a newer release, naming
it and where its `SHA256SUMS` are.

Everything CI and the image run is pinned: each `uses:` to a full commit SHA with its version in a
comment, the `Dockerfile`'s base and uv images by tag and digest, Python packages and the dev tools
(ruff, pytest, basedpyright) by `uv.lock`, and uv itself in CI by the Dockerfile's uv image tag, which each
job reads.
Dependabot (`.github/dependabot.yml`) proposes every update to them as a PR, except the job-local
`registry:3` service in the publishing workflows, whose digest is bumped by hand. Pin a new action or
image the same way, checking that the SHA is the commit its release tag points to
(`tests/test_rule_pinned.py`).

A `pull_request` workflow runs the PR's code: it declares its permissions, none of them `write`, logs
in to no registry, and each checkout in it sets `persist-credentials: false`
(`tests/test_rule_pull_request_workflows.py`). No script interpolates a value an outsider writes (a
title, a body, a commit message, a PR's branch name): it goes through `env:` and is read as
`"$VALUE"` (`tests/test_rule_workflow_untrusted_input.py`).

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
is the changelog users read. A PR must never change `current` or add a `[[changelog]]` entry
(`tests/test_rule_version_toml.py` fails when one changes without the others).
