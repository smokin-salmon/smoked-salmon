# Writing a test

What to know before you add a test to smoked-salmon. [AGENTS.md](../AGENTS.md) has the rules for the code itself.

## Running tests

```bash
uv run pytest -n auto                      # the whole suite, one worker per CPU, as in CI
uv run pytest tests/test_<area>_*.py       # the tests of one area while you iterate
```

AGENTS.md (Commands) has the longer command that also picks up the other files importing a package. Leave out
`-n auto` for a file or two: starting the workers costs more than it saves.

## Look for a helper first

Shared helpers are in `tests/helpers/`, one module per area. Look there before writing one of your own. The ones a
new test reaches for first:

- `uploader_dry_run`: `fake_upload_world` and `_run_up` run an upload end to end against a fake tracker.
- `cross_upload`: `FakeTracker` and `cross_upload_world`, a fake tracker with scripted outcomes and the image hosts
  and seedbox around it.
- `servers`: the asyncio server fix `tests/conftest.py` installs (see its docstring before you write a local server).
- `shipped_config`: the shipped config made loadable for a test.
- `webui_*`: the `salmon web` server, its jobs, pages, interaction and upload flow.
- `album_files`: `write_flac(path, bits=, rate=, **tags)` writes a FLAC with no audio, `album(folder, tracks=, tags=,
  cover=)` a folder of them. New tests use these; older modules keep their own copies and move to them only when a
  PR touches them anyway.

## Rules a test keeps

- **Loopback only.** `tests/conftest.py` refuses every other connection with `NetworkBlockedError`. A tracker is
  always a local fake, never a live one. A test that really needs the network is marked `@pytest.mark.network` (CI
  deselects it); never for a tracker.
- **No `async def test_`.** The project does not mark or configure async tests, so such a test does not run
  properly. Wrap the coroutine: `anyio.run(partial(fn, ...))`.
- **At most 5 requests per fake account in a test**, unless the limiter is patched. The rate limiter really waits:
  a 6th request inside 10.5 s waits for the window, and the test runs about 10 s slower.
- **A warning fails the run** (`max_warnings = 0`). Fix the code that warns.
- **`cfg` changes only through `monkeypatch.setattr`**, so it is undone after the test.

## Show that a test fails without its fix

Every bug fix comes with a test that fails without the fix. Prove it: switch the fix off, run the test, and watch it
fail for the right reason (not an import error or a typo). To set work aside, prefer a temporary commit to
`git stash`: the stash is shared between worktrees.

## How many tests

One end-to-end test per behaviour. Its variants are parametrised cases on the function that decides, not more
end-to-end runs. When the tests are more than about three times the size of the change, say why in the PR
description.

## Traps

- asyncclick, not click: `import asyncclick as click`. `cmd.main(...)` is a coroutine; run a command with
  `cmd(args=[...])`.
- anyio task groups wrap an exception raised in the body in an `ExceptionGroup`, so an `except click.Abort` around
  one misses it. `fetch_existing_group_candidates_in_background` (`uploader/dupe_checker.py`) shows the unwrap.
- Swapping a file back (`git checkout`, or a mutation check) within the same second keeps its mtime and its size, so
  Python reuses the stale `.pyc`. After restoring, `touch` the file and delete `__pycache__`.
- `OpsApi` overrides `report_lossy_master`: patching `BaseGazelleApi` alone misses OPS.
