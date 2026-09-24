# 0002. Cover hosts are chosen per tracker; a tracker's own image host is opt-in and confined to it

- **Status:** Accepted
- **Date:** 2026-09-22
- **Links:** #428

## Context

#428 added RED's internal image host (`red`). Its images only display for logged-in RED members,
and OPS, which accepts RED-hosted images. DIC cannot display them.

In a multi-tracker run, salmon uploads the cover once and reuses the URL for every tracker, so a
single global `cover_uploader = "red"` would have given DIC uploads a broken cover.

A downstream fork solved this by making `red` the default cover host for RED. That changes
behaviour for every existing config and requires a RED API key users may not have set.

## Decision

- Optional `[image.red]`, `[image.ops]`, `[image.dic]` sections with a `cover_uploader` that
  overrides `[image] cover_uploader` for that tracker.
- The upload loop keeps one cover URL per host: trackers sharing a host reuse the upload, a
  tracker with its own host gets its own, and a failed upload is retried for the next tracker.
- `red` is accepted only as `cover_uploader` under `[image.red]` or `[image.ops]`. It is refused in
  the shared `[image]` settings (which also keeps spectrals off RED's host, as RED's rules
  require) and under `[image.dic]`.
- Using `red` anywhere requires `tracker.red.api_key`, checked when the config loads.
- Opt-in: without an `[image.<tracker>]` section, behaviour is unchanged.

## Consequences

- Any future tracker-run image host is added the same way: map it to the trackers that can display
  it in `_TRACKER_ONLY_HOSTS` (`config/validations.py`).
- Only covers are per tracker. Description images and spectrals are uploaded once and appear on
  every tracker, so they must stay on a host every tracker can display.
- Users who want RED's host must opt in explicitly.
