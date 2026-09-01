# IdeaPress — backends

Three ways to reach a model. Switching between them is a **configuration change and nothing else**:
no workflow code differs, and a backend-parity test runs the identical workflow across all four
adapters (the three below plus a capability-poor fake) and asserts the same structure comes out.

## Ollama — the default

```toml
[inference]
mode = "ollama"

[inference.ollama]
base_url = "http://127.0.0.1:11434"
timeout_seconds = 300
```

IdeaPress chooses the model per stage, and unloads one before loading another so two are never
resident at once (ADR-0038). This is the configuration the product is guaranteed to work in: a
complete workflow, with nothing else installed.

## LoadCoach — optional

```toml
[inference]
mode = "loadcoach"

[inference.loadcoach]
base_url = "http://127.0.0.1:8766"
timeout_seconds = 600
```

LoadCoach routes each stage to a model by **task profile**, using its own measured capability
evidence, its reliability statistics and its admission control. IdeaPress supplies the task, not
the model.

What changes when you turn it on:

* **`[models.stages]` is ignored.** LoadCoach chooses. Set `honour_stage_bindings = true` to send
  your binding as an override instead — which pins the model and gives up the routing, the evidence
  and the reliability data for that stage. If LoadCoach routes elsewhere anyway, the attempt records
  a `model_override_not_honoured` degradation naming both models. A pin is a request, not a
  guarantee.
* **Long stages go through its queue.** `job_stages` (draft, revise, repair, project_review by
  default) are submitted asynchronously; everything else is synchronous, so you are never waiting
  behind background work.
* **Structured output is not enforced by LoadCoach on IdeaPress's behalf.** LoadCoach applies its
  *task profile's* schema, not the caller's, so IdeaPress asks for JSON and validates the shape
  itself. Every affected attempt records a `structured_output_unavailable` degradation saying so.
  Output is equivalent, because IdeaPress validated it either way.
* **Feedback goes back.** After a unit commits, its acceptance and validation result are posted to
  the job, once, and feed LoadCoach's reliability statistics.

Every attempt records the routing decision: its id, its score and its flags. `low_evidence` means
the decision rested on little measured evidence; `assumed_context` means the served context could
not be established, and a later context overflow is a consequence of that rather than a surprise.

## OpenAI-compatible endpoints

```toml
[inference]
mode = "openai_compatible"

[inference.openai_compatible]
base_url = "https://api.example.com/v1"
api_key_env = "MY_API_KEY"     # the variable's *name*, never the key itself
model = "some-model"
```

Reduced capabilities, honestly reported: no residency control, and structured output depends
entirely on the server behind the URL. **A non-loopback endpoint is remote**, and IdeaPress labels
it as egress on the backends page and in the workspace — your drafts leave the machine, and it says
so before you press the button.

`providers.allow_remote = false` (the default) refuses remote endpoints outright.

## When a backend is not there

Never a startup failure. Opening projects, reading committed units and exporting them need no model
at all, so an unreachable backend is a health component and a stage-level error.

```toml
[inference]
fallback_mode = "ollama"   # used when the primary does not answer
pin_backend = false        # true = fail the stage instead of falling back
```

| What happened | What IdeaPress does |
|---|---|
| Primary unreachable, fallback set, not pinned | Runs on the fallback and records `backend_fallback` naming both |
| Primary unreachable, pinned | Fails the stage with `BACKEND_UNAVAILABLE`; the project is untouched and resumable |
| LoadCoach API major differs | `BACKEND_VERSION_MISMATCH` naming both versions. No silent downgrade |
| Backend cannot enforce a schema | Asks for text, parses it, records the degradation. Never claims a schema was enforced |
| LoadCoach queued the work | The wait is on the attempt and shown in the UI |
| LoadCoach is up but can serve nothing (`NO_ELIGIBLE_MODEL`, `QUEUE_FULL`, …) | Treated as unavailable, **not** as a content rejection: it falls back if a fallback is set, and never commits an empty unit |

### On a single GPU, configure a fallback

**Observed against LoadCoach 1.0.0 on one 16 GB card.** IdeaPress's stages use different LoadCoach
task profiles seconds apart — `draft` wants `content.article_draft`, `audit_fast` wants
`content.review`, `critique` wants `general.reasoning` — and those profiles do not all resolve to
the same model. Once LoadCoach has loaded a model for one profile, a request for another is
refused `NO_ELIGIBLE_MODEL`: its admission check charges the full weight of a model that is
*already resident* against free VRAM, and nothing evicts to make room. The queue does not help;
routing refuses before a job reaches `waiting_resources`.

The practical consequence is simple:

```toml
[inference]
mode = "loadcoach"
fallback_mode = "ollama"   # on one GPU, treat this as required rather than optional
pin_backend = false
```

With the fallback set, a stage LoadCoach cannot serve runs on Ollama and records
`backend_fallback`; the project finishes either way. With `pin_backend = true` and no fallback, the
second stage of a project will fail on a single-GPU machine. A host with enough VRAM for two
resident models, or a LoadCoach that credits residency, does not have this problem.

## Checking one

```bash
ideapress backend list          # every configured backend, its reachability and its egress flag
ideapress backend test          # a round trip: health, latency, model list
ideapress doctor                # everything above plus the configuration around it
```
