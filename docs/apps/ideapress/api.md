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
| `GET /system/status` | Active stage runs, backend mode, optional telemetry snapshot |

## 2. Projects

| Endpoint | Notes |
|---|---|
| `POST /projects` | `{title, content_type, workflow_id, brief, author_material}` → project |
| `GET /projects` | Filter by status and content type; cursor pagination |
| `GET /projects/{id}` | Project, plan summary, unit states, stage history |
| `PUT /projects/{id}` | Update brief, author material or configuration; recompiles requirements on demand, never silently |
| `DELETE /projects/{id}` | Preview-then-confirm; archives to an export before deleting when asked |
| `POST /projects/{id}/plan` | Runs requirement compilation and outline; returns the task |
| `GET /projects/{id}/export` · `POST /projects/{id}/export` | `?format=markdown|html|json`; POST writes to the project directory and returns the artifact |

## 3. Stages and tasks

### `POST /projects/{id}/stages/{stage}/run`

```json
{"units": ["U-03"], "resume": true, "overrides": {"model_hint": null, "max_revision_rounds": 2}}
```

Returns `202` with a task:

```json
{"task_id": "01J9K…", "stage": "draft", "state": "running",
 "units_total": 1, "units_completed": 0, "stream_url": "/api/v1/projects/…/tasks/01J9K…/stream"}
```

| Endpoint | Notes |
|---|---|
| `GET /projects/{id}/tasks/{task_id}` | Task state, per-unit progress, attempts, degradations |
| `GET /projects/{id}/tasks/{task_id}/stream` | SSE: `stage.started`, `unit.started`, `attempt.started`, `token` (when streaming), `validation.completed`, `audit.completed`, `fact_check.completed`, `revision.started`, `unit.committed`, `unit.paused`, `stage.completed`, `stage.failed`. Every frame carries the SetSpec event envelope except `token`, which is bare ([ADR-0025 §3](../../adr/0025-envelope-boundaries.md)) |
| `POST /projects/{id}/tasks/{task_id}/cancel` | Honoured at the next model-call boundary |

Only one stage task runs per project at a time; a second returns 409 `STAGE_ALREADY_RUNNING`.

## 4. Units

| Endpoint | Notes |
|---|---|
| `GET /projects/{id}/units` | Unit list with state, version, requirement coverage, last validation |
| `GET /projects/{id}/units/{unit_id}` | Current content plus full provenance |
| `GET /projects/{id}/units/{unit_id}/history` | Every version with its attempts, validations, audits and critique verdicts |
| `POST /projects/{id}/units/{unit_id}/revise` | Targeted revision with optional instructions; bounded by the same limits |

## 5. Workflows and backends

| Endpoint | Notes |
|---|---|
| `GET /workflows` · `GET /workflows/{id}` | Definitions, stage order, gates, defaults, versions |
| `GET /backends` | Configured backends with mode, reachability, capabilities, and an egress flag for remote ones |
| `POST /backends/test` | Round-trip test against a backend; returns latency, model list and any version mismatch |

## 6. Settings

`GET /settings`, `PUT /settings` — runtime-changeable only; `inference.mode`, stage model bindings and
workflow limits are editable, while bind address, exposure, `server.allowed_hosts`, tokens, database
URL and `providers.allow_remote` are config-only and return 403 naming the key.

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
