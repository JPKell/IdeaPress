# Changelog

All notable changes to `ideapress` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

## [1.3.1] - 2026-09-07

### Changed

- `modelrack` range widened from `>=0.5,<0.6` to `>=0.7,<0.8` so the four applications can be
  installed into one environment again (packaging standards §4): `freeweight 1.1.0` and
  `loadcoach 1.1.3` floor it at 0.7, and `pip install freeweight loadcoach ideapress promptcadence`
  refused to resolve with 1.3.0. IdeaPress's providers are unchanged in shape across the range.

### Added

- `tests/unit/test_every_command_has_help.py`, walking `typer.main.get_command(app)`
  recursively so every command and option must carry help text (M9 audit Group 5, item D4).

- `tests/security/test_checklist.py`, in the shape of LoadCoach's, asserting
  `docs/security.md`'s checklist against the code (M9 audit Group 5, item Q4).

- `tests/unit/test_troubleshooting_covers_doctor.py`, ported from FreeWeight, holding
  `docs/troubleshooting.md` to naming every check `doctor` runs (M9 audit Group 5, item D6).

- `docs/upgrading.md` refreshed to cover every shipped version (M9 audit Group 5, item D9).

- `.github/workflows/release.yml` now writes `SHA256SUMS` over `dist/*` and attaches it
  alongside the wheel and sdist on the GitHub release (M9 audit Group 5, item R4).

- A `## Compatibility` table in `README.md` listing every declared suite package range
  from `pyproject.toml`, and `tests/unit/test_readme_compatibility.py` asserting the two
  cannot drift (M9 audit Group 5, item R3).

- `tests/integration/test_downgrade_and_schema_ahead.py` drives the packaging standards §6.1
  downgrade drill end to end: upgrade, write a row, back up, jump `alembic_version` ahead,
  `SchemaAhead` refuses and names both revisions and the backup directory, restore the backup,
  start again (M9 audit Group 3, item O2). `ensure_ready` did not previously detect this case at
  all — it fell through to an attempted `upgrade()` alembic would fail on its own terms. Also
  proves IdeaPress's own translation of the same refusal into `Runtime.startup_error` (spec §20
  AC7: an unreachable/broken database is a health condition here, never a startup crash).
- `tests/integration/test_backup_restore.py` (new — IdeaPress had none), in the shape of
  FreeWeight's own: WAL replay, corrupt-backup refusal, no leftover `.pre-restore` file, plus a
  round trip parametrized over `weightsdb.testing.temporary_sqlite`/`temporary_postgres` directly
  (M9 audit Group 3, item O4).
- `tests/fixtures/databases/ideapress-1.3.0.sqlite3`, a real `ideapress==1.3.0` PyPI install
  migrated and seeded with two projects through its own CLI (`ideapress project create`),
  exercised by `test_1_3_0_database_migrates_to_head_and_keeps_its_rows` (M9 audit Group 3, item
  O1). `.gitignore` now keeps `tests/fixtures/databases/*` out of the blanket `*.sqlite3`
  exclusion, the same way FreeWeight's does.

### Fixed

- **Every CLI argument and option now carries help text.** `test_every_command_has_help.py`
  found ~40 undocumented ones — every positional argument (`project_id`, `unit_key`, `task_id`,
  `workflow_id`, `prompt_id`) and several flags (`--json`, `--config`, `--content-type`,
  `--archived`) across `project`, `plan`, `stage`, `unit`, `workflow`, `prompts`, `backend`,
  `db` and `config` — so `--help` at any depth of the command tree is now complete.

### Removed

- `pydantic-settings` from `dependencies` — declared but never imported (ADR-0114); each
  application performs its own layered configuration merge. `requirements/ci.lock`
  recompiled with pip-tools 7.6.1 on Python 3.13 (M9 audit Group 5, the pydantic-settings
  finding from row L2).

## [1.3.0] - 2026-09-07

### Added

- A committed OpenAPI snapshot at `docs/openapi.json`, compared byte for byte against the served
  document by `tests/contract/test_openapi_snapshot.py` (ADR-0108, row K2). Regenerate with
  `python -c 'from tests.contract.test_openapi_snapshot import write; write()'` after any route
  change.
- **`project_review`'s prompt now has a budget** (row K3, J2's D3 follow-up,
  `docs/history/K3_HANDOFF.md`). `domain.context_assembly.assemble_review_context()` routes every
  committed unit through the same `cutctx.DropOldestPolicy` chain J2 wired up for per-unit
  assembly, behind a sibling seam of the same shape. Nothing is pinned — stage 15's documented
  input is every committed unit and nothing else — so units are dropped from the *end* of reading
  order first, keeping the earliest units (where the project's terms and facts are established)
  intact for as long as the new `workflow.project_review_context_budget_tokens` (default 24000)
  allows; a budget too small to hold even the single cheapest unit is refused with
  `ContextLimitExceeded` rather than silently reviewing an emptied document. A generous budget
  reproduces the prior unbudgeted rendering byte for byte (golden-tested against a
  pre-change capture, `tests/unit/test_context_budget.py`). A dropped review emits
  `context.compacted` once, on the same event sink `unit_loop`/`review_loop` already use.

- **`attempts.cache_write_tokens` and `attempts.cache_read_tokens`** (migration `0009`), and the
  two matching fields on `ideapress.domain.inference.TokenUsage` — ADR-0070 rule 4's spelling, and
  the classes LoadCoach has carried on its wire since rule 7 (row K4). Both backend adapters now
  fill them: a `0` from a protocol that bills no cache tier is a real zero and totals (rule 1),
  while a class the backend could have reported and did not stays `None` and keeps its floor.
  Existing rows stay `NULL` — a build that never asked observed nothing, and a back-fill of `0`
  would be the fabricated zero ADR-0016 forbids.

### Fixed

- **A token count LoadCoach could not take is no longer recorded as zero.** All four billable
  classes on `ideapress.domain.inference.TokenUsage` are now `int | None`, `None` meaning the
  backend reported no count. `infrastructure/backends/loadcoach.py` read `"unsupported"` and a
  missing key as `0` for input and output — harmless while LoadCoach sent `null` there, and a
  fabricated zero from loadcoach 1.1.3 onward, which renders every unavailable count as
  `"unsupported"` (ADR-0016 rule 4, ADR-0112). The ModelRack adapter's `_count` helper, which
  turned `UNSUPPORTED` into `0` "for arithmetic only", is gone for the same reason. An unreported
  class now becomes `UNSUPPORTED` at the BaseAiCore boundary, so the estimate does not total and
  the figure is announced as a floor (ADR-0069) instead of pricing a real call's input side at
  nothing. The unit detail page and `ideapress unit show` render an unreported count as an em
  dash, and the backend conformance suite states the rule for tokens as it already did for
  timings.

### Changed

- **`services/pricing.py` is now a thin edge over `loadledger.pricing`** (row K4, ADR-0110). The
  ~250-line reader row J1 transcribed from `promptcadence.services.pricing` moved into
  `loadledger 0.3.0`, the suite's one ADR-0072 reader, because `pricing_hash` is only a join
  between a stored usage and a price if one reader produces both applications' records. What stays
  here is where the path comes from (`[pricing] file`) and reporting a broken one as this
  application's own `ConfigurationError`. Every J1 pricing test passes unchanged, and the package
  carries a golden case asserting the moved reader reproduces the exact hashes this application's
  own loader produced beforehand. The dependency floor rises to `loadledger[sql]>=0.3,<0.4`.
- **A fully reported, fully priced attempt now renders a bare total** rather than "at least".
  Until row K4 this application's domain type carried no cache classes at all, so LoadLedger
  counted every debit as unmetered and every figure was a floor forever — for a reason that had
  nothing to do with the call (`docs/history/J1_HANDOFF.md` §8). A floor now means something was
  genuinely unreported, which makes `[budget] partial_pricing = "strict"` usable against a backend
  that reports all four classes.

## [1.2.0] - 2026-09-07

IdeaPress 1.2: **M13's three adoption phases** — IP-A1 LoadLedger and IP-A2 Commissioner (row J1)
and IP-A3 CutCtx (row J2). Every harness-arc package now has a second real consumer.

### Added
- **Per-unit and per-project cost on the workspace page**, from `loadledger 0.2.0` mounted into
  this application's own database (ADR-0050; migration `0007`). Every stage attempt debits once,
  atomically with its attempt row, at the one funnel every stage already routes through
  (`services/stages.py::record_attempt`). A local model's cost is `UNSUPPORTED`, rendered `—` with
  the reason, never `$0.00` (ADR-0016); a price list that could not total an estimate renders "at
  least" (ADR-0069). `[pricing] file` optionally names a price catalogue in the format ADR-0072
  defines; `[budget]` configures an optional `per_output` ceiling (one unit's own attempts, or a
  project's pseudo-run for a stage with no unit) and an optional `per_project` ceiling (lifetime,
  never resets). A bound `per_output` ceiling pauses the in-flight unit, reusing the existing
  pause/resume path (ADR-0103).
- **The workspace's egress badge now reads a recorded decision**, from `commissioner 0.1.1` mounted
  the same way (migration `0008`). Every attempt is evaluated against the configured backend and
  recorded, approved or denied, through Commissioner's shipped `OrderedClassificationPolicy`
  (ADR-0054). A fresh installation, or one whose backend just changed, states plainly that nothing
  has run yet rather than falling back to the pre-J1 flag.
- `inference.loadcoach.max_data_classification` and `inference.openai_compatible.max_data_classification`
  — the per-backend ceiling Commissioner's fail-closed policy checks against (ADR-0103, D6).

### Changed
- **Behaviour change:** a remote backend (`loadcoach` or `openai_compatible`, with
  `providers.allow_remote = true`) with no declared `max_data_classification` now has every attempt
  recorded as a denial (`no_ceiling_declared`), fail closed, rather than the badge simply stating
  "leaves this machine" with no durable record at all. Nothing about what actually runs changes —
  Commissioner does not enforce, and `providers.allow_remote` is the unchanged, real gate. See
  `docs/upgrading.md`.
- `setspec` floor raised from `>=0.4,<0.7` to `>=0.5,<0.7`, matching what `commissioner 0.1.1`
  itself requires (ADR-0103).
- `services/workspace.py`'s ad-hoc `"egress"` expression and `_is_remote` helper are deleted; the
  workspace badge and the per-attempt egress fact in a unit's provenance table both read
  `Database.egress` instead.
- **Stage context assembly runs through `cutctx`.** Stage context assembly's reduction (workflows §7) now runs through `cutctx`'s `DropOldestPolicy`,
  behind the unchanged `assemble_context()` seam: same sections, same order, same dropped order, the
  same `ContextLimitExceeded` carrying both numbers (ADR-0104, row J2). A dropped assembly emits the
  suite's `context.compacted` report once per attempt. `project_review`'s unbudgeted whole-document
  prompt is unchanged and out of scope — it performs no reduction to convert (see the row's handoff,
  `docs/history/J2_HANDOFF.md`).

## [1.1.0] - 2026-09-05

IdeaPress 1.1: **LA2** — a stage may pin a LoRA adapter, the pin travels to LoadCoach as its
`adapter` override, a pin that cannot be honoured fails its stage instead of being served by the
bare base, and every attempt names the subject that answered it. Phase 10 of the
[development plan](docs/apps/ideapress/development-plan.md).

**The exit was demonstrated, not argued.** A real installation ran three model-using stages, each
pinning a different adapter, over HTTP to a real `loadcoach serve` over a real `llama-server`: one
server process answered all three and LoadCoach's residency ledger holds one row, because an
adapter switch on a resident base writes none. The three answers to one prompt are visibly a
pirate, a terse editor and a verbose one, which is the canary that the adapters applied, and
`SELECT stage, adapter_name, subject_canonical_id FROM attempts` separates the three stages from
the database alone.

**Requires `loadcoach 1.1.0`** for a pinned stage or a declared classification above the default.
An installation with neither is 1.0 traffic and keeps working against a LoadCoach 1.0.

### Added
- **A stage may pin a LoRA adapter** (ADR-0083). `[models.stage_adapters]`, beside
  `[models.stages]` and sparse: a stage with no key has no pin, and a key present is a pin in
  effect. There is no second boolean, and it does **not** ride
  `[inference.loadcoach] honour_stage_bindings`, whose documented meaning is "give up routing" —
  an adapter pin does not surrender routing, so riding that flag would make the configuration lie.
  A pin naming a gate stage or an unknown stage is refused at startup by name, as
  `job_stages` already is; any pin at all is refused when `[inference] mode` is not `loadcoach`,
  because the direct and OpenAI-compatible paths stay adapter-free and an adapter through an
  OpenAI-compatible endpoint would evade identity tracking.
- **`[inference] data_classification`** — one value for every request this installation makes,
  because the true statement is about the installation and not about a stage. It travels to
  LoadCoach, which records `max(caller, adapter)` (ADR-0065 rule 2). The default is the lowest
  level, so the join equals the adapter's own classification and a `1.0` configuration behaves
  exactly as it did. A value outside the vocabulary is refused at startup rather than dropped at
  the wire: a misspelling that travelled as "declared nothing" would be an under-declaration
  nobody sees.
- **`ADAPTER_NOT_FOUND` and `ADAPTER_PROFILE_MISMATCH`** join the error vocabulary. Both **fail
  the stage** — a pin that cannot be honoured is never quietly served by the bare base
  (ADR-0064 rule 4) — and both are permanent for the request as written, so neither is retried.

### Changed
- **The persisted attempt gains an adapter axis** (ADR-0058, ADR-0080). Migration `0006` adds
  `adapter_name`, `adapter_digest` and `subject_canonical_id` to `attempts`, beside the four model
  columns. Each is written from what the backend said **answered** the request, never from the pin
  that asked for it — a pin can be refused between the two. Rows written before 1.1 are base
  subjects and keep `NULL` in all three: "no adapter answered" and "an adapter whose name we do not
  know" are different facts, and a back-fill would collapse them.
- **`baseaicore` floor raised to `>=0.4.1`.** `DataClassification`, which the new configuration key
  is validated against, arrived there.
- **`setspec` widened to `>=0.4,<0.7`** (E5's pin sweep). `mirrorwall 0.2.1` required
  `setspec<0.5` and every application carried the matching cap; `mirrorwall 0.2.2` lifted it.
  IdeaPress's `setspec` surface is `setspec.prompts` plus `GeneratorInfo`, neither of which
  changed, so the floor stays 0.4 and this widens a range without adopting any payload. The full
  suite was run against the resolved `setspec 0.6.0` (with `baseaicore 0.4.1`, which 0.6.0
  requires, and `mirrorwall 0.2.2`) and passes unchanged.

### Fixed
- The release pipeline no longer fails on a missing lockfile. `requirements/release.lock` and its
  `release.in` were never generated for this repository, while `release.yml`'s TestPyPI job
  installed from it — so the dry run failed at its first step. The lock is now committed,
  hash-verified, and resolves identically to the rest of the suite.
- `release.yml`'s `release` job is restricted to tag pushes (`if: github.event_name == 'push'`).
  `workflow_dispatch` had been added without that guard, so triggering the TestPyPI dry run by
  hand would also have run the real-PyPI job.
- The `release` job now builds through the same hash-pinned chain as the dry run
  (`pip install --require-hashes -r requirements/release.lock` and `build --no-isolation`)
  instead of resolving `build` and `twine` fresh, so the two jobs produce the same artifact.

## [1.0.0] - 2026-09-01

IdeaPress 1.0: the optional LoadCoach backend, a workspace to work in, and the hardening pass.
It still does the thing it was built to do with **only Ollama present** — no LoadCoach, no
FreeWeight, no configuration beyond the stage model bindings.

### Added
- **The optional LoadCoach backend** (P7). `inference.mode = "loadcoach"` routes every model-using
  stage through a running LoadCoach by task profile, with version negotiation on first contact,
  a per-attempt idempotency key, `X-Request-ID` / `X-Client-Name` propagation, synchronous
  `/generate` for interactive stages and the `/jobs` queue for long ones, SSE streaming, and the
  routing decision recorded on every attempt. No workflow code changes to switch.
- Feedback: after a unit commits, its acceptance and validation result are posted to LoadCoach
  once per job, idempotently, and never in a way that can fail the commit.
- `[inference.loadcoach] honour_stage_bindings` (default `false`) — the explicit opt-in that sends
  a `[models.stages]` binding to LoadCoach as a model override. Off by default because LoadCoach
  choosing the model is the reason to use it (ADR-0040).
- `[inference.loadcoach] job_stages` — which stages go through the queue rather than the
  synchronous endpoint. Refuses a name that is not a model-using stage.
- The configured `inference.fallback_mode` is now **applied**, not merely described: an
  unreachable backend falls back at the single choke point and records a `backend_fallback`
  degradation naming both backends. `pin_backend` fails the stage instead, project intact.
- A test asserting that only `services/inference.py` calls a backend's `generate`. The gateway's
  docstring has claimed since P2 that a test walks the source to prove this; none did.

### Added
- **The project workspace** (`/projects/{id}/workspace`, P8) — unit navigator, content, findings,
  requirement coverage, version history and provenance on one page, so the questions a person asks
  between stages are answered without a page change.
- **The plan editor**: reorder, split, merge, reassign and rewrite goals. Every edit re-validates
  the whole plan and one that would leave a blocking requirement with no unit responsible for it
  is refused **by name**, with the plan unchanged. Structural edits are refused once a unit holds
  committed text — finished work is never renumbered out from under itself (workflows §9).
- **The diff view**: line-by-line between any two committed versions, with `+`/`-` markers so
  colour is never the sole indicator, and unicode and 900-character lines carried through intact.
- **The export dialog**: what each format contains, which units will be left out and why, and the
  fact that exports are byte-identical — stated on the page rather than left to be discovered.
- A paused unit now shows its reason **and its remedy** on the page a person is already looking
  at: a budget-exhaustion pause names `workflow.structured_output_tokens`, its default and its
  range, with the resume action beside it.
- Routing metadata (decision id, score, flags) is rendered per attempt, and an egress badge says
  plainly whether work leaves this machine (P7 AC2, risk S4).
- `tests/accessibility/test_ui_checklist.py` covers UI/UX Standards §13 across **every** UI page,
  enumerated from the routers rather than from a hand-written list.

### Changed
- `BackendCapabilities` gains `routes_internally`. When a backend sets it, the gateway resolves no
  `[models.stages]` binding and performs no unload — model choice and residency belong to the
  backend that owns them (ADR-0040). Without this, `mode = "loadcoach"` on the shipped defaults
  would have pinned every request to the bound model and bypassed LoadCoach's routing, evidence
  and admission control entirely, while every stage still succeeded.
- Through LoadCoach, a `json_schema` request is sent as `json` and the difference is recorded as a
  `structured_output_unavailable` degradation: LoadCoach applies the *task profile's* schema, not
  the caller's, and for `content.review` that schema forbids `requirements_assessment` outright —
  which would have made ADR-0039's attestation impossible through this backend (ADR-0041).
- The backend-parity test now runs the identical workflow across **four** adapters, and asserts
  that one configured output budget reaches all four unchanged.

### Security
- **The sanitization sweep** (P9): model output is inert in every view and every export format,
  with the surfaces **enumerated mechanically** — export formats from `FORMATS`, templates by
  walking the tree, UI pages from the routers — so the named failure mode (a gap in exactly one
  surface) cannot be introduced by adding a page or a format.
- **Portable project archives**, hardened: `ideapress project export|import` (M7-27, spec §7.2).
  Nothing is written until everything is validated — containment, symlinks, hardlinks, device
  files, an entry-count cap, a per-entry cap, a total cap and a compression-ratio cap. A refused
  archive leaves no directory and no row, and `--inspect` reports what an archive contains without
  importing it.
- **ADR-0026 proven on a non-loopback bind** (M7-31): a LAN bind with no `allowed_hosts` refuses to
  start; `Host` is validated before routing, so a path that does not exist is still 421; CSRF is
  enforced on every form route, enumerated from the routers; egress is labelled.

### Performance
- All **seven** of spec §15's budgets are now asserted under the `performance` marker (M7-28), the
  four project-sized ones against a real 100-unit committed project (M7-30's missing fixture).

### Documentation
- [ADR-0040](../docs/adr/0040-routing-backend-owns-model-choice.md) — a routing backend owns model
  choice and residency.
- [ADR-0041](../docs/adr/0041-caller-schemas-do-not-travel-through-a-router.md) — a caller's output
  schema does not travel through a router; the caller still owns it.
- Workflows §11 no longer contradicts ADR-0039: a model may not decide a requirement is satisfied
  *by saying nothing about it*, which is a different rule from the one it replaced.

## [0.1.1] - 2026-08-31

The M7-verification fixes. The release blocker was 1a: on the default two-model configuration a
single unit whose review hit an exhausted output budget aborted the whole draft stage and left
the project unrecoverable from the CLI.

### Fixed
- **A stage LoadCoach refused is no longer reported as a successful empty generation.** LoadCoach
  answers a declined stage with HTTP 200 and a job record whose `state` is `failed`; the adapter
  read its benign defaults out of it — an absent `finish_reason` became `"stop"` — and returned
  empty text with no degradation. The unit committed empty, nothing said why, the configured
  fallback never engaged, and acceptance feedback was posted to LoadCoach about a job that never
  ran. A terminal-state check now sits at the one funnel the synchronous, queued and streaming
  paths share; `CONTEXT_LIMIT_EXCEEDED` raises `ContextLimitExceeded` and every other terminal
  non-completion — including an unrecognised code — raises the recoverable `BackendUnavailable`,
  which engages the fallback and leaves the project resumable.
- **A busy LoadCoach now engages the fallback instead of reporting a content rejection.**
  `NO_ELIGIBLE_MODEL` arrives as a 422 and `QUEUE_FULL` as a 429, and the adapter's 4xx branch
  turned every such answer into `ContentRejected` — which is not recoverable, so the configured
  fallback never engaged, and which told the user their *content* had been refused because a GPU
  was busy. Capacity codes are now classified by code rather than by status class, and the same
  set is shared with the failed-job path so the two cannot disagree.
- **A retry through LoadCoach is a retry again, not a replay of the previous failure.** LoadCoach
  replays the original job for a repeated idempotency key "whether the execution is still running
  or finished long ago", for 24 hours by default — including a job that *failed*. IdeaPress's key
  digested only the request's coordinates and text, so a stage declined once for a transient
  reason (a busy GPU, a full queue) produced the identical key on every later attempt and replayed
  that failure for a day, while the error told the user the project was resumable. The key now
  includes the stage run id, stamped by `InferenceGateway.begin_run`, so a fresh run is new work
  and a network-level retry within one run is still idempotent.
- `X-Request-ID` is now actually propagated to the backend. Nothing ever set
  `Correlation.request_id`, so the header documented as propagated was absent from every request.
- **A stage's terminal state and its terminal event now commit together** (ADR-0044). They were
  two transactions, and every poller in the product read them in the order that loses: `plan build`
  drained the event log and *then* asked whether the run had finished, so a run whose state
  committed first ended with no `stage.completed` or `stage.failed` line printed at all — a stage
  that stopped without saying whether it worked. CI found the same window as a flaky test on a
  slower runner. Swapping the order would only have moved the hazard to the SSE client, so the two
  writes are now one transaction (`StageEventSink.emit(..., alongside=...)`) and the CLI checks
  before it drains. LoadCoach already worked this way; the rule now exists for both.
- **A deterministic check may no longer be a restatement of its own requirement** (ADR-0042). The
  compiler emitted `must_contain_any` over phrases lifted from the requirement it had just written,
  so a unit satisfied the check by *quoting the requirement* — and the coverage report called that
  `deterministic_check`, a stronger claim than the audit makes and a false one. Observed on a real
  brief: a unit committed reporting `2/2 requirements satisfied` while its own critique read
  *"fails the blocking requirement R-006"*. Such checks are now dropped at compile time and logged;
  the requirement becomes honestly check-less and routes to the audit under ADR-0039. The gate's
  asymmetry is unchanged — a surviving check still settles its requirement and a model still cannot
  overturn it.
- **Grounding is verified, not assumed** (ADR-0043). Three mechanisms, layered.
  **(1)** A blocking requirement that asks for claims to rest on evidence, in a project with **no
  sources attached**, is now refused by `plan build` before any unit is written — naming the
  requirement and the remedy — because it is unsatisfiable, not merely hard. M8 observed the
  alternative: a brief asking for claims "grounded in usage figures" with nothing attached produced
  an invented footfall count, an invented attendance figure and a named 2023 audit that does not
  exist, and every gate passed it.
  **(2)** `fact_check` moves from a stage that existed in the vocabulary to one the unit loop runs,
  after the audits and before the critique, for any unit carrying a grounding-demanding requirement
  in a project that has sources. It reports claims the sources do not support as `major` findings
  that flow into the existing review loop. **It cannot pass a requirement** — only add findings — so
  a model still does not decide the gate.
  **(3)** Requirement coverage distinguishes *satisfied* from *satisfied against no source*, in the
  table and in all three export formats. Reporting them identically is what let a unit commit
  invented figures under a green report. Only a **non-blocking** grounding requirement can reach
  that state — a blocking one is refused at plan time — which is exactly the case the refusal
  deliberately lets through, and therefore exactly the case the report must be honest about.
- **A partially committed export now discloses what it is missing, or refuses.** A project with
  *nothing* committed has always refused; a project with *some* of its plan committed silently
  succeeded — dropping the uncommitted units, dropping the requirements they owed, reporting the
  committed count as though it were the plan, and rendering every coverage row as `Satisfied: yes`.
  A reader saw a complete document. Export now refuses such a project (in `--stdout` too, since the
  usual use is `> file`), with `--allow-partial` to opt in; what it then writes carries an
  incomplete banner, a *Sections not written* table with each unit's state and pause reason, and
  planned-versus-committed counts.
- **The requirement-coverage table lists every requirement once, answered or not.** It was built by
  walking the committed units, so a requirement shared by four units appeared four times and a
  requirement whose only unit never committed appeared not at all.
- **`ExportUnit.findings` and `.critiques` are populated.** Both fields existed since the exporters
  were written and nothing ever filled them, so a unit that committed carrying unresolved `major`
  findings — because the review stopped on `diminishing_returns` rather than because they were
  fixed — exported as though it had none.
- `ideapress export run` reports a refusal as a one-line message and exit 2, not a traceback.
- The LoadCoach adapter refuses a task-profile list it cannot read, instead of reporting an empty
  catalogue. Zero profiles is a valid number and a running LoadCoach never serves it, so reading
  "no identifier found" as "none served" made the check that exists to catch a renamed profile
  report the exact opposite of the truth — which is the shape that defect actually took.
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
- **A model's silence no longer settles a blocking gate** (ADR-0039, accepted; M7 finding 3 /
  M7-20). A requirement with no deterministic check was satisfied whenever the audit's findings
  did not mention its key; the audit stages (`audit_fast`/`audit_deep` prompts 1.1.0) now return
  an explicit per-requirement verdict (`met` / `not_met` / `cannot_judge`) and only a literal
  `met` satisfies — an absent verdict, `cannot_judge`, or an invented word all leave the
  requirement unsatisfied and pause the unit. New `workflow.allow_audit_gated_requirements`
  (default true); set false, even attestation is refused and the gate is wholly mechanical.
- A blocking requirement with no deterministic check is labelled **"guaranteed by model
  review, not a deterministic check"** everywhere it appears: the commit event, the unit and plan
  pages, `plan show`, `unit show`, and the coverage note of all three exports (the ADR-0039
  interim safeguard, kept under the accepted mechanism).
- The requirement compiler prompt (`stages.requirements.compile` 1.1.0, M7-21) now pushes a
  blocking requirement toward the strongest literal check the material supports
  (`must_contain_all`, or `must_not_contain` for prohibitions) and confines single-word
  `must_contain_any` to genuine alternatives.

### Added
- `workflow.structured_output_tokens` (default 8192, range 1024–131072): the output-token budget
  for the structured stages (requirements, outline, audits, critique, project review), previously
  a module constant. Raised above the default it also lifts the thinking floor of the
  text-writing stages (draft, repair, revise) — the M7 demonstration paused a draft whose
  message said to raise a budget no setting reached. A model that spends more reasoning tokens
  than the reference machine's can now be given room in `config.toml` instead of a code edit
  (M7 finding 1c).

## [0.1.0] - 2026-08-31

The first published version: the complete M1–M6 build.

### Added
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
