# IdeaPress — troubleshooting

Every documented failure, what it means, and what to do. `ideapress doctor` checks most of these
before you hit them.

## Start here

```bash
ideapress doctor
```

It names the configuration file it read, whether your data directory is writable, whether the
database is at head, whether your prompts match their manifest, whether every model-using stage has
a binding, and whether your backend answers. An unreachable backend is a **warning**, not a
failure: the application is designed to be useful without one.

## A unit paused and I do not know why

The reason is on the unit's page in the workspace, with the remedy beside it. The most common one:

> the model produced no text at all in 8192 output tokens, twice

A reasoning model spends output tokens on its own thinking before its first word. A budget that
looks generous can be exhausted before it reaches one. One knob fixes it for every stage:

```toml
[workflow]
structured_output_tokens = 16384    # default 8192, accepted range 1024–131072
```

```bash
ideapress stage run <project-id> draft --resume
```

Other units are unaffected — a paused unit never stops the stage. This is documented behaviour, not
a defect: pause, raise, resume.

## The error codes

| Code | What happened | What to do |
|---|---|---|
| `BACKEND_UNAVAILABLE` | The backend did not answer at all | Start it. Or set `inference.fallback_mode`. Committed work is untouched |
| `BACKEND_VERSION_MISMATCH` | LoadCoach speaks a different API major | The message names both versions. Upgrade whichever is older |
| `MODEL_NOT_CONFIGURED` | A stage has no `[models.stages]` binding | The message names the stage and the key. `ideapress config init` writes a full example |
| `PROVIDER_TIMEOUT` | It accepted the request and did not answer in time | Raise `timeout_seconds`, or use a smaller model. Different from being down |
| `CONTEXT_LIMIT_EXCEEDED` | The request exceeded what the model serves | Usually the output budget above. The message carries the numbers |
| `INSUFFICIENT_VRAM` | The preflight found less free VRAM than the model needs with its context | The message carries **both** figures. Close what is holding the card. Only raised with `ideapress[telemetry]` |
| `VALIDATION_FAILED` | Deterministic checks did not pass | The report names each one. Repair runs automatically; a unit pauses after `max_attempts_per_stage` |
| `REQUIREMENTS_UNMET` | A blocking requirement is not satisfied | The coverage report names it, and whether a check or an audit was deciding |
| `REVISION_LIMIT_REACHED` | Revision hit its round limit or its diminishing-returns floor | The record says which. Raise `max_revision_rounds`, or accept it |
| `CONTENT_REJECTED` | The model refused the task | **Not a failure.** Its stated reason is surfaced; rephrase, or bind a different model |
| `STAGE_ALREADY_RUNNING` | One stage runs per project at a time | Wait, or `ideapress stage cancel` |
| `STAGE_PRECONDITION_FAILED` | A stage was asked for out of order | Run the earlier stage first; the message says which |
| `EXPORT_FAILED` | The render or the write failed | Check disk space and the format name |
| `PROJECT_NOT_FOUND` / `UNIT_NOT_FOUND` | No such thing | `ideapress project list` |
| `SCHEMA_VERSION_UNSUPPORTED` | An archive from a newer IdeaPress | The message names both versions |
| `INSECURE_BINDING` | A non-loopback bind with no `allowed_hosts`, or `0.0.0.0` without acknowledgement | Set `server.allowed_hosts`, and terminate TLS in front of it |

## "It will not start"

It should always start. Nothing is required: no configuration file, no backend, no network. If it
refuses, it is one of the configuration refusals above, and the message names the key. Everything
else is a runtime condition the application is designed to survive.

## The plan looks wrong

Read the plan page before drafting: every requirement is shown with the span of *your* material it
was compiled from. If a requirement is not supported by the quotation beside it, the brief is the
problem, not the plan.

Fix the structure with the plan editor — reorder, split, merge, reassign, rewrite goals. Every edit
re-checks the whole plan, and one that would leave a blocking requirement with no unit responsible
for it is refused with the requirement named. Structural edits stop once a unit holds committed
text: finished work is never renumbered out from under itself.

## Exports differ between runs

They should not. The same committed project exports byte-identically:

```bash
ideapress export <id> --format markdown && sha256sum <path>
```

If two differ, the project changed between them — a revision, a new commit. `ideapress unit history`
shows what.

## Model output looks dangerous

It is stored verbatim and rendered inert everywhere: every page escapes it, the HTML export escapes
it, the JSON export carries it as a string value. It is never executed, never used to build a path,
and never passed to a template as markup. A path-traversal string is the one payload that is
*blocking* — it stops the commit rather than being rendered harmlessly.

## An archive will not import

`ideapress project import <path> --inspect` reports what it found and writes nothing. Refusals:
entries escaping the directory, symlinks or hardlinks, device files, too many entries, an entry too
large, too large in total, or a compression ratio past the bomb cap. Nothing is written until every
check has passed, so a refused import leaves no directory and no row.

## LoadCoach is configured and nothing routes

* `ideapress doctor` reports whether every stage's task profile exists on the running LoadCoach.
* `NO_ELIGIBLE_MODEL` from LoadCoach usually means it has discovered no models
  (`POST /api/v1/models/discover`) or that something else is holding the GPU — its admission
  control refuses to oversubscribe the card, which is it working correctly.
* Turning LoadCoach off mid-project leaves the project resumable. Committed units stay committed.
