# IdeaPress — configuration reference

**Generated from `ideapress.config.Settings`. Do not edit by hand.**

```bash
python -m ideapress.config_reference > docs/configuration.md
```

A test asserts this file equals what the generator produces, so a setting cannot be added, renamed
or re-defaulted without the reference following it in the same commit.

## Where configuration comes from

Precedence, lowest to highest:

1. built-in defaults — **everything has one**, and `ideapress serve` needs no configuration file at
   all (spec §20 AC1);
2. `config.toml` — `ideapress config path` prints where it is looked for, `ideapress config init`
   writes a commented example;
3. `IDEAPRESS_`-prefixed environment variables, nested with `__`
   (`IDEAPRESS_INFERENCE__MODE=loadcoach`);
4. explicit command-line overrides.

Merging is **per leaf field**, not per section: setting one key of `[server]` never discards its
siblings. `ideapress config show` reports which layer produced every value, and
`ideapress config validate` refuses an invalid file with the offending key named.

## `[server]`

Bind address and HTTP-level limits.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `host` | str | `"127.0.0.1"` | `IDEAPRESS_SERVER__HOST` | Interface to bind. Loopback by default; anything else requires allowed_hosts (ADR-0026). |
| `port` | int | `8767` | `IDEAPRESS_SERVER__PORT` |  |
| `allow_lan_exposure` | bool | `false` | `IDEAPRESS_SERVER__ALLOW_LAN_EXPOSURE` | Acknowledges a deliberate bind to every interface (0.0.0.0). Without it such a bind refuses to start. |
| `allowed_hosts` | tuple[str, ...] | `[]` | `IDEAPRESS_SERVER__ALLOWED_HOSTS` | Host header values accepted on a non-loopback bind, against DNS rebinding. Comma-separated in the environment. |
| `max_body_bytes` | int | `8388608` | `IDEAPRESS_SERVER__MAX_BODY_BYTES` | Largest request body accepted, before it is buffered. Briefs can be long. |

## `[storage]`

Where the database and the project artifact directory live.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `database_url` | str | *(none)* | `IDEAPRESS_STORAGE__DATABASE_URL` | SQLAlchemy URL. Defaults to a SQLite file under the XDG data directory. |
| `auto_migrate` | bool | `true` | `IDEAPRESS_STORAGE__AUTO_MIGRATE` | Run pending migrations at startup. Defaults true on SQLite; a PostgreSQL URL turns it off, because a failed migration there cannot be rolled back automatically (database standards §5.1). |
| `project_dir` | str | *(none)* | `IDEAPRESS_STORAGE__PROJECT_DIR` | Directory holding per-project artifacts and exports. Defaults under XDG data. |
| `statement_timeout_ms` | int | `30000` | `IDEAPRESS_STORAGE__STATEMENT_TIMEOUT_MS` |  |

## `[inference]`

Which backend runs stages, and what happens when it is not there.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `mode` | one of `ollama` | `loadcoach` | `openai_compatible` | `"ollama"` | `IDEAPRESS_INFERENCE__MODE` |  |
| `data_classification` | str | `"public"` | `IDEAPRESS_INFERENCE__DATA_CLASSIFICATION` | The data classification of the work this installation sends to a model. One value for every request, because the true statement is about the installation and not about a stage. Sent to LoadCoach, which records max(caller, adapter) (ADR-0065 rule 2). Unset is the lowest level, which joins to the adapter's own value. |
| `fallback_mode` | str | *(empty)* | `IDEAPRESS_INFERENCE__FALLBACK_MODE` | Optional; empty means no fallback. Ignored when pin_backend. |
| `pin_backend` | bool | `false` | `IDEAPRESS_INFERENCE__PIN_BACKEND` | True = never fall back; fail the stage instead. |

## `[inference.ollama]`

Direct Ollama, the default backend.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `base_url` | str | `"http://127.0.0.1:11434"` | `IDEAPRESS_INFERENCE__OLLAMA__BASE_URL` |  |
| `timeout_seconds` | int | `300` | `IDEAPRESS_INFERENCE__OLLAMA__TIMEOUT_SECONDS` |  |

## `[inference.loadcoach]`

The optional LoadCoach backend: queueing, routing by task profile, and feedback.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `base_url` | str | `"http://127.0.0.1:8766"` | `IDEAPRESS_INFERENCE__LOADCOACH__BASE_URL` |  |
| `api_key_env` | str | *(empty)* | `IDEAPRESS_INFERENCE__LOADCOACH__API_KEY_ENV` | Name of the environment variable holding the token. Never the token itself. |
| `timeout_seconds` | int | `600` | `IDEAPRESS_INFERENCE__LOADCOACH__TIMEOUT_SECONDS` |  |
| `honour_stage_bindings` | bool | `false` | `IDEAPRESS_INFERENCE__LOADCOACH__HONOUR_STAGE_BINDINGS` | Send the stage's `[models.stages]` binding to LoadCoach as a model override. Off by default: LoadCoach chooses the model, which is what it is for. Turning it on pins the model and gives up routing, evidence and reliability for that stage (ADR-0040). |
| `job_stages` | tuple[str, ...] | `['draft', 'revise', 'repair', 'project_review']` | `IDEAPRESS_INFERENCE__LOADCOACH__JOB_STAGES` | Stages submitted through the asynchronous `/jobs` queue rather than synchronous `/generate`. The long ones; everything else is interactive and submitted with `class = "interactive"` so a person is never queued behind background work. |
| `max_data_classification` | str | *(none)* | `IDEAPRESS_INFERENCE__LOADCOACH__MAX_DATA_CLASSIFICATION` | The most sensitive data this backend may receive: public | internal | confidential. Required for Commissioner to approve a remote call through it (row J1, ADR-0054); unset means no ceiling is declared, which a remote target is *denied* under, never assumed public (fail closed — a behaviour change from J1's egress badge, which previously rendered but never gated). Ollama carries no such key: it is never remote. |

## `[inference.openai_compatible]`

Any OpenAI-compatible endpoint. Empty base_url means "not configured", not "localhost".

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `base_url` | str | *(empty)* | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__BASE_URL` |  |
| `api_key_env` | str | *(empty)* | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__API_KEY_ENV` |  |
| `timeout_seconds` | int | `300` | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__TIMEOUT_SECONDS` |  |
| `model` | str | *(empty)* | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__MODEL` | The model name this endpoint serves. OpenAI-compatible servers expose one namespace with no provider prefix, so the `[models.stages]` bindings do not apply to it. |
| `max_data_classification` | str | *(none)* | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__MAX_DATA_CLASSIFICATION` | The most sensitive data this endpoint may receive: public | internal | confidential. Unset denies a remote endpoint outright (fail closed, ADR-0054); see `inference.loadcoach.max_data_classification`. |

## `[models]`

The `[models]` section: `[models.stages]` and, since 1.1, `[models.stage_adapters]`.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `stage_adapters` | dict[str, str] | *(section)* | `IDEAPRESS_MODELS__STAGE_ADAPTERS` | Per-stage LoRA adapter pins, sent to LoadCoach as its `adapter` override. Sparse: a stage with no key has no pin, and a key present is a pin in effect — there is no second boolean, and it does not ride `honour_stage_bindings` (ADR-0083). Values are LoadCoach's manifest names; IdeaPress holds no adapter registry. |

## `[models.stages]`

Stage -> model bindings for standalone mode (`[models.stages]`, spec §12).

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `requirements` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__REQUIREMENTS` |  |
| `research_synthesis` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__RESEARCH_SYNTHESIS` |  |
| `outline` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__OUTLINE` |  |
| `draft` | str | `"ollama/gemma4:12b"` | `IDEAPRESS_MODELS__STAGES__DRAFT` |  |
| `repair` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__REPAIR` |  |
| `audit_fast` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__AUDIT_FAST` |  |
| `audit_deep` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__AUDIT_DEEP` |  |
| `fact_check` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__FACT_CHECK` |  |
| `critique` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__CRITIQUE` |  |
| `revise` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__REVISE` |  |
| `project_review` | str | `"ollama/qwen3.5:9b-q8_0"` | `IDEAPRESS_MODELS__STAGES__PROJECT_REVIEW` |  |

## `[execution]`

How many generations may be in flight, and what happens when the model must change.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `max_concurrent_stages` | int | `1` | `IDEAPRESS_EXECUTION__MAX_CONCURRENT_STAGES` | Generations in flight at once. Only 1 is accepted: a higher value is refused at startup rather than silently honoured (ADR-0038). |
| `unload_before_model_switch` | bool | `true` | `IDEAPRESS_EXECUTION__UNLOAD_BEFORE_MODEL_SWITCH` | Unload the resident model before loading a different one. Turning this off lets two models contend for one GPU, which degrades to CPU or OOM without an error. |

## `[workflow]`

The bounds every loop in workflows §5 runs under.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `max_revision_rounds` | int | `3` | `IDEAPRESS_WORKFLOW__MAX_REVISION_ROUNDS` |  |
| `diminishing_returns_threshold` | float | `0.05` | `IDEAPRESS_WORKFLOW__DIMINISHING_RETURNS_THRESHOLD` |  |
| `max_attempts_per_stage` | int | `3` | `IDEAPRESS_WORKFLOW__MAX_ATTEMPTS_PER_STAGE` |  |
| `audit_escalation_threshold` | float | `0.6` | `IDEAPRESS_WORKFLOW__AUDIT_ESCALATION_THRESHOLD` |  |
| `require_clean_validation_to_commit` | bool | `true` | `IDEAPRESS_WORKFLOW__REQUIRE_CLEAN_VALIDATION_TO_COMMIT` |  |
| `context_budget_tokens` | int | `8000` | `IDEAPRESS_WORKFLOW__CONTEXT_BUDGET_TOKENS` | Token budget for assembled context (workflows §7). Requirements and the unit specification are never dropped to fit it; overflow fails with both numbers. |
| `project_review_context_budget_tokens` | int | `24000` | `IDEAPRESS_WORKFLOW__PROJECT_REVIEW_CONTEXT_BUDGET_TOKENS` | Token budget for project_review's whole-document context (workflows §2 stage 15). Nothing here is pinned — units are dropped, latest in reading order first — and the stage refuses with both numbers rather than silently reviewing an empty document. |
| `allow_audit_gated_requirements` | bool | `true` | `IDEAPRESS_WORKFLOW__ALLOW_AUDIT_GATED_REQUIREMENTS` | Whether an audit's explicit per-requirement attestation may satisfy a blocking requirement that has no deterministic check (ADR-0039). Silence never satisfies one either way. False forces a wholly mechanical gate: such a requirement pauses its unit until it gets a deterministic check or is demoted to advisory. |
| `structured_output_tokens` | int | `8192` | `IDEAPRESS_WORKFLOW__STRUCTURED_OUTPUT_TOKENS` | Output-token budget for the structured stages (requirements, outline, audit_fast, audit_deep, critique, project_review), and — when raised above the 8192 default — the thinking floor for the text-writing stages (draft, repair, revise) as well. Includes the model's reasoning: a thinking model spends output tokens before its first word of answer, and 8192 is the measured floor for the default models (spec §15). Raise this when a unit pauses with an exhausted output budget. |

## `[research]`

`[research]` — the whole configuration of the `research` stage (ADR-0116).

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `allowed_tools` | tuple[str, ...] | `['http_fetch', 'read_file']` | `IDEAPRESS_RESEARCH__ALLOWED_TOOLS` | The executor's allowlist. A shipped tool omitted here is refused `not_allowlisted` as a recorded result, never a startup failure; a name that is not a shipped tool is refused at startup, because it can only be a typo. |
| `allowed_hosts` | tuple[str, ...] | `[]` | `IDEAPRESS_RESEARCH__ALLOWED_HOSTS` | Hosts `http_fetch` may fetch from, compared case-insensitively. Empty — the default — means the tool is not registered at all, so a fresh installation fetches nothing until an operator names a host. |
| `max_fetch_bytes` | int | `1048576` | `IDEAPRESS_RESEARCH__MAX_FETCH_BYTES` | Largest document `http_fetch` will transfer. A body over it stops the transfer rather than truncating, because a note built from half a document cites a source that does not say what the note says. |
| `max_file_bytes` | int | `1048576` | `IDEAPRESS_RESEARCH__MAX_FILE_BYTES` | Largest file `read_file` will load from the project's `sources/` directory. A larger file is refused rather than partly read, for the same reason. |
| `timeout_seconds` | float | `30.0` | `IDEAPRESS_RESEARCH__TIMEOUT_SECONDS` | Per tool call. There is no way to express 'no timeout', deliberately. |
| `max_data_classification` | str | *(none)* | `IDEAPRESS_RESEARCH__MAX_DATA_CLASSIFICATION` | The most sensitive data a *remote* fetch target may receive: public | internal | confidential. Unset denies every remote host (fail closed, ADR-0054, ADR-0103 decision 2); a loopback host carries no ceiling and is approved. See `inference.loadcoach.max_data_classification`, whose rule this transcribes from the backend target to the fetch target. |

## `[providers]`

Egress policy. Remote inference is opt-in, per stage, and labelled in the UI.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `allow_remote` | bool | `false` | `IDEAPRESS_PROVIDERS__ALLOW_REMOTE` |  |

## `[pricing]`

``[pricing]`` — where a `baseaicore.ModelPricing` catalogue is read from (D3, ADR-0072).

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `file` | str | *(empty)* | `IDEAPRESS_PRICING__FILE` | Path to a JSON price catalogue in the format ADR-0072 defines. Empty means no prices are known; every debit accumulates tokens only and renders '—'. |

## `[budget]`

``[budget]`` — LoadLedger's two ceilings, and how a partial price counts (D1, D4, ADR-0069).

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `per_output_money_ceiling` | MoneyAmount | *(none)* | `IDEAPRESS_BUDGET__PER_OUTPUT_MONEY_CEILING` |  |
| `per_output_token_ceiling` | int | *(none)* | `IDEAPRESS_BUDGET__PER_OUTPUT_TOKEN_CEILING` |  |
| `per_project_money_ceiling` | MoneyAmount | *(none)* | `IDEAPRESS_BUDGET__PER_PROJECT_MONEY_CEILING` |  |
| `per_project_token_ceiling` | int | *(none)* | `IDEAPRESS_BUDGET__PER_PROJECT_TOKEN_CEILING` |  |
| `partial_pricing` | one of `floor` | `strict` | `"floor"` | `IDEAPRESS_BUDGET__PARTIAL_PRICING` | How a money ceiling treats a debit whose estimate did not total (ADR-0069). 'floor': the ceiling may fire late, by the unreported portion. 'strict': such a debit counts as exceeding, so the ceiling never binds late — at the cost of tripping on the first remote response a provider does not fully report. |

## `[logging]`

Structured logging. Project content is never logged at INFO or above (spec §14).

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `level` | one of `DEBUG` | `INFO` | `WARNING` | `ERROR` | `CRITICAL` | `"INFO"` | `IDEAPRESS_LOGGING__LEVEL` |  |
| `include_content` | bool | `false` | `IDEAPRESS_LOGGING__INCLUDE_CONTENT` | Store and log prompt and response text. Off by default: this is the user's private work, and hashes are enough for provenance. |
| `format` | one of `text` | `json` | `"text"` | `IDEAPRESS_LOGGING__FORMAT` |  |

## Refusals

Some values are refused at start-up rather than accepted and worked around, because silently
honouring one would produce a system the operator believes is configured differently from how it
behaves. Each refusal names the key.

| Configuration | Why it is refused |
|---|---|
| Non-loopback `server.host`, no `server.allowed_hosts` | Reachable from any page the user visits |
| `server.host = "0.0.0.0"` without `allow_lan_exposure` | The same exposure, spelled differently |
| `execution.max_concurrent_stages` above 1 | Two models on one GPU; IdeaPress has no queue |
| `inference.fallback_mode` naming no real mode | It reads as configured resilience and is none |
| `inference.fallback_mode` equal to `inference.mode` | A backend cannot fall back to itself |
| A `[models.stages]` key that is not a stage | It looks like a binding and binds nothing |
| A model-using stage with no `[models.stages]` binding | The stage would fail when it ran |
| `loadcoach.job_stages` naming a non-model stage | It would queue nothing and say nothing |

The first two are `INSECURE_BINDING` (ADR-0026); the third is ADR-0038. The rest are
`CONFIGURATION_ERROR`, raised before anything opens a socket.
