# IdeaPress — API

**Base path:** `/api/v1` · **Conventions:** [API and Contract Standards](../../standards/api-and-contract-standards.md)

This API primarily serves IdeaPress's own UI and user scripting. It is versioned and documented to the
same standard as the others, but no other application in the suite depends on it — IdeaPress is a leaf.

---

## 1. System

| Endpoint | Notes |
|---|---|
| `GET /health` | Components: `database`, `backend` (which one, reachable?), `prompts` |
| `GET /version` | Application, API and schema versions. **Never authenticated** ([ADR-0026 §5](../../adr/0026-local-http-hardening.md)) |
| `GET /system/status` | Active stage runs, backend mode, pin |

## 2. Projects

| Endpoint | Notes |
|---|---|
| `POST /projects` | `{title, content_type, workflow_id, brief, author_material}` → project |
| `GET /projects` | `?status`, `?content_type`, `?include_archived`, `?limit` (1–200); newest activity first. `?cursor` is the previous page's `page.next_cursor`; a cursor this endpoint did not issue is `400 VALIDATION_ERROR` naming `cursor`. `?offset` is still accepted |
| `GET /projects/{id}` | The project, plus `plan` (`units`, `requirements`, `blocking` counts; `null` before a plan exists), `units` (§4's unit list), `stages` (the newest 50 stage runs, newest first, each with its state, counts, `error_code`/`error_text`, the `options` it ran with and its `stream_url`) and `running_task_id` (`null` when nothing runs) |
| `PUT /projects/{id}` | Update brief, author material or configuration; recompiles requirements on demand, never silently |
| `DELETE /projects/{id}` | Preview-then-confirm. Without `?confirm=true` nothing is removed and the answer is what would be: `project`, `source_count`, `directory`, `directory_bytes`, and `archive_directory`, where an archive would be written. `?confirm=true&archive=true` first writes the project's archive (`<slug>-<UTC stamp>.ideapress.zip`, which `ideapress project import` reads) into `archive_directory`, `archives/` beside the project directory, and answers it as `archive` (`path`, `size_bytes`); an archive that cannot be written is `500 EXPORT_FAILED` and nothing is deleted |
| `POST /projects/{id}/plan` | Runs requirement compilation and outline; returns the task |
| `GET /projects/{id}/workspace` | `?unit` (the first unit when absent or unknown) and `?compare` (a version number). The workspace page's own view: `project`, `units` (the navigator), `selected_unit_key`, `unit` (§4's unit detail), `pause` (`paused`, `reason`, and `hint`, IdeaPress's remedy for an exhausted output budget only), `coverage_summary` (`total`, `satisfied`, `model_guaranteed`, `unsatisfied`), `diff` (the current version against `compare`: `diff_lines`, `diff_added`, `diff_removed`, `diff_old_version`, `diff_new_version`, `diff_unchanged`, `diff_truncated`, or `unavailable` naming why; `null` when not asked), `backend` (its mode and most recent recorded egress verdict), `project_cost`, `running_task_id` and `research.allowed_hosts` |
| `GET /projects/{id}/research` | The research record. `allowed_hosts` and `allowed_tools` say where a fetch may go before one is started. `notes`: `kind`, `title`, `citation`, `sha256`, `characters` (never the text). `tool_calls`: every call, refused and failed ones included, with `tool`, `status`, `reason`, `detail`, `duration_ms`, `invocation_id` and `egress_decision` (SetSpec `governance.egress_decision`, joined by `request.source_ref`; `null` for a read from `sources/`). The stage itself is `POST …/stages/research/run` |
| `GET /projects/{id}/plan` | The compiled requirements, each with `key`, `text`, `blocking`, `source` and `quote` (the material it rests on), `checks`, `mechanical` (whether a deterministic check settles it) and `units`; and the unit plan in reading order: `key`, `title`, `goal`, `requirements`, `state`, `target_words`. `editable_states` names the unit states a structural edit may touch |
| `POST /projects/{id}/plan/edits` | One edit: `{"operation": "reorder"\|"split"\|"merge"\|"reassign"\|"goal", "unit_keys": […], "requirement_keys": […], "text": "…", "position": n}`. The whole plan is re-checked and the answer is the plan as stored, as `GET` answers it. An edit that would leave a blocking requirement with no unit (`details.unassigned_requirement_keys`), or would renumber a unit holding committed or in-flight text (`details.protected_unit_keys`), is `400 VALIDATION_ERROR` and changes nothing |
| `GET /projects/{id}/export` · `POST /projects/{id}/export` | `?format=markdown|html|json`; POST writes to the project directory and returns the artifact |
| `GET /export/formats` | `formats`: each `format`, its file `extension` and a `description` of what it contains, in the export page's own words |

## 3. Stages and tasks

### `POST /projects/{id}/stages/{stage}/run`

```json
{"units": ["U-03"], "resume": true, "overrides": {"model_hint": null, "max_revision_rounds": 2}}
```

`overrides` apply to that run alone, are recorded on it, and are checked before it starts (row
WP5; until then every key was recorded and none applied):

| Key | Stages | What it does |
|---|---|---|
| `model_hint` | `draft`, `revise`, `project_review` | The model every call of the run uses, over each stage's `[models.stages]` binding (a hint to a routing backend, ADR-0040) |
| `max_revision_rounds` | `draft`, `revise` | The review loop's round limit for the run, within `workflow.max_revision_rounds`'s own bounds (0–100) |
| `instructions` | `revise` | The author's words for the reviser, at most 4 000 characters |

A key the stage does not read, or a value outside those bounds, is `400 VALIDATION_ERROR` naming
`overrides.<key>`, and nothing starts. `null` is no override.

Returns `202` with a task:

```json
{"task_id": "01J9K…", "stage": "draft", "state": "running",
 "units_total": 1, "units_completed": 0, "stream_url": "/api/v1/projects/…/tasks/01J9K…/stream"}
```

| Endpoint | Notes |
|---|---|
| `GET /projects/{id}/tasks/{task_id}` | Task state, per-unit progress, attempts, degradations |
| `GET /projects/{id}/tasks/{task_id}/stream` | SSE: `stage.started`, `unit.started`, `attempt.started`, `token` (when streaming), `validation.completed`, `audit.completed`, `fact_check.completed`, `revision.started`, `unit.committed`, `unit.unchanged`, `unit.skipped`, `unit.paused`, `stage.completed`, `stage.failed`. Every frame carries the SetSpec event envelope except `token`, which is bare ([ADR-0025 §3](../../adr/0025-envelope-boundaries.md)) |
| `POST /projects/{id}/tasks/{task_id}/cancel` | Honoured at the next model-call boundary |

Only one stage task runs per project at a time; a second returns 409 `STAGE_ALREADY_RUNNING`.

## 4. Units

| Endpoint | Notes |
|---|---|
| `GET /projects/{id}/units` | `units`, in reading order: `unit_key`, `ordinal`, `title`, `goal`, `state`, `paused_reason`, `requirement_keys`, `version`, `word_count`, `content_hash`, `coverage` (`satisfied` and `total` over the current version; `null` before one exists) and `last_validation` (the newest attempt that recorded checks: `attempt_id`, `stage`, `at`, `passed` — no blocking check failed — `failures`, `blocking_failures`; `null` before any) |
| `GET /projects/{id}/units/{unit_id}` | Current content plus full provenance |
| `GET /projects/{id}/units/{unit_id}/history` | `versions`, newest first: `version`, `committed`, `committed_at`, `content_hash`, `word_count`, `created_at`, `coverage`, and — from the stage run that produced it — `stage_run_id`, `attempts` (oldest first, each with its `attempt_id`), `validations` (each with its `attempt_id`), `findings` (with their `round`) and `critiques` (`verdict`, `rationale`, `improvement_delta`, `stop_reason`). A version an archive import brought back has no run and empty lists; the attempts of a run that committed nothing are on the unit detail |
| `POST /projects/{id}/units/{unit_id}/revise` | `{"instructions": "…"}` (optional) → `202` with a `revise` task. The unit must be `committed`, or `paused` with a committed version (data model §3's two `→ revising` arrows); otherwise `409 STAGE_PRECONDITION_FAILED`. The instructions reach the reviser as one finding, against the committed text, in a round that counts against `max_revision_rounds`. The result commits as a **new version**. The unit pauses with its version kept when that round raises validation failures. A review that changes nothing leaves the version current (`unit.unchanged`). The same run is `POST …/stages/revise/run` with `overrides.instructions` |

## 5. Workflows and backends

| Endpoint | Notes |
|---|---|
| `GET /workflows` · `GET /workflows/{id}` | Definitions, stage order, gates, defaults, versions |
| `GET /backends` | Configured backends with mode, reachability, capabilities, and an egress flag for remote ones |
| `POST /backends/test` | Round-trip test against a backend; returns latency, model list and any version mismatch |

## 6. Settings

`GET /settings`, `PUT /settings` — runtime-changeable only, in the document LoadCoach and
PromptCadence answer ([ADR-0100](../../adr/0100-promptcadences-runtime-changeable-set-is-five-tuning-numbers.md))
and WeightRoomGym's Settings page reads ([ADR-0127](../../adr/0127-every-application-publishes-its-settings-schema-and-weightroom-generates-the-form.md)
rule 4). Row WI1 brought IdeaPress to that shape.

* **Runtime-changeable:** the stage model bindings (`models.stages.<stage>`, one per model-using
  stage) and the workflow limits (`workflow.max_revision_rounds`,
  `workflow.diminishing_returns_threshold`, `workflow.max_attempts_per_stage`,
  `workflow.audit_escalation_threshold`, `workflow.require_clean_validation_to_commit`,
  `workflow.context_budget_tokens`) — each read by a stage and by nothing else. `inference.mode`
  and `logging.level` are not: the backend is built and logging configured once per process, so
  they belong to `config.toml` and a restart (ADR-0100 rule 1).
* **`GET`** answers `settings` (key → effective value), `definitions` and `config_only` (the keys
  refused by name). Each definition carries `type`, `description`, `minimum`, `maximum`,
  `configured`, `stored` (the row's value, or `null`), `source` (`database` when a row decides the
  value, else `configuration`), `shadowed_by` (the environment variable beating a stored row, or
  `null`) and `applies`.
* **`PUT`** takes a flat object — `{"workflow.max_revision_rounds": 2}` — stores each value as a
  row once its own field has validated it, and answers the same document. **`null` removes the
  key's row**, handing the key back to configuration. Bind address, exposure,
  `server.allowed_hosts`, tokens, database URL and `providers.allow_remote` are config-only and
  return `403 FORBIDDEN` naming the key; an unknown key or a refused value is
  `400 VALIDATION_ERROR` naming it, as in LoadCoach and PromptCadence; a request naming any
  refused key writes nothing.
* **Precedence** is [configuration standards §7](../../standards/configuration-standards.md)'s:
  `defaults → file → database → env → CLI`. A stored row is ignored while
  `IDEAPRESS_<SECTION>__<FIELD>` (for a binding, `IDEAPRESS_MODELS__STAGES__<STAGE>`) pins its key;
  the row is kept, shown under `stored` with `shadowed_by`, and decides the value once the variable
  is unset.
* **When a stored value takes effect:** as the next stage starts (`applies: "next_stage"`), in
  whichever process starts it — the served one or the `ideapress` CLI. Nothing applies it sooner.
  One edge: the settings a process's stages read are shared, so a stage started on one project
  refreshes them under a stage already running on another, which may then see the change from its
  next unit or model call.

## 7. Errors

Standard envelope, with the codes in the [spec §13](spec.md). Presentation rules:

* `VALIDATION_FAILED` lists every failing check with its class (blocking/advisory) and the unit.
* `REQUIREMENTS_UNMET` lists the requirement IDs and why coverage failed.
* `BACKEND_UNAVAILABLE` names the backend, its URL and whether a fallback was configured.
* `BACKEND_VERSION_MISMATCH` names both versions.
* `CONTENT_REJECTED` includes the model's stated reason verbatim and is distinguished from a failure.
* `REVISION_LIMIT_REACHED` reports the rounds used and the stop reason.

## 8. Authentication

Loopback with no tokens: open. Otherwise bearer tokens with `read` / `write` / `admin` scopes. This is
the application most likely to hold sensitive personal content, so LAN exposure carries the same
refusal-by-default behaviour as the others, and the UI states plainly when a remote backend is
configured.

## 9. Streaming

SSE per the suite conventions, with `Last-Event-ID` replay. A long drafting stage therefore survives a
browser refresh: the client reconnects and replays from the persisted stage events, which is the same
mechanism FreeWeight and LoadCoach use.

The stream handler is `async def` and the event store is synchronous, so every read into it is
dispatched to the worker threadpool by MirrorWall's `sse_response`; no SSE handler issues a query on
the event loop ([ADR-0003 §6–8](../../adr/0003-sync-vs-async-strategy.md)).
