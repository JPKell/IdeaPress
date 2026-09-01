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

## `[inference.openai_compatible]`

Any OpenAI-compatible endpoint. Empty base_url means "not configured", not "localhost".

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `base_url` | str | *(empty)* | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__BASE_URL` |  |
| `api_key_env` | str | *(empty)* | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__API_KEY_ENV` |  |
| `timeout_seconds` | int | `300` | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__TIMEOUT_SECONDS` |  |
| `model` | str | *(empty)* | `IDEAPRESS_INFERENCE__OPENAI_COMPATIBLE__MODEL` | The model name this endpoint serves. OpenAI-compatible servers expose one namespace with no provider prefix, so the `[models.stages]` bindings do not apply to it. |

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
| `allow_audit_gated_requirements` | bool | `true` | `IDEAPRESS_WORKFLOW__ALLOW_AUDIT_GATED_REQUIREMENTS` | Whether an audit's explicit per-requirement attestation may satisfy a blocking requirement that has no deterministic check (ADR-0039). Silence never satisfies one either way. False forces a wholly mechanical gate: such a requirement pauses its unit until it gets a deterministic check or is demoted to advisory. |
| `structured_output_tokens` | int | `8192` | `IDEAPRESS_WORKFLOW__STRUCTURED_OUTPUT_TOKENS` | Output-token budget for the structured stages (requirements, outline, audit_fast, audit_deep, critique, project_review), and — when raised above the 8192 default — the thinking floor for the text-writing stages (draft, repair, revise) as well. Includes the model's reasoning: a thinking model spends output tokens before its first word of answer, and 8192 is the measured floor for the default models (spec §15). Raise this when a unit pauses with an exhausted output budget. |

## `[providers]`

Egress policy. Remote inference is opt-in, per stage, and labelled in the UI.

| Key | Type | Default | Environment variable | Notes |
| --- | --- | --- | --- | --- |
| `allow_remote` | bool | `false` | `IDEAPRESS_PROVIDERS__ALLOW_REMOTE` |  |

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
