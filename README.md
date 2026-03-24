[![Build and Publish Docker Image](https://github.com/tomerh2001/smoked-salmon/actions/workflows/docker-image.yml/badge.svg)](https://github.com/tomerh2001/smoked-salmon/actions/workflows/docker-image.yml) [![Linting](https://github.com/tomerh2001/smoked-salmon/actions/workflows/lint.yml/badge.svg?branch=master)](https://github.com/tomerh2001/smoked-salmon/actions/workflows/lint.yml)

# 🐟 smoked-salmon  

A simple tool to take the work out of uploading on Gazelle-based trackers. It generates spectrals, gathers metadata, allows re-tagging/renaming files, and automates the upload process.

This repository is Tomer's actively maintained fork of `smokin-salmon/smoked-salmon`. Use this README for the fork-specific policy, integration-branch rules, and release/distro differences. Use upstream docs for the inherited base feature set.

## 🔗 Fork Links

- Fork repository: https://github.com/tomerh2001/smoked-salmon
- Upstream repository: https://github.com/smokin-salmon/smoked-salmon
- Fork issues: https://github.com/tomerh2001/smoked-salmon/issues
- Fork releases: https://github.com/tomerh2001/smoked-salmon/releases
- Docker images: `ghcr.io/tomerh2001/smoked-salmon:latest`, `ghcr.io/tomerh2001/smoked-salmon:personal-fork`, and `ghcr.io/tomerh2001/smoked-salmon:alpha`

## 🌟 Features

This section documents the fork-only value on `master`: what this fork currently has that upstream does not.

- Optional AI metadata review workflow from [PR #342](https://github.com/smokin-salmon/smoked-salmon/pull/342).
- Upload CLI automation flags and source-url helpers from [PR #345](https://github.com/smokin-salmon/smoked-salmon/pull/345).
- Bandcamp parsing fixes for catno-prefixed and label-hosted releases from [PR #347](https://github.com/smokin-salmon/smoked-salmon/pull/347).
- `open.qobuz.com` URL handling from [PR #352](https://github.com/smokin-salmon/smoked-salmon/pull/352).
- RED cookie-backed upload hardening from [PR #362](https://github.com/smokin-salmon/smoked-salmon/pull/362).
- Fork-specific release/distribution behavior:
  - release CI/CD on fork `master`
  - rolling Docker tags `personal-fork` and `alpha`
  - immutable releases in the form `0.10.1-personal-fork.<run>`
  - fork images report their own `0.10.1-personal-fork.<run>` runtime version inside Salmon

For the baseline smoked-salmon feature set that this fork inherits from upstream, see the upstream repository and README: https://github.com/smokin-salmon/smoked-salmon

## 🧩 Fork Master Composition

This fork's `master` branch is an integration branch. It is intentionally built from `smokin-salmon/smoked-salmon` `master` plus the in-flight patch sets below so there is one branch that always reflects the combined state I run locally.

It is not meant to be reviewed upstream as one giant PR. The upstream review units are the smaller PRs listed here.

| PR | Status | Included in fork `master` | Summary |
| --- | --- | --- | --- |
| [#342](https://github.com/smokin-salmon/smoked-salmon/pull/342) | Open | Yes | Optional AI metadata review workflow |
| [#345](https://github.com/smokin-salmon/smoked-salmon/pull/345) | Open | Yes | Upload CLI automation flags and source-url helpers |
| [#347](https://github.com/smokin-salmon/smoked-salmon/pull/347) | Open | Yes | Bandcamp parsing fixes for catno-prefixed and label-hosted releases |
| [#352](https://github.com/smokin-salmon/smoked-salmon/pull/352) | Open | Yes | `open.qobuz.com` URL handling |
| [#362](https://github.com/smokin-salmon/smoked-salmon/pull/362) | Open | Yes | RED cookie-backed upload hardening |

Fork-only commits on `master`:

- release CI/CD for the fork `master` branch
- rolling Docker tags `personal-fork` and `alpha`
- immutable fork releases in the form `0.10.1-personal-fork.<run>`
- fork-specific README and integration-branch policy
- fork images report their own `0.10.1-personal-fork.<run>` runtime version inside Salmon

How new work enters fork `master`:

This is the mandatory workflow for any change that lands on this fork. It is the canonical policy for `master`. Do not skip steps, do not change the order, and do not shortcut around it.

Every fork-changing task must follow this exact sequence:

1. Create a new issue on the upstream repository for the bug or feature.
2. Branch from upstream `smokin-salmon/smoked-salmon` `master`, not from this fork's `master`.
3. Implement the fix on that upstream-based branch and open a focused upstream PR.
4. Merge that PR branch into this fork's `master` so the integration branch stays ahead with the combined local state.
5. Let the fork `master` CI/CD publish a new release and refresh the rolling Docker tags.
6. Let local consumers use the new fork release artifacts instead of relying on an editable local checkout.

If you only want the AI work without the rest of the integration branch, use [PR #342](https://github.com/smokin-salmon/smoked-salmon/pull/342).
