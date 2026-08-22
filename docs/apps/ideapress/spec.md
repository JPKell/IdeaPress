# IdeaPress — Specification

**Type:** Application · **Import/distribution name:** `ideapress` · **Default port:** 8767 · **Env prefix:** `IDEAPRESS_`
**Status:** Specified, not implemented. Corrected 2026-08-21 by the
[final architecture audit](../../reviews/final_architecture_audit.md) (ADR-0025, ADR-0026).
**Related:** [Workflows](workflows.md) · [API](api.md) · [Data Model](data-model.md) · [Development Plan](development-plan.md) · [Risks](risks.md)

---

## 1. Purpose

Turn an idea into finished content through configurable workflows in which **Python owns the control
flow and models perform bounded tasks**. Every stage output is validated deterministically before it
is committed; a model never decides that the work is done.

IdeaPress must be fully useful on its own, against a plain Ollama installation, and must get better —
without any workflow code changing — when LoadCoach is available.

## 2. Scope

* Projects: a source idea, its brief, its plan, its units and its state.
* Workflows: ordered, configurable stages with gates between them.
* Requirement compilation: author intent → machine-checkable, identified, blocking requirements.
* Generation, validation, audit, revision and commit per unit.
* Content types: article and report at 1.0; the registry is open.
* Inference abstraction with three backends: direct Ollama, LoadCoach, OpenAI-compatible.
* Exports: Markdown, HTML, JSON at 1.0.
* Web UI, CLI, project storage, and a local API.

## 3. Explicit non-goals

* **No benchmarking.** IdeaPress never measures model capability; if it wants evidence, LoadCoach has
  it.
* **No routing algorithms.** Standalone mode uses explicitly configured models per stage. Intelligent
  selection is LoadCoach's job and is optional.
* **Never requires FreeWeight.** Not at install, not at runtime, not for any feature.
* Never requires LoadCoach. It is an optional backend.
* No autonomous agent loop; no unbounded model-directed iteration.
* No publishing to third-party platforms at 1.0 (export to file only).
* No collaborative multi-user editing.
* No execution of model-generated code, ever.

## 4. Responsibilities

| Area | Responsibility |
|---|---|
| Projects | Creation, brief, plan, units, state, history, resumption |
| Workflows | Stage definitions, ordering, gates, retries, bounded revision loops |
| Requirements | Compilation from author material; identity; blocking vs advisory; propagation to every stage |
| Generation | One bounded model task per stage attempt, through the inference port |
| Validation | Deterministic checks first; structural, requirement coverage, reference integrity |
| Audit and critique | Model-assisted review that *reports*; the writer stage repairs |
| Revision | Bounded rounds with a diminishing-returns stop; "leave it alone" is a valid verdict |
| Commit | Atomic write of a validated unit with full provenance |
| Export | Deterministic rendering to the supported formats |
| Backends | One inference port, three adapters, switchable by configuration alone |

## 5. Dependencies

**Suite:** `baseaicore`, `setspec`, `modelrack`, `weightsdb`, `mirrorwall`.
Optional extra: `sweatmeter` (status display only).
**Third party:** `fastapi`, `uvicorn[standard]`, `typer`, `pydantic`, `pydantic-settings`,
`sqlalchemy`, `alembic`, `jinja2`, `httpx` (LoadCoach adapter).
**External services:** an inference backend — Ollama by default, or LoadCoach, or any
OpenAI-compatible endpoint.

**Required at startup:** none. IdeaPress starts, opens projects and exports existing content with no
backend reachable.

## 6. Consumers

Users, via web UI and CLI. IdeaPress exposes a local API for its own UI and for scripting; it is not
designed as a service other applications depend on.

## 7. Public APIs

### 7.1 HTTP (`/api/v1`, detail in [API](api.md))

```text
GET  /health                       GET  /version                  GET  /system/status
GET  /projects                     POST /projects                 GET  /projects/{id}
PUT  /projects/{id}                DELETE /projects/{id}
GET  /projects/{id}/units          POST /projects/{id}/plan
POST /projects/{id}/stages/{stage}/run                            (async: returns a task)
GET  /projects/{id}/tasks/{task_id}                               GET .../stream (SSE)
POST /projects/{id}/tasks/{task_id}/cancel
GET  /projects/{id}/units/{unit_id}                               GET .../history
POST /projects/{id}/units/{unit_id}/revise
GET  /projects/{id}/export                                        POST /projects/{id}/export
GET  /workflows                    GET  /workflows/{id}
GET  /backends                     POST /backends/test
GET  /settings                     PUT  /settings
```

### 7.2 CLI

```text
ideapress serve | health | doctor | version
ideapress config show|validate|init|path      ideapress db upgrade|status|backup|restore
ideapress project create|list|show|delete|import|export
ideapress plan build|show
ideapress stage run|list|status|cancel
ideapress unit list|show|history|revise
ideapress workflow list|show
ideapress backend list|test|switch
ideapress prompts list|show|build
```

## 8. Inputs

The idea and brief; author material (style guide, audience, constraints, source documents); workflow
selection and configuration; per-stage model bindings (standalone) or task-profile mappings
(LoadCoach); prompts; configuration.

## 9. Outputs

Compiled requirements; plans; unit drafts with full attempt history; validation and audit reports;
committed units; exported documents; per-unit provenance (backend, model, prompt version, validation
results); events.

## 10. Data ownership

Owns `ideapress.sqlite3`: projects, briefs, plans, units, requirements, stage_runs, attempts,
validations, audits, revisions, drafts, exports, backend_config, settings. Owns its project artifact
directory. Reads nothing belonging to another application.

## 11. Public contracts

1. **Backend contract.** The `InferenceBackend` port is IdeaPress-internal but stable: switching
   adapters changes configuration only, never workflow code.
2. **Stage contract.** Every stage takes a typed input and returns a typed output plus a validation
   result; no stage returns raw model text to another stage without validation.
3. **Provenance contract.** Every committed unit records backend, model identity, prompt IDs and
   versions, requirement coverage and validation results.
4. **Export contract.** Exports are deterministic: the same committed project exports byte-identically.
5. **LoadCoach contract.** The adapter uses only LoadCoach's documented `/api/v1`, checks version
   compatibility, and degrades explicitly. It sends the rendered prompt as `system` + `prompt`, and
   LoadCoach passes that text to the provider unmodified — which is what keeps the per-attempt
   `prompt_sha256` provenance truthful ([LoadCoach Spec §9](../loadcoach/spec.md)).

## 12. Configuration

```toml
[server]     host = "127.0.0.1"  port = 8767  allow_lan_exposure = false
             allowed_hosts = []     # required when host is not loopback (ADR-0026)
[storage]    database_url = "sqlite:///<data>/ideapress.sqlite3"  auto_migrate = true
             project_dir = "<data>/projects"

[inference]  mode = "ollama"            # ollama | loadcoach | openai_compatible
             fallback_mode = ""         # optional; empty means no fallback
             pin_backend = false        # true = never fall back, fail instead

[inference.ollama]           base_url = "http://127.0.0.1:11434"  timeout_seconds = 300
[inference.loadcoach]        base_url = "http://127.0.0.1:8766"  api_key_env = ""  timeout_seconds = 600
[inference.openai_compatible] base_url = ""  api_key_env = ""  timeout_seconds = 300

# Standalone stage → model bindings (ignored in loadcoach mode unless overridden)
[models.stages]
requirements       = "ollama/qwen3.5:9b-q8_0"
research_synthesis = "ollama/qwen3.5:9b-q8_0"
outline            = "ollama/qwen3.5:9b-q8_0"
draft              = "ollama/gemma4:12b"
repair             = "ollama/qwen3.5:9b-q8_0"
audit_fast         = "ollama/qwen3.5:9b-q8_0"
audit_deep         = "ollama/qwen3.5:9b-q8_0"
fact_check         = "ollama/qwen3.5:9b-q8_0"
critique           = "ollama/qwen3.5:9b-q8_0"
revise             = "ollama/qwen3.5:9b-q8_0"
project_review     = "ollama/qwen3.5:9b-q8_0"

# One key per model-using stage in [Workflows §2](workflows.md), spelled exactly as the stage is.
# A binding for a stage that does not exist, or a model-using stage with no binding, fails startup
# validation naming the stage — the audit found the previous list used `edit` and `audit`, neither of
# which is a stage identifier.

[workflow]   max_revision_rounds = 3   diminishing_returns_threshold = 0.05
             max_attempts_per_stage = 3  audit_escalation_threshold = 0.6
             require_clean_validation_to_commit = true
[providers]  allow_remote = false
[logging]    level = "INFO"  include_content = false
```

Stage → LoadCoach task profile mapping lives in the adapter, in one place
([Workflows §6](workflows.md)), never scattered through workflow code.

## 13. Error behaviour

```text
BACKEND_UNAVAILABLE        VALIDATION_FAILED          PROJECT_NOT_FOUND
BACKEND_VERSION_MISMATCH   REQUIREMENTS_UNMET         UNIT_NOT_FOUND
MODEL_NOT_CONFIGURED       STAGE_PRECONDITION_FAILED  STAGE_ALREADY_RUNNING
PROVIDER_TIMEOUT           REVISION_LIMIT_REACHED     EXPORT_FAILED
CONTEXT_LIMIT_EXCEEDED     CONTENT_REJECTED           SCHEMA_VERSION_UNSUPPORTED
```

Behavioural rules:
* A failed stage **pauses the project at that stage**; committed units are untouched and the stage is
  resumable.
* A stage never commits an output that failed a blocking validation.
* Revision stops at the configured round limit or when improvement falls below the diminishing-returns
  threshold, and records which stop applied.
* `CONTENT_REJECTED` (a model refusing the task) is a distinct outcome from a failure, and is
  surfaced with the model's stated reason.
* Backend unavailable: fall back if configured and not pinned; otherwise fail the stage clearly.

## 14. Security considerations

* Loopback default; LAN exposure requires tokens, acknowledgement and `server.allowed_hosts`; the
  `Host` header is validated on every request before routing. This is the application holding the
  user's private work, and an unauthenticated loopback service is reachable from any page the user
  visits without that check ([ADR-0026](../../adr/0026-local-http-hardening.md)).
* **Model output is never executed**, never used to build a path, never rendered unescaped. Markdown
  is sanitized with an allowlist.
* Project and export paths are containment-checked; project IDs and unit IDs are validated against a
  strict pattern before touching the filesystem.
* Imported project archives are hardened against traversal, symlinks and decompression bombs.
* Prompts and drafts are the user's content: stored locally, never uploaded, never logged at INFO or
  above.
* Remote backends require explicit opt-in and are labelled as egress in the UI, per stage.
* `include_content` logging is off by default.

## 15. Performance considerations

Model time dominates; IdeaPress's own budgets:

| Measure | Target |
|---|---|
| Stage orchestration overhead (excluding inference) | ≤ 50 ms per attempt |
| Validation of a 5 000-word unit | ≤ 200 ms |
| Project load (100 units) | ≤ 300 ms |
| Export of a 100-unit project to Markdown | ≤ 2 s |
| Export of a 100-unit project to HTML | ≤ 5 s |
| Editor page render | ≤ 300 ms |
| Draft autosave round-trip | ≤ 100 ms |

Long documents stream to disk rather than being held in memory more than once.

## 16. Cross-platform considerations

Fully portable — no platform-specific code beyond the shared path handling. Optional telemetry display
degrades per [Cross-Platform Standards](../../standards/cross-platform-standards.md). IdeaPress is the
most likely component to be used on Windows or macOS, which is why it takes no hard dependency on
`sweatmeter`.

## 17. Observability

* Structured logs with `request_id`, `project_id`, `unit_id`, `stage`, `attempt`, `backend`,
  `model_canonical_id`.
* Persisted stage events with SSE replay, so a long stage survives a refresh.
* Health components: `database`, `backend` (naming which one and its reachability), `prompts`.
* Every unit's history shows every attempt, its validations, its audits and what changed.

## 18. Test strategy

| Layer | Coverage |
|---|---|
| Unit | Requirement compilation; every validator; revision-stop logic; diminishing-returns detection; stage state machine; export rendering |
| Contract | Backend port conformance for all three adapters; LoadCoach API version negotiation; SetSpec envelopes |
| Integration | Full workflow against `FakeProvider`; project persistence; resumption after a failed stage; migrations both dialects |
| E2E | Idea → plan → draft → audit → revise → commit → export, over HTTP and CLI |
| Backend-parity | **The same workflow run against all three backends produces the same structure**, with only content differing |
| Failure-path | Backend down mid-stage; LoadCoach version mismatch; validation failure; revision limit; refusal; context overflow; disk full mid-export |
| Security | No execution of model output; sanitized rendering; traversal; archive hardening |
| Performance | Every budget in §15 |
| Live (marked) | Real Ollama and real LoadCoach: one short project end to end |

## 19. Compatibility and versioning

* Application semver; API `v1`; workflows and content types versioned independently.
* A project records the workflow version it was created with; a workflow upgrade never rewrites
  committed units.
* Prompt versions recorded per attempt; changing a prompt does not alter existing units.
* Export format changes are versioned; re-export of an old project is byte-stable for its recorded
  version.

## 20. Acceptance criteria

1. `pip install ideapress && ideapress serve` produces finished content with **only Ollama** present —
   no LoadCoach, no FreeWeight, no configuration beyond stage model bindings.
2. Switching `inference.mode` between `ollama`, `loadcoach` and `openai_compatible` requires **no
   workflow code change** — proven by the backend-parity test.
3. No model output can end a gated stage: a "this is fine, stop" response does not satisfy a gate.
4. Every stage output passes deterministic validation before commit.
5. A failed or cancelled stage leaves committed units intact and is resumable.
6. Every committed unit records backend, model, prompt versions, requirement coverage and validation
   results.
7. LoadCoach unavailable ⇒ configured fallback or a clear error; **never a startup failure**.
8. IdeaPress imports nothing from FreeWeight or LoadCoach (asserted by import-linter).
9. Exports are deterministic and byte-stable for the same committed project.
10. Model output containing scripts, template syntax or path traversal is stored and rendered inert.
11. Full test suite passes with no backend reachable and no network.
12. All IdeaPress gold standards in [Gold Standards §2](../../standards/gold-standards.md) are met.

## 21. Future extensions

* More content types (novel, narrative pack, documentation set) via the existing registry.
* More export formats (PDF, EPUB, DOCX).
* Failure memory — remembering what previously failed for a project so retries avoid it.
* Concept competition — generating several execution approaches and selecting among them.
* Research backends (local document ingestion; opt-in web search).
* Per-project prompt overrides with the same record schema and hashing.
* Publishing integrations (explicitly opt-in, clearly marked as egress).
* Using LoadCoach's reliability data to inform stage-level model hints.
