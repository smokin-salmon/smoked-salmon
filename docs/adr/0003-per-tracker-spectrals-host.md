# 0003. Spectrals and description image hosts are chosen per tracker too; spectrals are uploaded once per host

- **Status:** Accepted
- **Date:** 2026-09-30
- **Links:** #535, extends [0002](0002-per-tracker-cover-host.md)

## Context

0002 made the cover host a per-tracker setting and kept spectrals and description images on one
shared host, because salmon uploaded the spectrals once, before the first tracker, and reused the
URLs for every tracker. Users asked to send spectrals and description images to a different host
per tracker as well (#535).

A per-tracker spectrals host changes when the spectral files are needed: a later tracker with its
own host needs them after the first tracker's upload, while they used to be deleted before it. By
default the spectrals folder is `Spectrals/` inside the album folder, so keeping it there would put
it in every torrent made from that folder afterwards.

## Decision

- `[image.red]`, `[image.ops]` and `[image.dic]` also take `specs_uploader` and `image_uploader`,
  overriding the `[image]` setting of the same name for that tracker. One resolver,
  `ImageUploader.host_for(site_code, kind)`, serves the three kinds.
- Which host is allowed where stays data in `HOST_RULES`:
  - A host whose images only display on some trackers (`displays_on`) is allowed only under those
    trackers' sections, for any kind, and never in the shared `[image]` settings.
  - A host with `spectrals_refused` (such as `red` and `ra`) is never a specs host, per tracker
    or shared.
  - `red` anywhere needs `tracker.red.api_key`.
- Spectrals are uploaded lazily, once per host: the first tracker's host before its upload, as
  before, and another host the first time a tracker using it is uploaded to. Trackers sharing a host
  reuse the URLs, and so do their transcodes and lossy master reports.
- The spectral files are deleted as soon as every tracker the run can still reach has its host's
  upload. When the run may need them later, they are moved out of the album folder first, and
  deleted when the run ends, error or not.
- Without per-tracker keys nothing changes: one upload, deleted at the same point as before.
- `image_uploader` per tracker is accepted and resolved, but nothing in `salmon up` uploads
  description images yet. It is for tracker-aware image uploads to come (#548).

## Consequences

- Only hosts every tracker can display may be set in `[image]`; a host limited to some trackers can
  still host that tracker's spectrals if its `HOST_RULES` row does not refuse spectrals.
- Spectrals are not uploaded up front to every configured tracker's host: the user picks the next
  tracker after each upload and may stop early.
- A run with several specs hosts keeps the spectral files for its duration, in a temporary
  directory outside the album.
