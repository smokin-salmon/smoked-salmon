# Architecture Decision Records

Each file records one decision: what was decided, when, and why. They are not documentation of
the current code, so they do not go stale: a record is never rewritten, only marked
**Superseded by NNNN** when a later decision replaces it.

Read the relevant records before proposing a change they rule out. If you still think a decision
is wrong, open an issue that addresses its reasoning.

Write one only for a decision someone would otherwise re-open: most changes need nothing more
than a good PR description.

| # | Decision | Status |
|---|---|---|
| [0001](0001-tracker-connection-pool.md) | Tracker requests share a pool of two kept-alive connections, timed per socket | Accepted |
| [0002](0002-per-tracker-cover-host.md) | Cover hosts are chosen per tracker; a tracker's own image host is opt-in and confined to it | Accepted |

## Template

```markdown
# NNNN. Title

- **Status:** Accepted
- **Date:** YYYY-MM-DD
- **Links:** #issue, #pr

## Context
What forced a decision, with the evidence.

## Decision
What we do.

## Consequences
What follows, including what we gave up and what this rules out.
```
