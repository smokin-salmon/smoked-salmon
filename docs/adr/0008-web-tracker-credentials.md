# 0008. salmon web edits tracker credentials in config.toml; each job reads them when it starts

- **Status:** Proposed
- **Date:** 2026-11-23
- **Links:** #1034, amends [0004](0004-web-interface.md) sections 1 and 3 and [0005](0005-config-compatibility.md)
  decision 6

## Context

Session cookies expire, and a tracker's API key may be rotated. They are the settings a user changes most often,
and the only way to change them was to edit `config.toml` and restart salmon. For `salmon web` in Docker, that means
restarting the container, which also loses the job history (0004, Consequences).

The config cannot simply be loaded again:

- `cfg` is loaded once, when salmon is imported, and 65 modules hold it by name. Four also derive values from it at
  import, among them `salmon.trackers.tracker_list`, the trackers a run offers.
- Nothing may write `cfg` while salmon runs (`tests/test_rule_cfg_is_read_only.py`, 0004 Consequences): a job must
  not see its settings change under it.
- The tracker clients read their cookie and key from `cfg` when they are built. Since 0004, an account's limiter,
  lock and pool outlive a job.

What those pools hold was checked on master. Each one uses a `DummyCookieJar`, and `_send` passes the cookie or the
`Authorization` header with each request, so a pool keeps no credential. The authkey and passkey are cached on a
client, and each client belongs to one job.

## Decision

### 1. Each job takes the credentials from the file when it starts

- `salmon.trackers.credentials` holds a snapshot of each tracker's session cookie and API key in a contextvar.
  `RedApi`, `OpsApi` and `DICApi` read their cookie and key through it when they are built. Without a snapshot (the
  CLI, anything outside a job), they read `cfg` as loaded. `cfg` itself is never changed.
- `JobManager._run` reads `config.toml`, and checks its tracker tables as a start does (old keys and placeholders
  included), then sets the snapshot along with the job's other context. The rest of the file is not checked again: a
  library or download folder that is offline (a NAS) must not stop jobs that never touch it. It does this when the job starts running, not when it was queued. A running job keeps
  its snapshot until it ends, so an upload never changes credentials part way. The job's tasks inherit the snapshot,
  and `run_on_request_loop` carries it to the request loop.
- A file that cannot be read or decoded, or whose tracker tables would be refused, fails the job before anything is
  sent, with the file and the reason.
  There is no fallback to the credentials salmon started with, which the user can no longer see.
- The trackers stay those salmon started with. A tracker removed from the file fails a job only when that job builds
  its client, saying a restart is needed. A tracker added to the file is ignored until a restart. Only the session
  cookie and API key are read again: any other change to the file still needs a restart.
- Reading the file again prints no config warning: the load gave them once already (`compat.quiet`).
- `_send`, the pools and the limiter are unchanged. Jobs that started with different cookies share one account's
  limiter, lock and pool, and a request still sends the cookie or the key, never both.
- Every value a snapshot reads, in every form a cookie takes (as configured, decoded, as sent), is added to the
  redaction filter before the job runs.

### 2. The web writes one file, through one route

This amends 0004 section 3 ("The web deletes or changes nothing by itself") and 0005 decision 6 ("salmon never
rewrites the user's config file").

- The only file salmon web writes on its own account is `config.toml`, through `POST /api/settings/trackers/{code}`.
  It writes only when the user saves on the Settings page, and that route follows the same token, JSON and `Origin`
  rules as the others.
- A save changes only the `session` and `api_key` of a tracker salmon started with, where the file writes them
  (tomlkit), keeping every comment and the layout. DIC has no API key field. salmon still never rewrites the config by
  itself: old keys, placeholders and defaults are handled as 0005 says, by warnings, and never written back.
- The whole edited text is read as a start reads it before anything is written. A change that would leave a config
  salmon refuses is not written, and its reason is shown.
- The file is written to a temporary file in the same folder, then moved over it with `os.replace`, keeping its mode,
  its owner and the symlink it may be reached through. When the folder takes no new file, the owner cannot be kept
  (salmon not running as the file's owner, nor as root) or the move fails (a config bind-mounted on its own into a
  container), the file is written in place instead: it first grows to the new length, padded with spaces, so a full
  disk fails before the old text is touched, and if writing over it fails all the same the old text is put back. A
  write in place cannot be atomic: if putting the old text back fails too (a copy-on-write filesystem that is full),
  the save says the file may be cut short. A job that starts during a write reads the file before or after it. A
  temporary file that cannot be written for any reason but a permission (a full disk) leaves the config as it was.
- A file salmon may not write is never written, and the page says so. A file with no write bit set counts as one
  even for root, who could write it anyway, as salmon does in the Docker image. `chmod a-w config.toml` is how a user
  turns saving off.
- `GET /api/settings/trackers` never returns a stored value. For each credential it gives whether it is set and,
  for a value of 16 characters or more, its last 4. Each saved value is added to the redaction filter once it is
  written, before the answer or a job could repeat it. A refused save masks its values in its own message only, so
  a value never saved masks nothing after.
- A save takes only what a request header can carry: printable ASCII with no spaces, at most 4096 characters (a
  browser's cookie limit). Anything else is refused before it is written, rather than failing every job on that
  tracker. A blank API key removes the key.
- A request a tracker sends to its login page says, under salmon web, that a new cookie can be pasted on the
  Settings page.
- salmon web serves plain HTTP (0004 section 3). When the page is open over plain HTTP from another machine (not
  HTTPS, not loopback such as an SSH tunnel), it says a save would send the cookie or key unencrypted, and a save
  takes a second, explicit click. Only the browser can tell, before anything is sent. It is not refused: a VPN that
  encrypts (WireGuard, Tailscale) carries plain HTTP safely.
- After a save, the page runs the connection check for that tracker as a job: at most two requests, and only because
  the user saved. Nothing is checked on page load.

## Consequences

- No restart is needed to renew a cookie or rotate a key, from the page or by editing the file by hand.
- `cfg` stays read-only, and 0004's rule stands that a runtime write to it is a bug. What can change while the
  server runs is the snapshot a job reads when it starts.
- Every web job reads the config when it starts. A config whose tracker tables a hand edit broke stops new jobs,
  with the reason, instead of being ignored. A save checks the whole file, so it is refused while a folder the config
  names is offline.
- Each save adds one connection check, at most two requests to that tracker.
- Ruled out: loading `cfg` again or replacing it, adding or removing a tracker without a restart, the page showing
  or editing any other setting, and a fallback to the credentials salmon started with.
