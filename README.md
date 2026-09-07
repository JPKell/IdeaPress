# IdeaPress

Turns an idea into finished content through configurable, Python-controlled workflows in which models perform bounded, validated tasks.

**Status:** `1.2.0`, on PyPI — 10 phases built through M8's 1.0 and the LA2/M13 rows beyond it:
requirements, plan, draft/validate/repair/commit, audit and bounded revision, exports, an optional
LoadCoach backend, per-stage adapter pins, and now per-unit/per-project cost (LoadLedger), a
recorded egress decision (Commissioner) and CutCtx-driven stage context compaction — see
[development plan](docs/apps/ideapress/development-plan.md) for what each phase adds.

Part of the **Local AI Suite**.

## Install

```bash
pip install ideapress
ideapress serve
```

Starts on `127.0.0.1:8767` with zero configuration. See [docs/apps/ideapress/spec.md](docs/apps/ideapress/spec.md) §12 for the full configuration surface and `IDEAPRESS_*` environment variables.

## Quickstart

```bash
pip install ideapress
ideapress serve            # starts the web UI + API on 127.0.0.1:8767
ideapress health --json     # same health data the API reports, from the CLI
ideapress --help
```

## Documentation

Project documentation lives under [`docs/`](docs/README.md). Start with [`docs/README.md`](docs/README.md).

| Read this | For |
|---|---|
| [docs/apps/ideapress/spec.md](docs/apps/ideapress/spec.md) | Purpose, scope, non-goals, public contracts, configuration, acceptance criteria |
| [docs/apps/ideapress/development-plan.md](docs/apps/ideapress/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -m "not live and not performance"
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and [`SECURITY.md`](SECURITY.md) for
how to report a vulnerability.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
