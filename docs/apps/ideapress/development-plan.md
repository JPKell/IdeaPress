# IdeaPress — Development Plan

**Sequence position:** third application. Phases 1–6 depend only on BaseAiCore, SetSpec (including
`setspec.prompts`), ModelRack, WeightsDB and MirrorWall — all of which exist once LoadCoach P4 has
published its extractions, so IdeaPress can start **in parallel with LoadCoach P5+**. Only Phase 7 needs a released LoadCoach 1.0 to build against — a **build-time**
prerequisite for the optional adapter, never a runtime dependency of the product.
**Milestones:** M7 (beta) at P6 · **M8 (IdeaPress 1.0) at P9**.

The ordering is deliberate: standalone mode is built first and completely, so that "IdeaPress works
without LoadCoach" is a demonstrated property from the beginning rather than a claim tested at the end.

---

## Phase 1 — Skeleton, storage, projects

**Goal:** a project can be created, listed and opened in the UI and CLI, with a real migrated database.

**Prerequisites:** `baseaicore`, `setspec`, `weightsdb`, `mirrorwall`.

**Work**
* Repository skeleton; settings with precedence and refusals; logging; request IDs; health/version;
  CLI skeleton — all on the shared packages from day one (IdeaPress is the first application that
  never writes its own plumbing).
* Models and migration `0001`: `projects`, `sources`, `settings`, `api_tokens`.
* Project service: create, list, open, update brief/author material, archive, delete with preview.
* UI on MirrorWall: project list, project detail shell.

**Files/subsystems**
```text
src/ideapress/{__main__,__about__,config,bootstrap}.py
src/ideapress/{domain/project,services/projects,infrastructure/db/**}.py
src/ideapress/web/{app,routes/{system,projects}}.py  src/ideapress/cli/{main,commands/{project,config,db}}.py
tests/integration/test_migrations.py  tests/e2e/test_project_crud.py
```

**Tests**
* Config precedence and refusals; health shape; migrations on both dialects.
* Project slug uniqueness and path safety (a title with `../` produces a safe slug).
* Delete previews exactly what it removes; archive-before-delete works.

**Acceptance criteria**
1. `ideapress serve` starts with zero configuration and no backend reachable.
2. A project can be created and reopened across a restart.
3. Import-linter confirms no import of `freeweight` or `loadcoach`.

**Known risks:** none material.
**Likely failure modes:** a project title producing an unsafe slug; a data directory created with
permissive modes.
**Gold standards:** shared packages from the start; no plumbing duplication. **Deferred:** everything model-related.

---

## Phase 2 — The inference port and the Ollama backend

**Goal:** a trivial "hello" stage runs a real model through the port, and the same stage runs against
`FakeProvider` in tests.

**Prerequisites:** P1; `modelrack>=0.5,<0.6`; `setspec>=0.4,<0.5` (carrying `setspec.prompts`, from
LoadCoach P4 — IdeaPress never writes prompt machinery,
ADR-0028).

**Work**
* `InferenceBackend` protocol, `StageRequest`, `StageResult`, `StageEvent`, `BackendHealth`.
* `OllamaBackend` over ModelRack; stage → model bindings from configuration; capability reporting.
* `FakeBackend` for tests (wrapping ModelRack's `FakeProvider`).
* Backend health, `GET /backends`, `POST /backends/test`, `ideapress backend list|test`.
* Prompt records and the `prompts` CLI, on `setspec.prompts` — the loader, renderer and hashing come
  from the package; IdeaPress supplies only its own pack.

**Files/subsystems**
```text
src/ideapress/domain/inference.py  src/ideapress/infrastructure/backends/{ollama,fake}.py
src/ideapress/services/{backends,prompts}.py  src/ideapress/prompts/**
tests/contract/test_backend_conformance.py  tests/unit/test_prompt_pack.py
```

**Tests**
* Backend conformance suite (run against every adapter as they arrive).
* Missing stage binding ⇒ `MODEL_NOT_CONFIGURED` naming the stage and the setting.
* Backend unreachable ⇒ health degraded, `POST /backends/test` reports it, application still runs.
* Prompt pack: parses, variables declared, renders, manifest current, no inline prompts in Python.

**Acceptance criteria**
1. A "hello" stage produces real text through Ollama and identical text through the fake, given the
   same seed.
2. Workflow-facing code imports `modelrack` nowhere — only the adapter does (asserted).
3. Stage identifiers in `[models.stages]`, in the port's `StageId` and in
   Workflows §2 are the same set; a binding for a non-existent stage, or a model-using
   stage with no binding, fails startup validation naming the stage.

**Known risks:** the port shaped around Ollama. Mitigated by writing the OpenAI-compatible adapter in
P6, before 1.0.
**Likely failure modes:** a `StageResult` that cannot represent a structured-output response; the
adapter leaking `modelrack` types into workflow-facing code.
**Gold standards:** one port; adapters isolated.
**Deferred:** real stages.

---

## Phase 3 — Requirements, plan and the first real workflow

**Goal:** an idea becomes compiled requirements and a unit plan, visible in the UI, with the
compilation gated deterministically.

**Prerequisites:** P2.

**Work**
* Tables: `requirements`, `units`, `stage_runs`, `attempts`, `stage_events`.
* Stages: `requirements` (compile), `outline` (plan), with their prompt records.
* Deterministic gates: every requirement has an ID and a checkable statement; every blocking
  requirement is assigned to at least one unit.
* Stage runner with the state machine, persisted events and SSE.
* `POST /projects/{id}/plan`; plan UI; `ideapress plan build|show`.

**Files/subsystems**
```text
src/ideapress/domain/{requirements,plan,stage_state}.py
src/ideapress/services/{stages,events}.py  src/ideapress/web/routes/stages.py
tests/unit/{test_requirement_compilation,test_plan_gates}.py
tests/integration/test_plan_stage.py
```

**Tests**
* Requirement compiler does not fabricate: benign author material yields no invented requirement
  (a fixture-based test with an assertion on requirement provenance).
* A blocking requirement assigned to no unit fails the plan gate with the requirement named.
* Malformed model output for a JSON stage retries, then fails cleanly — never commits.
* Stage events gap-free; SSE replay after a disconnect.

**Acceptance criteria**
1. An idea plus a brief yields identified requirements and an ordered unit plan, visible in the UI.
2. A model that returns "looks good, no requirements needed" does **not** satisfy the gate.

**Known risks:** requirement compilation inventing constraints. Mitigated by the anti-fabrication test
and by advisory/blocking separation.
**Likely failure modes:** a requirement with no checkable statement passing the gate; a plan that
leaves a blocking requirement unassigned; malformed JSON committed rather than retried.
**Gold standards:** Python owns progression; deterministic gates.
**Deferred:** drafting.

---

## Phase 4 — Draft, validate, repair, commit

**Goal:** a unit is drafted, deterministically validated, repaired if necessary, and committed with
full provenance — the core loop, with no model-assisted judgement anywhere in it yet.

**Prerequisites:** P3.

**Work**
* Tables: `unit_versions`, `validations`, `coverage`, `exports`.
* Stages: `draft`, `validate` (no model), `repair`, `coverage` (no model), `commit` (no model).
* Deterministic validators: structural, length, format, content constraints, reference integrity,
  consistency, safety.
* Context assembly with the documented budget and reduction order.
* Bounded repair loop; unit pausing when attempts are exhausted.
* Unit UI: content, validation report, coverage, history.

**Files/subsystems**
```text
src/ideapress/domain/{validation,context_assembly,commit}.py
src/ideapress/domain/validators/*.py  src/ideapress/services/units.py
src/ideapress/web/routes/units.py
tests/unit/{test_validators,test_context_budget,test_repair_loop}.py
tests/integration/test_draft_to_commit.py
```

**Tests**
* Every validator: pass, fail, boundary, malformed input, unicode.
* Blocking failure routes to repair; three failures pause the unit and commit nothing.
* Context budget: reduction order respected; requirements never dropped; overflow fails with numbers.
* Commit is atomic: a failure mid-commit leaves no partial version.
* Provenance complete on every committed version (asserted field by field).
* Model output containing `<script>`, `{{ }}` and `../../etc/passwd` is stored and rendered inert.

**Acceptance criteria**
1. A unit is drafted, validated and committed against a real Ollama model.
2. A deliberately non-compliant draft is repaired or the unit pauses — nothing invalid is committed.
3. Every committed unit names the backend, model, prompt versions and validation results that produced
   it.

**Known risks:** validators too strict, blocking legitimate output. Mitigated by the
blocking/advisory split and by making thresholds configuration.
**Likely failure modes:** a partial commit after a mid-write failure; context reduction silently
dropping a requirement; provenance missing the prompt version.
**Gold standards:** nothing commits unvalidated; complete provenance.
**Deferred:** audit and critique.

---

## Phase 5 — Audit, critique and bounded revision

**Goal:** model-assisted review that reports rather than decides, with revision bounded and its stop
reason recorded.

**Prerequisites:** P4.

**Work**
* Tables: `audit_findings`, `critiques`.
* Stages: `audit_fast`, `audit_deep` (escalation only), `fact_check` (optional, on for
  research-backed content types), `critique`, `revise`.
* Escalation policy; "leave it alone" as a first-class verdict; diminishing-returns detection computed
  from deterministic finding counts, not from the critic's self-assessment.
* Findings UI with severity, evidence and what changed between rounds.

**Files/subsystems**
```text
src/ideapress/domain/{audit,critique,revision_policy}.py  src/ideapress/services/review.py
tests/unit/{test_escalation,test_revision_stop,test_diminishing_returns}.py
tests/integration/test_review_loop.py
```

**Tests**
* Fast audit below threshold escalates exactly once per round.
* "Leave it alone" ends revision without a change; a purely stylistic critique does not trigger one.
* Revision stops at the round limit **or** on diminishing returns, and records which.
* A critique that would make a unit worse (validation regressions increase) is rejected and the prior
  version retained — the analogue of FreeWeight's critique regression rate.
* Auditors cannot modify content (asserted by the stage's return type and a test).

**Acceptance criteria**
1. A unit with a planted defect is caught by audit and repaired by the writer stage.
2. A clean unit is not revised endlessly: the loop stops and says why.
3. No audit or critique stage can commit or alter content.

**Known risks:** unbounded polishing consuming hours. Mitigated by the round limit and the
diminishing-returns stop, both tested.
**Likely failure modes:** a revision that worsens validation being accepted; escalation firing on
every unit and doubling model cost; a critique verdict parsed as a command rather than a report.
**Gold standards:** auditors report, writers repair; bounded loops with recorded stops.
**Deferred:** exports, project review.

---

## Phase 6 — Exports, project review, second backend · **M7 beta**

**Goal:** a finished project can be exported deterministically, and the same workflow runs on a second
backend with no code change.

**Prerequisites:** P5.

**Work**
* Stage `project_review` (cross-unit consistency findings, advisory).
* Exporters: Markdown, HTML (MirrorWall-themed, self-contained), JSON (full structure and provenance);
  deterministic rendering; export format versioning.
* `OpenAICompatibleBackend` with honest capability reporting and the degradation record when
  structured output is unavailable.
* **Backend-parity test**: the same project workflow against `ollama`, `openai_compatible` and the
  fake, asserting identical structure.
* Content types: `article` and `report`, both registered through the open registry.

**Files/subsystems**
```text
src/ideapress/domain/exporters/{markdown,html,json}.py  src/ideapress/services/export.py
src/ideapress/infrastructure/backends/openai_compatible.py
src/ideapress/content_types/{article,report}/**
tests/integration/test_backend_parity.py  tests/unit/test_exporters.py
```

**Tests**
* Export determinism: the same committed project exports byte-identically, twice and across platforms.
* HTML export opens offline with no external request.
* Parity: identical unit count, requirement coverage and validation outcomes across backends.
* A backend without structured output records the degradation and still produces a valid unit.

**Acceptance criteria**
1. A complete project exports to Markdown, HTML and JSON deterministically.
2. Switching `inference.mode` between `ollama` and `openai_compatible` requires no workflow change.
3. IdeaPress is fully useful with **no LoadCoach and no FreeWeight** — this is the beta claim.

**Known risks:** export drift between formats. Mitigated by a shared rendering model with
format-specific serializers.
**Likely failure modes:** non-deterministic ordering from an unsorted collection; an HTML export
referencing an external asset; a backend difference changing unit structure rather than only wording.
**Gold standards:** deterministic exports; backend parity; standalone completeness.
**Deferred:** LoadCoach.

---

## Phase 7 — Optional LoadCoach backend

**Goal:** with LoadCoach configured, the same workflows run through it — with routing metadata,
queueing and feedback — and nothing about the workflow code changes.

**Prerequisites:** P6; **LoadCoach 1.0 (M5)**.

**Work**
* `LoadCoachBackend`: version negotiation on first contact against the unauthenticated
  `GET /api/v1/version`; `LOADCOACH_TASK_MAP` in exactly one module, mapping audits to
  **`content.review`** rather than `code.review`; `system`/`prompt` sent as rendered, which LoadCoach
  forwards unmodified; synchronous `/generate` for interactive stages and `/jobs` for long ones; SSE
  streaming; a per-attempt idempotency key; `X-Request-ID` and `X-Client-Name: ideapress`; routing
  metadata (decision id, score, flags, runtime profile hash, served context) captured onto the
  attempt.
* Feedback: after a unit commits, post acceptance and validation results to
  `POST /jobs/{id}/feedback`.
* Degradation: configured fallback, or a clear failure when pinned; version mismatch reported with
  both versions.
* UI: backend indicator, routing metadata on each attempt, an egress badge for remote-routed work.

**Files/subsystems**
```text
src/ideapress/infrastructure/backends/loadcoach.py  src/ideapress/services/feedback.py
tests/contract/test_loadcoach_backend.py  tests/integration/test_loadcoach_degradation.py
tests/live/test_loadcoach_live.py       # marked
```

**Tests**
* Against a schema-driven mock built from LoadCoach's committed OpenAPI snapshot, obtained from the
  `loadcoach` distribution as a **test-only** dependency of `ideapress[dev]`
  (Testing Standards §8): generate, stream, job submission,
  cancel, feedback. `lint-imports` continues to forbid `from loadcoach import …` anywhere under
  `src/`, and the clean-venv install-check proves `pip install ideapress` pulls in no application.
* Version mismatch ⇒ `BACKEND_VERSION_MISMATCH` naming both versions; no silent downgrade.
* LoadCoach unreachable with fallback configured ⇒ falls back and records the degradation; pinned ⇒
  fails the stage, project intact.
* `LOADCOACH_TASK_MAP` appears in exactly one file (asserted by a grep test), is **total** over the
  model-using stages in Workflows §2, and every value it names exists in the task
  profiles the configured LoadCoach reports from `GET /task-profiles` (checked at backend test time,
  so a profile renamed on the other side surfaces as a clear error rather than
  `TASK_PROFILE_NOT_FOUND` mid-project).
* The prompt LoadCoach forwarded equals the prompt IdeaPress rendered — asserted against the
  schema-driven mock's recorded request, because the attempt's `prompt_sha256` provenance depends
  on it.
* Parity extended: the same workflow across all **three** backends produces identical structure.
* Feedback posted once per committed unit, idempotently.
* Live (marked): a real project stage through a real LoadCoach.

**Acceptance criteria**
1. Switching to `inference.mode = "loadcoach"` changes no workflow code and produces equivalent output.
2. Routing metadata is visible per attempt in the UI.
3. Turning LoadCoach off mid-project leaves the project resumable.
4. Feedback reaches LoadCoach and appears in its reliability statistics.

**Known risks:** coupling creeping in through convenience. Mitigated by the single-module task map, the
import-linter contract and the parity test.
**Likely failure modes:** a LoadCoach task ID appearing outside the adapter; a retried submission
creating a duplicate job because no idempotency key was sent; feedback posted more than once per
committed unit.
**Gold standards:** optional integration; identical workflow code; explicit degradation.
**Deferred:** using LoadCoach reliability data for stage hints (post-1.0).

---

## Phase 8 — UI completion and the editing experience

**Goal:** IdeaPress is pleasant to use for real work: a workspace where a person reads, judges and
directs, rather than watching logs.

**Prerequisites:** P7 (or P6 if LoadCoach is deferred).

**Work**
* Project workspace: unit navigator, content view with findings inline, requirement coverage panel,
  version history and diff, per-unit actions.
* Live stage view over SSE with per-unit progress and token streaming where available.
* Plan editor: reorder, split, merge units; edit goals; reassign requirements — all with re-validation.
* Diff view between unit versions.
* Export dialog with format, scope and content-inclusion choices stated plainly.
* Accessibility and UI checklist pass.

**Files/subsystems**
```text
src/ideapress/web/routes/{workspace,plan,export}.py  src/ideapress/web/templates/**
src/ideapress/web/static/js/{workspace,diff}.js
tests/e2e/{test_workspace,test_plan_editing,test_export_dialog}.py
tests/accessibility/test_ui_checklist.py
```

**Tests**
* A long stage survives a browser refresh (SSE replay).
* Plan edits re-validate coverage and refuse an edit that orphans a blocking requirement.
* Diff view correct across versions, including unicode and long lines.
* Every UI checklist item passes.

**Acceptance criteria**
1. A user can run a project start to finish from the UI alone.
2. Findings, coverage and history are visible without leaving the unit.
3. Every acceptance item in ui ux standards passes.

**Known risks:** the editor growing into a word processor. Mitigated by scope: IdeaPress produces and
validates content; it is not a rich text editor, and revisions go through stages.
**Likely failure modes:** a plan edit orphaning a blocking requirement; a long stage losing its live
view after a refresh; diff rendering breaking on long lines or unicode.
**Gold standards:** accessible, dense, honest UI.
**Deferred:** hardening.

---

## Phase 9 — Hardening and 1.0 · **M8**

**Goal:** IdeaPress 1.0 — installable, safe with untrusted model output, documented, complete against
its acceptance criteria.

**Prerequisites:** P8.

**Work**
* Security pass: sanitization of all rendered model output, archive import hardening, path containment,
  auth and exposure refusals, `Host` validation, the CSRF token on form routes, egress labelling.
* Performance pass against every budget.
* Documentation: README, quickstart, configuration reference, workflow guide, content-type authoring
  guide, backend guide, troubleshooting, backup/restore, upgrade notes.
* Project import/export archives (portable projects) with hardened extraction.
* Publish `ideapress 1.0.0`.

**Files/subsystems**
```text
src/ideapress/services/project_archive.py
docs/{quickstart,configuration,workflows,content-types,backends,troubleshooting,upgrading,security}.md
tests/security/**  tests/performance/**  tests/e2e/test_full_project_journey.py
```

**Tests**
* Every security test in Security Standards §14, plus:
  model output rendered inert in every view and every export format; malicious project archive rejected
  (traversal, symlink, bomb).
* Every performance budget met.
* Clean-machine install: `pip install ideapress` → create → plan → draft → export, with only Ollama.
* Full journey with LoadCoach, and again with LoadCoach stopped mid-project.

**Acceptance criteria**
1. All 12 acceptance criteria in the [spec §20](spec.md) pass.
2. All IdeaPress gold standards in Gold Standards §2 are met.
3. Documentation complete; `ideapress doctor` diagnoses every documented failure mode.
4. `ideapress 1.0.0` published.

**Known risks:** the temptation to add content types and formats before hardening. Mitigated by the
deferred list being final for 1.0.
**Likely failure modes:** a sanitizer gap in one export format but not another; documentation drift
from the generated configuration reference; an archive import path that writes before validating.
**Gold standards:** every IdeaPress gold standard, measured.
**Deferred to post-1.0:** more content types and export formats, failure memory, concept competition,
research backends, per-project prompt overrides, publishing integrations, reliability-informed stage
hints.
