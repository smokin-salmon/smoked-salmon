# 0004. The web interface runs jobs in threads, sends every tracker request from one loop, and asks through one interface

- **Status:** Accepted
- **Date:** 2026-10-08
- **Links:** #625, amends [0001](0001-tracker-connection-pool.md)

## Context

chodeus's fork has a working web interface, `salmon web`, written by styx-techno and chodeus: FastAPI
and uvicorn, a Svelte front end, each job in a worker thread with an event loop of its own, prompts
turned into browser questions by patching asyncclick, and a token gate. chodeus has no time to bring it
upstream, so we port it. A long-lived server breaks things the CLI could assume: one command, one event
loop, one user at a terminal.

**Tracker state assumes one loop and one client.** On master the rate limiter is one
`AsyncLimiter(5, 10)` on `BaseGazelleApi`, shared by every tracker. The lock that sends requests that are
not idempotent one at a time, and the pool of 0001, belong to one client instance. A web job builds its
own client, so two jobs give one account two locks and two pools. Measured below.

**The limiter is a leaky bucket.** It lets 5 requests through at once, then one every 2 s: 6 requests
reach the tracker within 2 s, and 9 within 10 s.

**Prompts are calls into asyncclick.** salmon asks at 74 places in 22 modules: 32 `click.confirm` (sync),
28 `click.prompt` (async), 12 `click.edit` (sync), one `input()` (`tagger/pre_data.py`) and one
`prompt_async` (the spectrals viewer's "press enter"). 12 of them sit in 7 sync functions. The fork
rebinds 8 functions at server start (11 assignments: `asyncclick.prompt`, `confirm`, `edit`, `echo`,
`secho`, `view_spectrals`, `flush_stdin`, `handle_spectrals_upload_and_deletion` in three modules, and an
`input` in `pre_data`), dispatching on a contextvar. A later `from asyncclick import confirm`, or a new
module importing a patched function by name, bypasses the bridge without any error.

**`-yyy` is process-wide.** `salmon up -yyy` and `cross-upload` set `cfg.upload.yes_all`, which 23 places
in 8 modules read. In a server, one job's `-yyy` would answer the other jobs' questions.

**The pipeline blocks its loop.** Hashing the torrent (`Torrent.generate()`), the staging copy
(`shutil.copytree`) and every `click.confirm` run on the event loop. On a loop shared by all jobs, one
album's hashing would stop the server and every other job, including a tracker request in flight.

**A server is reachable by more than its user.** In the fork:

- A loopback bind needs no token at all. On a shared seedbox every local user can reach loopback. Any web
  page the user opens can send a POST without a body, which needs no CORS preflight, unless the browser
  blocks public pages from reaching loopback (not all do): `/api/jobs/{id}/cancel` and
  `/api/checkconf?force=true` take none. FastAPI 0.142 reads a body as JSON only when it is sent as JSON,
  which keeps its other routes out of such a page's reach.
- `/api/images/upload` takes any regular file inside the browsable roots, which include
  `dottorrents_dir`, and the catbox uploader does not check that it is an image. A `.torrent` there, whose
  announce URL carries the passkey, can be sent to a public host.
- The dashboard runs the connection check when it loads: two requests per tracker, at most once per 5
  minutes unless forced.

### Evidence: two upload-like jobs on one account

A throwaway harness (not in the repo) ran two jobs through `BaseGazelleApi._request` against a fake
tracker on loopback, in a network namespace with no other interface. Each job sends 11 requests: the
index call that authenticates, 3 searches, 3 group lookups sent together (the dupe check gathers its
searches the same way), the upload POST, a torrent lookup, the edit page and the edit POST. The fake answers a GET in 0.2 s and a POST in
3 s. The 429 runs answer job A's second request at once with 429 and `Retry-After: 6`.

| Run | Requests | Took | Shortest span holding 6 requests | POSTs in flight at once | Connections opened | Job B's requests during A's 6 s wait |
|---|---|---|---|---|---|---|
| master, one loop, a client per job | 22 | 37.0 s | 2.0 s | 2 | 6 | 4 |
| master's limiter, jobs in threads | 13 of 22 | job B failed* | 2.0 s | 1 | 4 | job B failed* |
| the fork's `SharedLimiter` in master's `_request`, jobs in threads | 22 | 43.2 s | 10.0 s | 2 | 8 | 1 |
| the fork as it is, jobs in threads | 22 | 43.2 s | 10.0 s | 2 | 8 | 0 |
| one loop, one limiter, lock and pool per account | 22 | 43.2 s | 10.0 s | 1 | 6 | 0 |
| jobs in threads, every tracker request on one loop (decided below) | 22 | 43.2 s | 9.99 s | 1 | 6 | 0 |

\* `RuntimeError: Task ... got Future ... attached to a different loop`: aiolimiter binds to the first
loop that uses it, and job B failed the first time it had to wait.

Request times at the fake, in seconds (idx index, br search, tg group, t torrent, ed edit page; POSTs as
start-end):

```text
master, one loop
  A: 0.00 idx, 0.21 br, 0.41 br, 4.00 br, 8.00 tg, 10.00 tg, 12.00 tg, 20.01-23.01 UPLOAD, 24.00 t, 26.00 ed, 30.01-33.01 EDIT
  B: 0.00 idx, 0.21 br, 2.01 br, 6.00 br, 14.00 tg, 16.00 tg, 18.00 tg, 22.01-25.01 UPLOAD, 28.00 t, 32.00 ed, 34.01-37.01 EDIT
master's limiter, jobs in threads
  A: 0.00 idx, 0.21 br, 0.42 br, 2.01 br, 4.01 tg, 6.01 tg, 8.01 tg, 10.01-13.01 UPLOAD, 13.01 t, 14.00 ed, 16.01-19.01 EDIT
  B: 0.00 idx, 0.21 br, then RuntimeError
the fork as it is, jobs in threads
  A: 0.00 idx, 0.21 br, 10.02 br, 10.41 br, 20.02 tg, 20.02 tg, 20.22 tg, 20.42-23.42 UPLOAD, 30.02 t, 30.23 ed, 30.43-33.44 EDIT
  B: 0.00 idx, 0.21 br, 0.41 br, 10.01 br, 10.22 tg, 10.22 tg, 20.22 tg, 30.02-33.03 UPLOAD, 33.03 t, 40.03 ed, 40.24-43.24 EDIT
jobs in threads, every tracker request on one loop
  A: 0.00 idx, 0.21 br, 0.41 br, 10.02 br, 10.23 tg, 10.43 tg, 20.22 tg, 20.43-23.43 UPLOAD, 30.03 t, 30.23 ed, 33.03-36.04 EDIT
  B: 0.00 idx, 0.21 br, 10.02 br, 10.23 br, 20.02 tg, 20.02 tg, 20.22 tg, 30.03-33.03 UPLOAD, 33.03 t, 40.03 ed, 40.23-43.24 EDIT

429 runs (A's second request answered 429 at 0.21, Retry-After: 6)
master, one loop
  A: 0.00 idx, 0.21 br(429), 10.00 br, 14.00 br, 16.00 br, 20.00 tg, 22.00 tg, 24.00 tg, 28.00-31.01 UPLOAD, 32.00 t, 34.00 ed, 36.00-39.01 EDIT
  B: 0.00 idx, 0.21 br, 0.41 br, 2.00 br, 4.00 tg, 6.00 tg, 8.00 tg, 12.01-15.01 UPLOAD, 18.00 t, 26.00 ed, 30.00-33.01 EDIT
the fork as it is, jobs in threads
  A: 0.00 idx, 0.21 br(429), 6.22 br, 10.01 br, 10.21 br, 20.01 tg, 20.21 tg, 20.21 tg, 26.22-29.23 UPLOAD, 30.01 t, 30.21 ed, 40.02-43.03 EDIT
  B: 0.00 idx, 0.21 br, 10.01 br, 10.21 br, 16.22 tg, 20.01 tg, 30.01 tg, 30.22-33.22 UPLOAD, 36.23 t, 40.02 ed, 40.22-43.22 EDIT
jobs in threads, every tracker request on one loop
  A: 0.00 idx, 0.21 br(429), 10.01 br, 10.21 br, 20.02 br, 20.22 tg, 20.23 tg, 30.02 tg, 30.23-33.23 UPLOAD, 36.23 t, 40.02 ed, 43.03-46.03 EDIT
  B: 0.00 idx, 0.21 br, 6.22 br, 10.01 br, 10.21 tg, 16.22 tg, 20.02 tg, 26.23-29.23 UPLOAD, 30.02 t, 30.23 ed, 40.02-43.02 EDIT
```

- master, one loop: the two uploads overlap (20.01-23.01 and 22.01-25.01), and job B goes on sending
  through A's wait (0.41, 2.00, 4.00, 6.00).
- The fork shares one budget per tracker and pauses it on a 429, but its POSTs overlap (B's upload
  30.02-33.03, A's edit 30.43-33.44) and each job opens its own pool: 8 connections against 6.
- With every request on one loop, POSTs go one at a time (A's edit waits for B's upload to end at 33.03),
  the account keeps one pool, and B waits out A's 429 (its next request at 6.22). The prototype checked
  that the caller's contextvars arrive with each request.
- The sliding window admits 5 requests per 10 s by entry time. At the tracker, 6 arrived within 9.99 s:
  arrival times jitter by milliseconds.

## Decision

### 1. Jobs run in threads; every tracker request runs on one loop

- Each job runs in a worker thread with an event loop of its own, as in the fork, so a step that blocks
  (hashing, copying, a prompt waiting for its answer) stalls that job only.
- Every tracker request runs on the server's loop. `BaseGazelleApi._request`, called from another loop,
  hands the call over with `asyncio.run_coroutine_threadsafe`, which carries the caller's contextvars (dry
  run, held request messages, the job's output), and waits for the result. In the CLI there is only one
  loop, and nothing is handed over.
- On that loop, each tracker account has one rate limiter, one lock for requests that are not idempotent,
  and one kept-alive pool of two connections. This amends 0001: the pool was per client.
- The limiter is a sliding window, ported from the fork's `SharedLimiter`: at most 5 requests enter in any
  10 s, plus a margin against arrival jitter that slice 1 sets by measurement. Each tracker gets its own
  budget; master's one limiter is shared by RED, OPS and DIC.
- A 429 pauses the account's limiter for the wait it asks for (`_rate_limit_wait`, capped as today), so
  every request to that tracker waits, not only the one that got it. In the CLI too.
- An `UnknownOutcomeError` ends its job with a status of its own, telling the user the request may have
  reached the tracker and to check the site before trying again. The web adds no retry of its own, of a
  request or of a job, and offers none on such a job.
- A job cancelled while one of its requests that are not idempotent is in flight stops once the tracker
  has answered that request: the hand-over does not pass that cancellation on. Any other request is
  cancelled at once.
- At most `[web] max_jobs` jobs run at once (default 2); the others wait in order. One job per album
  folder at a time. A question left unanswered for 30 minutes aborts its job, as in the fork.

### 2. Prompts go through one interface; output is captured per job

- A new `salmon.interaction` module defines what a command can ask: `prompt`, `confirm`, `edit`,
  `show_spectrals`, and `assume_defaults` (what `-yyy` sets). The implementation in use is held in a
  contextvar. The CLI's implementation calls asyncclick as today, so the CLI and its tests do not change.
- Every call site calls the interface instead (`await interaction.confirm(...)`), but the one in
  `setup_config`, which the web never runs. The 6 other sync functions that hold some of them, and their
  callers, become async.
- `-yyy` sets `assume_defaults` for its run only. `cfg.upload.yes_all` stays a config default and is no
  longer written at runtime. What the CLI never skips, even with `-yyy`, the web does not skip either.
- The web's implementation turns each call into a question for the browser and waits for the answer in
  the job's thread. `edit` sends the text and gets it back edited (the CLI opens `$EDITOR`).
  `show_spectrals` publishes the job's spectral images, served by the web app; the CLI's viewer on port
  55110 is not started for a web job. `--dry-run` is an option of the job, set with `dryrun.mode()` in its
  context, as the CLI does.
- `salmon web` replaces `sys.stdout` and `sys.stderr` with wrappers that send what a job writes (echo,
  secho, `err=True`, print) to that job's log, found through a contextvar, and anything else to the real
  streams. Nothing in asyncclick is patched.

### 3. Security

- `salmon web` binds `127.0.0.1:55155` by default.
- A token is always required, on loopback too. It comes from `SALMON_WEB_TOKEN`, else `[web] token`. On
  loopback with neither, salmon makes one at each start and prints a login link carrying it in the URL
  fragment (`#token=...`), which a browser never sends to the server. Any other bind refuses to start
  without a configured token. A configured token has at least 32 characters.
- The browser trades the token for a cookie once (`HttpOnly`, `SameSite=Strict`, `Path=/`, and `Secure`
  over HTTPS); a login link's page then drops the fragment from the address bar and the history
  (`history.replaceState`). Scripts send `Authorization: Bearer`.
- salmon serves plain HTTP and terminates no TLS. On any bind but loopback, the token and the cookie cross
  that network in clear: salmon says so when it starts, and the documentation tells users to reach it
  through a TLS reverse proxy, an SSH tunnel or a VPN unless they trust the network.
- A request other than GET or HEAD must be sent as JSON (`Content-Type: application/json`, `{}` at least)
  and, when the browser sends an `Origin`, come from the server's own origin. The websocket checks its
  `Origin` the same way. GET routes change nothing and send nothing to a tracker.
- The `Host` header must be a loopback name, the bind address, or one of `[web] allowed_hosts` (DNS
  rebinding). CORS is enabled only with `--dev`, for the Vite dev server.
- Paths: the browser can list, and start jobs on, folders inside `download_directory` and `library_dirs`
  only. A path is resolved (`realpath`) before it is checked, a root itself is refused as a job's folder,
  and so is an album folder that is a symlink, as in the fork's `validate_album_dir`.
- The web deletes or changes nothing by itself. Folders change only through the steps the CLI runs, on the
  folder `staged_source` gives (#531), and every delete checks `cfg.directory.protects()`. A library album
  is never changed.
- Image uploads from the browser take image files only, from inside the roots.
- Every event that leaves the server (log lines, questions, job results and errors, websocket messages)
  goes through one redaction filter where it is emitted: `redact_tracker_text` with every secret the
  config holds (tracker sessions and API keys, image host keys, seedbox and torrent client passwords) and
  each account's authkey and passkey once known. Tracebacks go to the server's own stderr, never into a
  job's log: a chained aiohttp error repeats its request URL, and a cross-upload download sends
  `torrent_pass` in the query. No route serves a `.torrent`, the config, or a tracker page.
- The connection check runs when the user asks for it, never on page load.

### 4. Stack and packaging

- The server is aiohttp, which salmon already uses as its tracker client and for the spectrals viewer:
  no new Python dependency, so no `salmon[web]` extra. FastAPI with `uvicorn[standard]` would add 12
  packages to the lock (fastapi, starlette, uvicorn, uvloop, httptools, watchfiles, websockets,
  python-dotenv, pyyaml, click, annotated-doc, opentelemetry-api). `salmon web` imports `aiohttp.web`
  lazily (#368). Request bodies are msgspec structs, as the config is. The fork's routers become aiohttp
  handlers; their checks (paths, the SSRF guard) are kept.
- The front end is the fork's Svelte 5 and Vite app, in `webui/`. Its build is committed in
  `src/salmon/webui/static/`, marked `linguist-generated`, and a CI job builds it again from the committed
  source and lockfile and fails if the result differs. A git install (`uv tool install git+...`) and the
  Docker image then carry the UI without Node; Node is needed only to change the front end.
- CI: Node's version pinned in a file, `actions/setup-node` pinned by SHA, `npm ci --ignore-scripts`,
  `svelte-check`, `tsc`, and the build check.
- Dependabot watches npm in `/webui`, with the same cooldown and grouping as the other ecosystems. A bump
  that changes the build needs a rebuild commit before CI passes.
- Docker: the same image, `EXPOSE 55155`, run as `salmon web --host 0.0.0.0` with `SALMON_WEB_TOKEN`.
  Trivy scans it as today; it holds no `node_modules`.
- The spectrals viewer `salmon up` opens (`salmon/web`, port 55110) stays as it is for the terminal.

### 5. Scope of v1

- In v1: login, jobs (live log, questions, cancel), the folder browser, spectrals (make and view, no
  upload), file checks without trackers, the upload page, with dry run, then convert (transcode, downconvert
  and compress, each the command's own code).
- Later, each its own change: checks against trackers (the dupe check), cross-upload, store search and
  metadata (with the fork's SSRF guard), tag, the description generator, image uploads, the connection
  check.
- The CLI stays the authority: the web calls the same code, and offers nothing the CLI cannot do.

### 6. Tests

- Backend tests in pytest, with aiohttp's test server and client, coroutines run with `anyio.run` as in
  the rest of the suite. Anything that reaches a tracker reaches a local fake (`tests/test_trackers_session.py`,
  the fork's fake Gazelle). The network guard in `tests/conftest.py` stays as it is.
- Tracker safety under the web is tested from two threads against the fake: one budget, one POST in
  flight, one pool, the 429 pause, contextvars carried over, cancellation during a POST.
- Security tests: the auth matrix, the JSON and origin rules, the host check, path confinement (symlinks,
  roots, library folders), and planted secrets redacted from every kind of event.
- Front end: `svelte-check` and `tsc` in CI, and a test that the committed build is served. No browser
  tests in v1.

## Consequences

- The CLI's tracker traffic changes with slice 1, never upward for an account: at most 5 requests in any
  10 s per tracker (up to 9 before), a 429 holds every request to that tracker, and requests that are not
  idempotent go one at a time per account instead of per client. Short batches can take longer: a sixth
  request waits for the window instead of 2 s.
- RED, OPS and DIC each get their own budget; a multi-tracker run no longer shares one.
- A prompt added later goes through `salmon.interaction`, or it hangs a web job on the server's stdin. A
  test fails on any direct call to asyncclick's `prompt`, `confirm` or `edit`, or to `input()`, outside
  the CLI's implementation and `setup_config`.
- Jobs share the process: module-level state the pipeline writes is shared between them.
  `cfg.upload.yes_all` was the one found; a new runtime write to `cfg` is a bug.
- Jobs and their history live in memory. A restart loses them, and an upload cut off by a restart may
  have reached the tracker without the server being able to say so.
- Committing the build puts generated files in diffs and adds a rebuild to every npm bump.
- Ruled out: jobs as tasks on the server's loop (any blocking step would stop the server, and making every
  step non-blocking has no end), a limiter or pool per job or per client, patching asyncclick, and a mode
  without a token.
