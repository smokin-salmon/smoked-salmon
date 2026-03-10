# Contributing to smoked-salmon

Thank you for your interest in contributing to smoked-salmon! This guide walks you through the standard contribution workflow.

## Before You Start

- Check the [issue tracker](https://github.com/smokin-salmon/smoked-salmon/issues) for existing issues or feature requests related to your change.
- For non-trivial changes, open an issue first to discuss your approach with the maintainers. This avoids wasted effort if the change doesn't align with the project's direction.
- Small fixes (typos, minor bug fixes) can go straight to a pull request.

## Development Environment Setup

### Prerequisites

- Python 3.11 or later
- [uv](https://github.com/astral-sh/uv) (package manager)
- System dependencies: `sox`, `flac`, `mp3val`, `lame` (see the [README](README.md) for platform-specific instructions)

### Setting Up

1. **Fork the repository** on GitHub by clicking the "Fork" button on the [repository page](https://github.com/smokin-salmon/smoked-salmon).

2. **Clone your fork** locally:

   ```bash
   git clone https://github.com/<your-username>/smoked-salmon.git
   cd smoked-salmon
   ```

3. **Add the upstream remote** (to keep your fork in sync):

   ```bash
   git remote add upstream https://github.com/smokin-salmon/smoked-salmon.git
   ```

4. **Install dependencies**:

   ```bash
   uv sync
   ```

## Making Changes

### 1. Create a Feature Branch

Always work on a new branch, never directly on `master`:

```bash
git checkout master
git pull upstream master
git checkout -b feat/your-feature-name
```

Use a descriptive branch name with a prefix:
- `feat/` for new features (e.g., `feat/add-bandcamp-support`)
- `fix/` for bug fixes (e.g., `fix/spectral-filename-encoding`)
- `docs/` for documentation changes (e.g., `docs/update-install-guide`)
- `refactor/` for code refactoring (e.g., `refactor/simplify-upload-flow`)

### 2. Make Your Changes

- Keep changes focused. One logical change per branch/PR.
- Follow the existing code style (the project uses [ruff](https://github.com/astral-sh/ruff) for linting and formatting).

### 3. Run Lint Checks Locally

Before committing, make sure your code passes the lint checks that run in CI:

```bash
# Linting and auto-fix
uv run ruff check . --fix

# Formatting
uv run ruff format .

# Type checking
uv run basedpyright
```

These same checks run automatically on every pull request, so fixing them locally saves time.

### 4. Commit Your Changes

Write clear commit messages using an **imperative sentence** that describes what the commit does (as if completing the phrase "This commit will ..."):

```
<Imperative verb> <what changed>

<optional longer description>
```

- Start with a capital letter
- Use imperative mood (`Add`, `Fix`, `Improve`, not `Added`, `Fixes`, `Improving`)
- Keep the first line concise (ideally under 72 characters)

**Examples** (from actual project history):

```bash
git commit -m "Add --essential-only option to strip extra files during upload"
git commit -m "Fix spectral filenames with undecodable characters causing database errors"
git commit -m "Improve tracker error handling and checkconf diagnostics"
git commit -m "Handle multi-tracker upload failure gracefully"
git commit -m "Extract Bandcamp catalog numbers"
```

### 5. Push to Your Fork

```bash
git push origin feat/your-feature-name
```

## Submitting a Pull Request

1. Go to your fork on GitHub and click **"Compare & pull request"**.
2. Make sure the base branch is `master` on the upstream repository.
3. Write a clear PR description:
   - **What** the change does
   - **Why** it's needed (link to the related issue if there is one, e.g., "Closes #42")
   - **How** it works (brief summary for non-trivial changes)
4. Submit the pull request.

## The Review Process

After you submit your PR:

1. **CI checks** will run automatically (ruff linting and basedpyright type checking). Make sure they pass.
2. A maintainer will **review your code**. They may:
   - Approve it as-is
   - Request changes (with specific feedback)
   - Ask questions about your approach
3. **Address review feedback** by pushing additional commits to the same branch. No need to open a new PR.
4. Once all feedback is addressed and CI passes, a maintainer will merge your PR.

Don't be discouraged if changes are requested — it's a normal part of the process and helps maintain code quality.

## Keeping Your Branch Up to Date (Rebasing)

If `master` has moved forward while your PR is in review, you may need to rebase:

```bash
git fetch upstream
git rebase upstream/master
```

If there are conflicts, Git will pause and let you resolve them file by file:

```bash
# Edit the conflicted files to resolve conflicts, then:
git add <resolved-file>
git rebase --continue
```

After rebasing, you'll need to force-push your branch:

```bash
git push origin feat/your-feature-name --force-with-lease
```

> Use `--force-with-lease` instead of `--force` — it's safer because it won't overwrite changes you haven't seen.

## Reporting Bugs

Search the [existing issues](https://github.com/smokin-salmon/smoked-salmon/issues) first to avoid duplicates. When filing a bug report, please use the following structure:

### Bug Report Template

**Summary** — A one-line description of the problem.

**Environment**
- smoked-salmon version (e.g., `0.10.0`)
- Install method (e.g., `uv tool install`, Docker, manual)
- OS and Python version
- Tracker (if relevant, e.g., RED / OPS / DIC)
- Command used (e.g., `salmon up ./album -s WEB`)

**Reproduction** — Numbered steps to reproduce the issue.

**Actual behavior** — What actually happened.

**Traceback** — The full error traceback (if applicable). Use a code block:

````
```
Traceback (most recent call last):
  ...
```
````

**Expected behavior** — What you expected to happen instead.

**Notes** — Any additional context, observations, or suggestions for a fix (optional).

## Suggesting Features

Feature requests are welcome! Open an issue describing:

- The **problem** you're trying to solve
- Your **proposed solution**
- Any **alternatives** you've considered

Thank you for contributing!
