# Changelog

All notable changes to `ideapress` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Fixed
- A review-stage output budget exhausted on one unit (the model returning no text at all, twice)
  now **pauses that unit** with the stage and the budget in the reason, and the draft stage
  continues to the remaining units. Before, the failure aborted the whole stage: one
  hard-to-critique unit left every unit after it undrafted (M7 finding 1a).
- `stage run <id> draft --resume` now recovers a unit that a crash or stage failure left
  mid-review (`drafting`/`validating`/`auditing`/`revising`): the unit is reset to `paused` —
  only when the run that owned it is demonstrably gone — and then re-entered. Before, such a unit
  had no legal transition back into the loop and the project was unrecoverable from the CLI
  (M7 finding 1b).

- `ideapress config show` on an invalid `config.toml` now exits 2 with the refusal's one-line
  message, matching `config validate` and `serve`, instead of printing a raw traceback
  (M7 finding 4).
- The requirement grounding evidence — the source document and the **verbatim quote** the
  compiler cited — now appears in the coverage section of all three exporters (Markdown, HTML
  with escaping, JSON as a structured `source` object). It was shown in the live views but
  dropped from the exported artefact, which is where the fabrication-detection mitigation for
  risk T6 matters most (M7 finding 2).

### Changed
- A blocking requirement with no deterministic check is now labelled **"guaranteed by model
  review, not a deterministic check"** everywhere it appears: the commit event, the unit and plan
  pages, `plan show`, `unit show`, and the coverage note of all three exports. This is the interim
  safeguard for M7 finding 3 (M7-20); what actually satisfies such a requirement is unchanged and
  is decided by ADR-0039, which is Proposed and awaiting sign-off.
- The requirement compiler prompt (`stages.requirements.compile` 1.1.0, M7-21) now pushes a
  blocking requirement toward the strongest literal check the material supports
  (`must_contain_all`, or `must_not_contain` for prohibitions) and confines single-word
  `must_contain_any` to genuine alternatives.

### Added
- `workflow.structured_output_tokens` (default 8192, range 1024–131072): the output-token budget
  for the structured stages (requirements, outline, audits, critique, project review), previously
  a module constant. A model that spends more reasoning tokens than the reference machine's can
  now be given room in `config.toml` instead of a code edit (M7 finding 1c).
- `GET`/`PUT /settings`, `GET /workflows/{id}`, `POST /projects/{id}/units/{unit_id}/revise`, and
  the `workflow` and `prompts` command groups — the four endpoints and two groups the specification
  lists that the phases had not yet built.
- Exporters for Markdown, HTML and JSON, byte-identical for the same committed project across
  repeats, locales, timezones and hash seeds. The HTML is a single self-contained file with no
  external reference of any kind, so it opens with no network.
- Export format versioning, recorded on every export and embedded in every rendered document.
- `GET`/`POST /projects/{id}/export`, `GET /export/formats`, and `ideapress export run|formats`.
- The `project_review` stage: cross-unit consistency findings, advisory by design.
- The open content-type registry, with `article` and `report` shipped.
- Review: `audit_fast`, `audit_deep` (escalation only, once per unit per round), `critique` with
  "leave it alone" as a first-class verdict, and `revise` bounded by the round limit or by
  diminishing returns computed from deterministic finding counts. Which stop applied is recorded.
- A revision that increases validation failures is rejected and the previous version kept.
- Migration `0004`: `audit_findings`, `critiques`. The findings, their severity and evidence, and
  what changed between rounds, on the unit page.
- The core loop: draft, validate, repair (bounded, then pause the unit), coverage and an atomic
  commit, with complete provenance on every committed version.
- Migration `0003`: `unit_versions`, `validations`, `coverage`, `exports`.
- The unit page and `ideapress unit list|show|history`, showing content, coverage, validation and
  the attempts that produced it.
- Deterministic validation: all seven families from workflows §4 — structural, length, format,
  content constraints, reference integrity, consistency and safety — with failures classed blocking
  or advisory, and no model involved anywhere.
- Context assembly to a token budget, with the reduction order as data. Requirements and the unit
  specification are never dropped; when they alone exceed the budget the stage fails carrying both
  the required figure and the budget.
- The stage runner: a background thread per stage, the unit state machine as a transition table,
  persisted gap-free stage events, and SSE that replays from `Last-Event-ID` after a disconnect.
- `POST /projects/{id}/plan`, `POST /projects/{id}/stages/{stage}/run`, the task and stream
  endpoints, `GET /workflows`, and the plan page showing each requirement beside its quotation.
- `ideapress plan build|show` and `ideapress stage run|list|status|cancel`.
- Migration `0002`: `requirements`, `units`, `stage_runs`, `attempts`, `stage_events`.
- Requirement compilation: every requirement carries a verbatim quotation from the author material,
  and one that cannot be quoted is refused and shown as refused. Check kinds are a closed set of
  literal-string and numeric comparisons; there is deliberately no pattern check.
- The plan gate: every blocking requirement must be assigned to at least one unit, and an empty
  requirement list or an empty plan does not satisfy it.
- The inference port (`InferenceBackend`, `StageRequest`, `StageResult`, `StageEvent`,
  `BackendHealth`) and three adapters: Ollama over ModelRack, a deterministic offline fake, and an
  OpenAI-compatible one. Switching between them is configuration.
- One generation runs at a time, through one function, and the resident model is unloaded before a
  different one loads (ADR-0038).
- The prompt pack on `setspec.prompts`: versioned JSON records with a hashed manifest.
- `GET /backends`, `POST /backends/test`, a backend page that states where content goes, and
  `ideapress backend list|test|switch`.
- Projects: create, list, open, update, archive, and delete with a preview of exactly what will be
  removed. Slugs are derived from the title and never taken from input, and a project's artifact
  directory is created private and containment-checked.
- Migration `0001` on WeightsDB's runner: `projects`, `sources`, `settings`, `api_tokens`, verified
  against both SQLite and PostgreSQL 16 including the Alembic-to-models parity check.
- `ideapress project create|list|show|archive|delete` and `ideapress db upgrade|status|backup|restore`.
- Project list and detail pages, with the shared CSRF token on every form.
- Application skeleton on the shared packages: typed settings with the documented precedence and
  refusals, structured logging with the suite's correlation fields, `/health`, `/version`,
  `/system/status`, the system page, and the `serve`/`health`/`doctor`/`version`/`config` commands.
- `[execution]` configuration: `max_concurrent_stages` (only 1 is accepted; a higher value is
  refused at startup) and `unload_before_model_switch`.
- A startup check that `[models.stages]`, `StageId` and workflows §2 name the same stages, and
  refuses a binding for a stage that does not exist or a model-using stage with none.

### Fixed
- An export's recorded `sha256` is now the file's own digest, so `sha256sum` on the exported file
  reproduces it.
- A second process opening the database no longer marks a running stage as interrupted. Migration
  `0005` records which process owns a stage run, and only a run whose owner is gone is marked.
- `/api/v1/docs` and `/api/v1/openapi.json` work: response annotations imported only under
  `TYPE_CHECKING` left forward references FastAPI could not resolve, so building the schema raised.
- The CI workflow parses again: an edit had left a duplicate `env:` key on one step, which GitHub
  refuses and PyYAML accepts, so the whole workflow was invalid and no job ran. A test now loads it
  with a loader that refuses duplicate keys the way GitHub does.
- The PostgreSQL CI job now names the server `weightsdb.testing` looks for, passes
  `WEIGHTSDB_POSTGRES_URL` rather than `DATABASE_URL`, and uses the `+psycopg` driver this project
  installs; it previously could not connect at all.
- `pytest` now collects under the bare console script as well as `python -m pytest`
  (`pythonpath = ["."]`), which is the invocation CI runs.
- Coverage measures the importable `ideapress` package rather than the `src/ideapress` path, so a
  non-editable install reports real coverage instead of 0 %.
- The PostgreSQL CI job selects tests by path; it previously selected a marker this repository never
  declares, collecting nothing.
- `ruff` no longer formats `docs/`, which is a byte-identical mirror of the suite documentation.
- Restored the six mirrored `docs/apps/ideapress/` documents; three were missing and three had been
  edited downstream.

### Changed
- `loadcoach` moved out of the `dev` extra into a `loadcoach-contract` extra, so the default
  development and CI environment has no other application installed.

- Widened the `sweatmeter` pin to `>=0.4,<0.5`. SweatMeter's first published release is `0.4.0`
  (`0.3.0` completed its development plan but never reached the index), and it adds the in-process
  NVML GPU backend, selected automatically wherever the optional `pynvml` extra is installed.

### Added
- Repository scaffold generated from the suite's development plan (no functional code yet).
