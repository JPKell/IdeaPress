# Lockfiles

Exact, hash-verified pins for this repository's **own** CI and release pipeline, required by
Packaging and Release Standards §4 and Security Standards §11.

| File | Contents | Used by |
|---|---|---|
| `release.in` / `release.lock` | The build and publish chain (`build`, `hatchling`, `twine`) | Both jobs in `release.yml` |
| `ci.lock` | Runtime dependencies plus the `dev` and `postgres` extras | Every CI job that installs this package |

## What these are not

They do **not** define what a consumer installs. `pip install ideapress` resolves the compatible
ranges in `pyproject.toml`; an application that shipped pinned runtime dependencies would be
un-coinstallable with the rest of the suite. These files exist so that a green build stays green:
without them every CI run re-resolves, and a new `ruff` or `mypy` release can change the result
with no commit to explain it — and `pip-audit` would be auditing today's resolution rather than
what the build actually used.

`hatchling` is pinned in `release.lock` rather than left to build isolation because both
`release.yml` jobs run `python -m build --no-isolation`, so the backend comes from that lock.

## `ci.lock`

`requirements/ci.lock` pins the runtime dependencies plus the `dev` and `postgres` extras, with
hashes, resolved against PyPI (row K2, 2026-09-07 — every suite dependency IdeaPress 1.2 needs,
including `loadledger`, `commissioner` and `cutctx`, was already published). Every CI job that
installs this package now runs

```yaml
      - run: pip install --require-hashes -r requirements/ci.lock
      - run: pip install . --no-deps
```

except the 3.14 early-warning job, which resolves from ranges on purpose. The one trap is the
second line: without `--no-deps`, `pip install .` re-resolves the ranges and the lock stops
meaning anything.

Regenerate after any change to `pyproject.toml`'s dependencies or extras:

```bash
pip install "pip-tools==7.6.1"
pip-compile --extra=dev --extra=postgres --generate-hashes --no-emit-index-url \
    --output-file=requirements/ci.lock --pip-args='--no-cache-dir' --strip-extras pyproject.toml
```

Locally the repository still runs against editable installs of the sibling packages; the lock is
what makes a green CI build mean something.

## Regenerating `release.lock`

Run after any change to the build chain, and commit the result:

```bash
pip install "pip-tools==7.6.1"
pip-compile --generate-hashes --no-emit-index-url --strip-extras \
    --output-file requirements/release.lock requirements/release.in
```

`uv pip compile` is the sanctioned alternative (Security Standards §11).

Generated with **pip-tools 7.6.1**. Note that the `--no-index` recorded in each lock's own header
comment is pip-tools' rendering of `--no-emit-index-url`, which only suppresses writing the index
URL into the output. Passing a literal `--no-index` to pip-compile 7.6.1 disables the index for
resolution and fails with `No matching distribution found for build`; use the commands above.

## Interpreter

Resolved on Python 3.13, matching every other repository in the suite. Every pin's
`requires-python` admits 3.12, and no pin is CPython-ABI-specific, so the same lock installs on
both supported versions.
