# Lockfiles

Exact, hash-verified pins for this repository's **own** release pipeline, required by Packaging
and Release Standards §4 and Security Standards §11.

| File | Contents | Used by |
|---|---|---|
| `release.in` / `release.lock` | The build and publish chain (`build`, `hatchling`, `twine`) | Both jobs in `release.yml` |

## What these are not

They do **not** define what a consumer installs. `pip install ideapress` resolves the compatible
ranges in `pyproject.toml`; an application that shipped pinned runtime dependencies would be
un-coinstallable with the rest of the suite. This lock exists so that the artifact a tagged
release publishes is the artifact the TestPyPI dry run proved — without it the two jobs would
each re-resolve `build` and `hatchling` from PyPI and could build with different backends.

`hatchling` is pinned here rather than left to build isolation because both jobs run
`python -m build --no-isolation`, so the backend comes from this lock.

## No `ci.lock` yet

Sibling repositories also carry `requirements/ci.lock`, pinning runtime dependencies plus the
`dev` and `postgres` extras for every CI job. This repository does not have one: `ci.yml`
installs `-e ".[dev]"` and re-resolves on each run, so a new `ruff` or `mypy` release can change
a CI result with no commit to explain it, and `pip-audit` audits today's resolution rather than
what the build used. Closing that gap is tracked work, not something this file claims is done.

## Regenerating

Run after any change to the build chain, and commit the result:

```bash
pip install "pip-tools==7.6.1"
pip-compile --generate-hashes --no-emit-index-url --strip-extras \
    --output-file requirements/release.lock requirements/release.in
```

`uv pip compile` is the sanctioned alternative (Security Standards §11).

Generated with **pip-tools 7.6.1**. Note that the `--no-index` recorded in the lock's own header
comment is pip-tools' rendering of `--no-emit-index-url`, which only suppresses writing the index
URL into the output. Passing a literal `--no-index` to pip-compile 7.6.1 disables the index for
resolution and fails with `No matching distribution found for build`; use the command above.

## Interpreter

Resolved on Python 3.13, matching every other repository in the suite. Every pin's
`requires-python` admits 3.12 — the version `release.yml` builds on — and no pin is
CPython-ABI-specific, so the same lock installs on both.
