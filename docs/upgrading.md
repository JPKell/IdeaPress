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

## 1.1.0 → 1.2.0

No migration data changes — two new tables are added (`ledger_*`, `egress_decisions`, migrations
`0007`/`0008`), all empty until IdeaPress's first attempt after the upgrade.

What is new: per-unit and per-project cost on the workspace page (`[pricing]`, `[budget]`), and the
egress badge is now backed by a durable, queryable decision instead of a computed flag.

One behaviour to know about if you run a remote backend (`loadcoach` or `openai_compatible` with
`providers.allow_remote = true`):

* **A remote backend now needs a declared ceiling, or every attempt against it is recorded as
  denied.** Set `inference.loadcoach.max_data_classification` or
  `inference.openai_compatible.max_data_classification` to `"public"`, `"internal"` or
  `"confidential"` — whichever this installation's `inference.data_classification` may reach that
  backend. **Nothing about what actually runs changes**: Commissioner records a verdict and enforces
  nothing, and IdeaPress's own `providers.allow_remote` gate is exactly what it was. What changes is
  the record: before this row, a remote call left no queryable trace beyond the workspace badge's
  current-moment "leaves this machine"; from this row, every attempt against an unceilinged remote
  backend is a durable `denied` row (fail closed, never assumed public), and the workspace badge
  reads that record rather than recomputing the flag.

## 1.2.0 → 1.3.0

Migration `0009` adds `attempts.cache_write_tokens` and `attempts.cache_read_tokens`. Existing
rows stay `NULL` — a build that never asked observed nothing, never a fabricated zero.

What is new: a committed OpenAPI snapshot (`docs/openapi.json`), and a budget on the
`project_review` prompt (`workflow.project_review_context_budget_tokens`, default 24000) so a
project with many committed units no longer sends every one of them unbounded — units drop from
the end of reading order first, keeping the earliest (where terms and facts are established)
intact for as long as the budget allows.

Two behaviours to know about:

* **A token count LoadCoach could not report is no longer recorded as zero.** All four billable
  classes on `TokenUsage` are now `int | None`; an unreported class renders as an em dash instead
  of a fabricated `0`, and a fully reported, fully priced attempt now renders a bare total instead
  of "at least" — the floor qualifier before this release was an artifact of this application
  carrying no cache classes at all, not a fact about any call.
* **The dependency floor rises to `loadledger[sql] >= 0.3, < 0.4`.** `services/pricing.py` is now
  a thin edge over `loadledger.pricing`; `[pricing] file` and the configuration error it raises on
  a broken path are unchanged, and every existing pricing test passes unchanged.

## 1.3.0 → 1.3.2

No migration and no data changes across either patch.

* **1.3.1 widened the `modelrack` floor to `>=0.7,<0.8`** so the four applications can be
  installed into one environment again (packaging standards §4): `freeweight 1.1.0` and
  `loadcoach 1.1.3` floor it at 0.7, and `pip install freeweight loadcoach ideapress promptcadence`
  refused to resolve with 1.3.0. IdeaPress's providers are unchanged in shape across the range;
  nothing about how a project runs changes.
* **1.3.2 raised the `mirrorwall` floor to `>=0.2.2,<0.3`** — a dependency-resolution fix only:
  the two earlier releases pin `setspec<0.5`, below this application's own floor, so the declared
  lowest range could never resolve. Nothing behaves differently for an operator.

## 1.3.x → 1.4.0

One migration (`0010`, a new `tool_call_records` table) and **no change at all** unless you
configure and run the new `research` stage.

* **Nothing runs it implicitly.** `research` is Optional (workflows §2 row 2) and is started by
  hand: `ideapress stage run <project> research`. A project that never runs it has no `sources`
  rows, drafts exactly what 1.3 drafted, and exports exactly what 1.3 exported.
* **A fresh installation fetches nothing.** `[research] allowed_hosts` defaults to empty, and an
  empty list means `http_fetch` is **not registered** — not "loopback only", which is ToolYard's
  own reading of an empty list and deliberately not this application's. Until you name a host, a
  URL in a brief produces a recorded refusal, never a request.
* **Running the stage changes three things downstream, on purpose.** Research notes enter the
  draft and repair context (and are the first section dropped when the context budget binds);
  `fact_check` gains documents to check claims against, so it starts applying to projects it
  previously skipped for want of a source; and an export's grounding statement reports that
  sources existed. If you do not want any of that for a project, do not run the stage for it.
* **Declare a ceiling before you name a remote host.** A remote fetch target with no
  `[research] max_data_classification` is *denied* — fail closed, the same rule
  `[inference.loadcoach] max_data_classification` has followed since 1.2. The denial is a
  recorded `egress_decisions` row and a `tool_call_records` row; the stage completes and writes no
  note. A loopback host carries no ceiling and is approved.
* **Where to put local documents.** `<project directory>/sources/`, and only there. It is the one
  path a tool call may read; an export sitting beside it in the project directory is outside
  containment and is refused.

```toml
[research]
allowed_hosts = ["docs.example.com"]
max_data_classification = "public"
```

## Checking the version

```bash
ideapress version         # the application and the API version it serves
curl -s localhost:8767/api/v1/version
```
