# IdeaPress

Turns an idea into finished content through configurable, Python-controlled workflows in which models perform bounded, validated tasks.

**Status:** `1.3.3` in the repository (tagged `v1.3.3`; `pip index versions ideapress` says what PyPI serves). 10 phases built through M8's 1.0 and the LA2/M13 rows beyond it:
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

## Compatibility

Declared version ranges from `pyproject.toml` — kept from drifting by
`tests/unit/test_readme_compatibility.py`, which parses the file and fails if this table disagrees:

| Package | Range |
|---|---|
| `baseaicore` | `>=0.4.2,<0.5` |
| `setspec` | `>=0.5,<0.7` |
| `modelrack` | `>=0.7,<0.8` |
| `weightsdb` | `>=0.2,<0.3` |
| `mirrorwall` | `>=0.2.2,<0.3` |
| `loadledger` | `>=0.3,<0.4` |
| `commissioner` | `>=0.1.1,<0.2` |
| `cutctx` | `>=0.1,<0.2` |
| `toolyard` | `>=0.1.1,<0.2` |
| `sweatmeter` | `>=0.4,<0.5` |

`loadledger` and `commissioner` are installed with their `[sql]` extra (not optional in practice:
both mount unconditionally in `infrastructure/db/models.py`). `toolyard` is how the `research`
stage fetches and reads (ADR-0116); nothing else in IdeaPress touches it. `sweatmeter` is the
optional
`[telemetry]` extra — never a hard dependency — and gates only `doctor`'s VRAM-preflight capability
flag; IdeaPress shows no machine telemetry (ADR-0115).

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
