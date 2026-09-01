# IdeaPress — upgrading

## The guarantees

* **Application semver.** A major is the only thing that may break you.
* **The HTTP API is `v1`** and additive within it.
* **A workflow upgrade never rewrites committed units.** A project records the workflow version it
  was created with, and keeps it.
* **Prompt versions are recorded per attempt.** Changing a prompt does not alter existing units;
  it changes what the *next* attempt does, and the record says which version produced what.
* **Export formats are versioned.** Re-exporting an old project is byte-stable for its recorded
  version.

## Before you upgrade

```bash
ideapress db backup                       # the database
ideapress project export <id> --to ./     # a portable copy of anything you care about
```

The archive is the one that survives an installation being deleted: it carries the brief, the
requirements, the plan, every committed version and the provenance, and imports into any IdeaPress
of the same schema major.

## Upgrading

```bash
pip install --upgrade ideapress
ideapress db upgrade      # not needed if storage.auto_migrate is true, which is the SQLite default
ideapress doctor
```

On **PostgreSQL**, `auto_migrate` defaults to *off* and you run `db upgrade` yourself: a failed
migration there cannot be rolled back automatically, so it is a decision rather than a side effect
of starting.

## Downgrading

Restore the backup. Migrations are forward-only in practice — a schema written by a newer IdeaPress
may carry columns an older one does not know, and `db restore` from before the upgrade is the
supported path back.

## 0.1.x → 1.0.0

No migration is required and no data changes.

What is new: the LoadCoach backend, the project workspace, the plan editor, the diff view, the
export dialog, portable project archives, and the hardening pass.

Two behaviours to know about if you are coming from 0.1.x:

* **`[models.stages]` is ignored in `loadcoach` mode** unless you set
  `[inference.loadcoach] honour_stage_bindings = true`. In 0.1.x the mode did not exist, so nothing
  changes for an Ollama user.
* **`inference.fallback_mode` now actually falls back.** In 0.1.x it was reported by
  `ideapress backend list` and never applied. If you had set it expecting nothing to happen, you
  will now get a fallback — and a `backend_fallback` degradation on the attempt saying so.

## Checking the version

```bash
ideapress version         # the application and the API version it serves
curl -s localhost:8767/api/v1/version
```
