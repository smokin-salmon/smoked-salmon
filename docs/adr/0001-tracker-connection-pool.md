# 0001. Tracker requests share a pool of two kept-alive connections, timed per socket

- **Status:** Accepted
- **Date:** 2026-09-22
- **Links:** #432, #437

## Context

`BaseGazelleApi._request` used to open a new `aiohttp.ClientSession`, and so a new TCP connection
and TLS handshake, for every tracker request. Gathered calls therefore produced bursts of
simultaneous handshakes from one IP, a shape tracker edge protection can treat as scanner traffic
and answer with a temporary IP refusal.

#437 moved to one kept-alive session per API instance. Two problems showed up in review,
measured against a local fake tracker using the real 5-per-10 s rate limiter:

- With `ClientTimeout(total=...)`, the time a request spends waiting for a free pooled connection
  counts against its timeout. At a 1.2 s response time, 5 gathered calls made **6** requests: the
  last expired in the queue, was aborted mid-response and retried, which also dropped the
  kept-alive connection.
- With a single connection, short batches run strictly one after another (5 calls at 1.2 s took
  6.0 s instead of 1.2 s).

Long batches are paced by the rate limiter, not the pool: 15 requests took 21.2 s with a limit of
1, 2 or 5 connections alike, against 15 connections opened by the old code.

## Decision

- One `ClientSession` per tracker API instance, `TCPConnector(limit=2)`, reused for all requests
  and closed with the CLI context.
- Timeouts cover the socket only: `ClientTimeout(total=None, sock_connect=t, sock_read=t)`, so
  waiting for a free connection never counts against a request.
- `DummyCookieJar`, with cookies and headers passed per request, so an API-key request still sends
  no session cookie.
- Retries back off exponentially with jitter instead of a fixed 1 s.

## Consequences

- A command opens at most two connections per tracker instead of one per request.
- Two connections are a compromise: one would be the least visible shape, but made interactive
  batches up to 5x slower. More than two gains nothing for long batches, which the rate limiter
  paces.
- Reducing the number of requests is a separate problem (#432); this decision only changes their
  shape.
- `tests/test_trackers_session.py` pins the pool cap, the cookie isolation and the queued-timeout
  behaviour.
