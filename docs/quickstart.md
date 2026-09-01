# IdeaPress — quickstart

From nothing to a finished, validated document. **Ollama is the only thing you need**; LoadCoach
and FreeWeight are optional and IdeaPress never asks for them.

## 1. Install

```bash
pip install ideapress
ideapress doctor
```

`doctor` tells you what it found and what to do about anything it did not. Nothing is required to
start: no configuration file, no backend, no network.

## 2. Pull the two default models

```bash
ollama pull qwen3.5:9b-q8_0     # structured work: requirements, plans, audits, critique
ollama pull gemma4:12b          # prose: drafting and revision
```

Two models rather than one because extraction and drafting reward different things. They are
never resident at the same time — IdeaPress unloads one before loading the other, because they do
not both fit on a 16 GB card with room for their context (ADR-0038).

## 3. Start it

```bash
ideapress serve
```

Loopback only, on port 8767. Open <http://127.0.0.1:8767>.

## 4. Write a brief that can be checked

This is the step that decides whether anything downstream is worth having. IdeaPress compiles your
brief into **requirements**, and a requirement is only as checkable as the sentence it came from:

> The article must state that inference runs entirely on the reader's own machine, that no document
> content is uploaded anywhere, and that the reader supplies the hardware. It must not use a
> marketing register.

Each of those becomes a requirement with a quotation from your own text beside it, so you can see
what was compiled and from where. A brief of adjectives compiles into requirements nothing can
check.

## 5. Plan, then read the plan

```bash
ideapress project create "Local inference for writers" --brief-file brief.md
ideapress plan build <project-id>
ideapress plan show <project-id>
```

The plan page pairs every requirement with the span of your material it came from. **Read it before
drafting.** A requirement the material does not support is visible as exactly that, and the plan
editor is there to fix the structure before any words are written.

## 6. Draft

```bash
ideapress stage run <project-id> draft
```

Each unit is drafted, validated by Python, audited by a model, critiqued, revised if it needs it,
and committed only when it passes. Watch it in the workspace, or follow the CLI.

**If a unit pauses**, the reason is on its page with the fix beside it. The most common one: a
reasoning model spends output tokens on its own thinking before its first word, and can exhaust the
budget before reaching one. Raise it:

```toml
[workflow]
structured_output_tokens = 16384
```

```bash
ideapress stage run <project-id> draft --resume
```

Other units are unaffected — a paused unit never stops the stage.

## 7. Export

```bash
ideapress export <project-id> --format markdown
```

Three formats: `markdown` (the document as a reader gets it), `html` (one self-contained file that
opens with no network at all) and `json` (everything, including the provenance of every attempt).
Exports are byte-identical for the same committed project, so two can be compared with `sha256sum`.

## What to read next

* [configuration.md](configuration.md) — every setting, generated from the code
* [workflows.md](workflows.md) — what the sixteen stages do and which ones a model cannot decide
* [backends.md](backends.md) — Ollama, LoadCoach and OpenAI-compatible endpoints
* [troubleshooting.md](troubleshooting.md) — every documented failure and its remedy
