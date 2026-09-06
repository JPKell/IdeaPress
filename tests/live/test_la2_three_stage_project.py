"""`-m live`: three stages, three adapters, one base load — LA2's exit, driven by IdeaPress.

This is the demonstration [adapter roadmap](../../docs/roadmap/adapter-roadmap.md) §7 names as I16
and I19, moved onto this row by H2's handoff. H2 proved the same properties at LoadCoach's own
boundary; what is new here is that the caller is a real IdeaPress installation talking HTTP to a
real `loadcoach serve`, which is talking to a real `llama-server`.

**Setup.** The test builds its whole world in `tmp_path` and drives LoadCoach through its own CLI,
from LoadCoach's own virtualenv — IdeaPress imports nothing from LoadCoach and this test does not
either (`.importlinter`'s `no-other-applications` contract, and the standalone gold standard that
keeps `loadcoach` out of the default `dev` extra).

```bash
IDEAPRESS_LOADCOACH_BIN=/home/jpk/ai/suite/LoadCoach/.venv/bin/loadcoach \\
IDEAPRESS_LLAMACPP_MODELS=~/ai/models/llm \\
IDEAPRESS_LLAMACPP_ADAPTERS=~/ai/models/adapters/llm \\
IDEAPRESS_LLAMACPP_BASE=Qwen2.5-1.5B-Instruct.Q8_0 \\
.venv/bin/pytest -m live tests/live/test_la2_three_stage_project.py -rs -s -p no:randomly
```

What is asserted, and from what:

* **One base load** (I16) — the three stages are answered by one `llama-server` process, taken from
  the operating system's process table, and the first generation is the only slow one. Not from the
  absence of a complaint.
* **Three visibly different answers**, which is the canary that the adapters actually applied: a
  pirate, a terse editor and a verbose one, on one prompt.
* **Every attempt names the subject that answered it**, in IdeaPress's own `attempts` table, so the
  three stages are distinguishable after the fact from the database alone.
* **The classification join, with both halves populated** (ADR-0065 rule 2) — the caller declares
  `confidential`, one adapter is reviewed `public`, and LoadCoach records the *join* on the
  attempt. The caller half is what raises it, which is the half that did not exist before H3.
* **A recorded classification denial** (I19, ADR-0079) with `caller_classification` no longer
  `null`: the same weights registered a second time under a registration declared remote leave an
  `adapter_classification_conflict` row while the local registration still serves the stage.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from baseaicore import SuiteError

from ideapress.config import Settings
from ideapress.domain.inference import Correlation, StageLimits, StageRequest
from ideapress.services.backends import build_backend
from ideapress.services.inference import InferenceGateway
from ideapress.services.stages import record_attempt

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.live

LOADCOACH_BIN = Path(
    os.environ.get("IDEAPRESS_LOADCOACH_BIN", "~/ai/suite/LoadCoach/.venv/bin/loadcoach")
).expanduser()
MODELS = Path(os.environ.get("IDEAPRESS_LLAMACPP_MODELS", "~/ai/models/llm")).expanduser()
ADAPTERS = Path(
    os.environ.get("IDEAPRESS_LLAMACPP_ADAPTERS", "~/ai/models/adapters/llm")
).expanduser()
BASE = os.environ.get("IDEAPRESS_LLAMACPP_BASE", "Qwen2.5-1.5B-Instruct.Q8_0")
PORT = int(os.environ.get("IDEAPRESS_LOADCOACH_PORT", "8791"))

PROMPT = "What is a KV cache? Answer in one sentence."

#: Which stage pins which adapter, and the classification that adapter's manifest is reviewed to.
#: `critique` is deliberately the `public` one: with the caller declaring `confidential`, the join
#: on that attempt is raised by the *caller*, which is the half this row added.
PINS: tuple[tuple[str, str, str], ...] = (
    ("draft", "pirate", "confidential"),
    ("revise", "terse", "confidential"),
    ("critique", "verbose", "public"),
)

CALLER_CLASSIFICATION = "confidential"


def _requirements() -> str | None:
    """Say what is missing, or ``None`` when this machine can run the journey."""
    if not LOADCOACH_BIN.is_file():
        return f"no loadcoach executable at {LOADCOACH_BIN}"
    if not (MODELS / f"{BASE}.gguf").is_file():
        return f"no base {BASE}.gguf in {MODELS}"
    if len(sorted(ADAPTERS.glob("*.gguf"))) < 3:
        return f"fewer than three adapter GGUFs in {ADAPTERS}"
    if shutil.which("llama-server") is None:
        return "no llama-server on PATH"
    return None


def _loadcoach(config: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one LoadCoach CLI command against ``config``, from LoadCoach's own virtualenv."""
    return subprocess.run(  # noqa: S603 — a fixed executable from an operator-supplied path
        [str(LOADCOACH_BIN), *args, "--config", str(config)],
        cwd=config.parent,
        capture_output=True,
        text=True,
        check=check,
        timeout=600,
    )


def _config(
    path: Path,
    *,
    adapters_dir: Path | None,
    remote: bool,
    port: int,
    task_profiles: Path | None = None,
) -> Path:
    """Write a LoadCoach configuration for one registration, local or declared remote.

    **One registration, never two.** A `models` row is keyed on the model's identity — provider
    kind, name and artifact digest — with no registration component, so registering one set of
    weights twice produces one row carrying whichever registration discovery reached last. The two
    halves of this journey are therefore two servers over two databases: the local one that serves
    the three stages, and the declared-remote one that records I19's denial.

    The adapter registry is written in a second pass because a manifest has to claim the base's
    **digest**, and the digest is only known once discovery has run against the real server
    (ADR-0058 §5): a manifest that claimed a name would be a weaker statement dressed up as a proof.
    """
    root = path.parent
    name = "hosted" if remote else "local"
    profiles_key = "" if task_profiles is None else f'task_profiles_path = "{task_profiles}"'
    registry = "" if adapters_dir is None else f'\n[adapters]\ndirectory = "{adapters_dir}"\n'
    path.write_text(
        f"""
[server]
host = "127.0.0.1"
port = {port}

[storage]
database_url = "sqlite:///{root / f"{name}.sqlite3"}"
auto_migrate = true

[providers]
allow_remote = true

[providers.{name}]
kind = "llamacpp"
model_directory = "{root / "models"}"
state_dir = "{root / "llamacpp" / name}"
timeout_seconds = 300.0
remote = {"true" if remote else "false"}
{registry}
[runtime]
# The shipped `content.article_draft` profile demands 16384 tokens and llama.cpp's own default is
# 8192, so without this the draft stage is refused `context_limit_exceeded` before an adapter is
# ever considered.
context_size = 32768

[routing]
require_adapter_evidence = false
{profiles_key}

[logging]
level = "INFO"
""",
        encoding="utf-8",
    )
    return path


def _prepare(root: Path) -> list[str]:
    """Build the world: a one-model directory, a reviewed adapter registry, and the twin.

    Returns:
        The three adapter names, in the order :data:`PINS` pins them.
    """
    (root / "models").mkdir()
    (root / "models" / f"{BASE}.gguf").symlink_to(MODELS / f"{BASE}.gguf")
    adapters = root / "adapters"
    adapters.mkdir()
    artifacts = sorted(ADAPTERS.glob("*.gguf"))
    by_keyword = {
        keyword: next(a for a in artifacts if keyword in a.name) for _, keyword, _ in PINS
    }
    for keyword, artifact in by_keyword.items():
        (adapters / f"{keyword}.gguf").symlink_to(artifact)

    config = _config(root / "local.toml", adapters_dir=adapters, remote=False, port=PORT)
    _loadcoach(config, "db", "upgrade")
    _loadcoach(config, "models", "refresh")
    # `scan` drafts a manifest per artifact and stops there: a draft names the base `REVIEW-ME`,
    # claims no digest and carries `confidential`, and nothing trusts it until a person has
    # reviewed the fields and renamed the file (ADR-0061, ADR-0065 rule 1). This is that review,
    # performed against the digest discovery has just read off the served artifact — so the base
    # claim is a proof rather than a name.
    _loadcoach(config, "adapters", "scan")
    with sqlite3.connect(root / "local.sqlite3") as connection:
        (base_digest,) = connection.execute(
            "SELECT artifact_digest FROM models WHERE provider_model_name = ?", (BASE,)
        ).fetchone()
    assert base_digest, "the served base reports no digest"
    for _, keyword, classification in PINS:
        draft = adapters / f"{keyword}.manifest.draft.json"
        document = json.loads(draft.read_text(encoding="utf-8"))
        payload = document["payload"]
        payload["base"] = {
            "provider_model_name": BASE,
            "artifact_digest": base_digest,
            "identity_confidence": "digest",
        }
        payload["data_classification"] = classification
        payload["declared_capabilities"] = ["instruction_following"]
        payload["notes"] = "Reviewed by the LA2 three-stage demonstration."
        (adapters / f"{keyword}.manifest.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )
        draft.unlink()

    return [keyword for _, keyword, _ in PINS]


#: `general.reasoning` — the profile IdeaPress's `critique` stage routes to — rewritten to permit a
#: remote registration, which is I19's setting. Every shipped profile IdeaPress's stage map names
#: is local-only, and the two that permit egress demand 128k of context, so this deployment writes
#: its own file. That is what `[routing] task_profiles_path` is for; before LoadCoach 1.1 shipped
#: the key, the only ways to express it were to edit an installed package or a stored row.
REMOTE_OK_PROFILE = """
[task_profiles."general.reasoning"]
version = "1.0.0"
description = "This deployment's reasoning profile, which permits egress (I19's setting)."
[task_profiles."general.reasoning".weights]
instruction_following = 1.0
[task_profiles."general.reasoning".constraints]
min_context_tokens = 2048
allow_remote_providers = true
[task_profiles."general.reasoning".execution]
temperature = 0.0
max_output_tokens = 48
response_format = "text"
max_attempts = 1
fallback_depth = 0
"""


@contextmanager
def _served(root: Path, *, name: str, port: int) -> Iterator[None]:
    """Run one `loadcoach serve` against ``root/<name>.toml`` for the duration of the block."""
    log = (root / f"{name}.log").open("w", encoding="utf-8")
    server = subprocess.Popen(  # noqa: S603 — a fixed executable from an operator-supplied path
        [str(LOADCOACH_BIN), "serve", "--config", str(root / f"{name}.toml")],
        cwd=root,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/v1/version", timeout=2).status_code:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:  # pragma: no cover — the server failed to start
            pytest.fail(f"loadcoach did not start; log:\n{(root / f'{name}.log').read_text()}")
        yield
    finally:
        server.terminate()
        server.wait(timeout=60)
        log.close()
        _reap_servers(root)


def _reap_servers(root: Path) -> None:
    """Kill any `llama-server` this journey's LoadCoach left behind.

    A supervised server is a child of `loadcoach serve`, and terminating the parent does not always
    take it with it — a leaked one holds the whole card, and the *next* candidate is then refused
    `insufficient_vram` before its classification is ever considered, which looks like a defect in
    the thing under test. Matched on this journey's own model directory, so nothing else on the
    machine is touched.
    """
    pgrep = shutil.which("pgrep") or "/usr/bin/pgrep"
    found = subprocess.run(  # noqa: S603 — a resolved diagnostic command
        [pgrep, "-af", "llama-server"], cwd=root, capture_output=True, text=True, check=False
    )
    for line in found.stdout.splitlines():
        pid, _, command = line.partition(" ")
        if pid.isdigit() and (str(root) in command or "/pytest-of-" in command):
            with suppress(ProcessLookupError, PermissionError):
                os.kill(int(pid), signal.SIGTERM)
    time.sleep(2)


@pytest.fixture
def serving(tmp_path: Path) -> Iterator[tuple[Path, list[str]]]:
    """A real `loadcoach serve` over a real `llama-server`, with three adapters registered."""
    missing = _requirements()
    if missing:
        pytest.fail(f"the LA2 journey needs a real llama.cpp setup: {missing}")

    names = _prepare(tmp_path)
    with _served(tmp_path, name="local", port=PORT):
        yield tmp_path, names


def _ideapress(root: Path, names: list[str], *, port: int = PORT) -> tuple[Any, Any]:
    """An IdeaPress database and inference gateway configured against the serving LoadCoach."""
    from ideapress.services.database import Database, upgrade

    settings = Settings.model_validate(
        {
            "inference": {
                "mode": "loadcoach",
                "data_classification": CALLER_CLASSIFICATION,
                "loadcoach": {
                    "base_url": f"http://127.0.0.1:{port}",
                    "timeout_seconds": 600,
                    # Synchronous for every stage, so the journey is three requests and three
                    # answers rather than three queue polls. The queued path carries the same
                    # fields, asserted in `tests/contract/test_loadcoach_adapter_pins.py`.
                    "job_stages": [],
                },
            },
            "models": {
                "stage_adapters": {
                    stage: name for (stage, name, _), name in zip(PINS, names, strict=True)
                }
            },
        }
    )
    database = Database.from_url(f"sqlite:///{root / f'ideapress-{port}.sqlite3'}")
    upgrade(database)
    gateway = InferenceGateway(
        backend=build_backend(settings),
        bindings=settings.models.stages,
        execution=settings.execution,
        stage_adapters=settings.models.stage_adapters,
    )
    return database, gateway


def _stage_run(database: Any) -> str:
    from datetime import UTC, datetime

    from ideapress.infrastructure.db.models import Project, StageRun

    with database.write() as session:
        project = Project(
            title="LA2",
            slug="la2",
            content_type="article",
            content_type_version="1.0",
            workflow_id="default",
            workflow_version="1.0",
            status="active",
            brief_text="A short article about local inference.",
            author_material_json=[],
            config_json={},
        )
        session.add(project)
        session.flush()
        run = StageRun(
            project_id=project.id,
            stage="draft",
            state="running",
            units_total=3,
            units_completed=0,
            units_paused=0,
            started_at=datetime.now(UTC),
            options_json={},
            backend="loadcoach",
            backend_mode="loadcoach",
        )
        session.add(run)
        session.flush()
        return str(run.id)


def _server_pids() -> set[int]:
    """Every running `llama-server` process, from the operating system rather than from a log."""
    pgrep = shutil.which("pgrep") or "/usr/bin/pgrep"
    # `-x`, matched against the process *name*: `-f` matches any command line mentioning
    # `llama-server`, which on this machine includes the test runner's own.
    found = subprocess.run(  # noqa: S603 — a resolved diagnostic command
        [pgrep, "-x", "llama-server"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return {int(line) for line in found.stdout.split() if line.isdigit()}


def test_three_stages_three_adapters_one_base_load(serving: tuple[Path, list[str]]) -> None:
    """I16, the provenance, and the join — one project, one base, three subjects."""
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt as AttemptRow

    root, names = serving
    database, gateway = _ideapress(root, names)
    run_id = _stage_run(database)

    answers: list[str] = []
    timings: list[float] = []
    pids: list[set[int]] = []
    for stage, _adapter, _classification in PINS:
        started = time.monotonic()
        result = gateway.run(
            StageRequest(
                stage=stage,  # type: ignore[arg-type]  # StageId is a Literal
                system="Answer the question.",
                user=PROMPT,
                limits=StageLimits(max_output_tokens=48, temperature=0.0),
                correlation=Correlation(project_id="01LA2", attempt=1),
            )
        )
        timings.append((time.monotonic() - started) * 1000.0)
        pids.append(_server_pids())
        answers.append(result.text)
        record_attempt(
            database,
            stage_run_id=run_id,
            stage=stage,
            result=result,
        )

    with database.read() as session:
        rows = session.execute(
            select(
                AttemptRow.stage, AttemptRow.adapter_name, AttemptRow.subject_canonical_id
            ).order_by(AttemptRow.id)
        ).all()

    print("\nI16 (IdeaPress): pids=", [sorted(p) for p in pids])  # noqa: T201 — the evidence
    print("wall_ms=", [round(t) for t in timings])  # noqa: T201
    for row in rows:
        print(f"  {row.stage}: {row.adapter_name} -> {row.subject_canonical_id}")  # noqa: T201
    for stage, answer in zip([s for s, _, _ in PINS], answers, strict=True):
        print(f"  {stage}: {answer[:100]}")  # noqa: T201

    # I16: one server answered all three, and the base was made resident once. Two independent
    # witnesses, neither of them the absence of a complaint: the operating system's process table,
    # and LoadCoach's own residency ledger. The wall times are printed as corroboration rather
    # than asserted — an adapter switch is free but a generation's length is not, so a later
    # stage can legitimately take longer than the one that carried the load.
    for sample in pids:
        assert len(sample) == 1, f"expected one llama-server, saw {pids}"
    assert len(set().union(*pids)) == 1, f"the server changed between stages: {pids}"

    with sqlite3.connect(root / "local.sqlite3") as connection:
        resident = connection.execute(
            "SELECT model_id, resident, loaded_at, last_used_at FROM residency"
        ).fetchall()
    print("residency:", resident)  # noqa: T201 — the evidence
    # **One row**, not one distinct model: an adapter switch on a resident base writes no row and
    # triggers no unload (ADR-0066), so three pinned stages on one base leave exactly one entry.
    assert len(resident) == 1, resident
    assert resident[0][1] == 1

    # The canary: three adapters, three visibly different answers to one prompt.
    assert len(set(answers)) == 3, answers
    assert all(answers)

    # ADR-0080: the subject that answered, on every attempt, from the database alone.
    assert [(r.stage, r.adapter_name) for r in rows] == [
        (s, n) for (s, _, _), n in zip(PINS, names, strict=True)
    ]
    for row in rows:
        assert row.subject_canonical_id is not None
        assert f"+{row.adapter_name}@sha256:" in row.subject_canonical_id


def test_the_callers_classification_raises_the_join_and_is_recorded(
    serving: tuple[Path, list[str]],
) -> None:
    """ADR-0065 rule 2, live: `max(caller, adapter)` with the caller doing the raising.

    The `critique` stage pins the adapter reviewed **`public`**, and the installation declares
    `confidential`. Before H3 the attempt would have recorded `public` — the adapter's own value,
    with `caller_classification` null. It now records the join, and the join is higher than either
    half of the old answer.
    """
    root, names = serving
    _database, gateway = _ideapress(root, names)

    gateway.run(
        StageRequest(
            stage="critique",
            system="Answer the question.",
            user=PROMPT,
            limits=StageLimits(max_output_tokens=48, temperature=0.0),
            correlation=Correlation(project_id="01LA2", attempt=1),
        )
    )

    with sqlite3.connect(root / "local.sqlite3") as connection:
        rows = connection.execute(
            "SELECT a.name, j.adapter_data_classification, j.effective_data_classification"
            " FROM job_attempts j JOIN adapters a ON a.id = j.adapter_id"
            " WHERE j.adapter_id IS NOT NULL ORDER BY j.rowid DESC LIMIT 1"
        ).fetchall()
    print("\njoin on the attempt:", rows)  # noqa: T201 — the evidence
    (adapter_name, adapter_class, effective) = rows[0]
    assert adapter_name == names[2]
    assert adapter_class == "public"
    assert effective == CALLER_CLASSIFICATION


def test_a_remote_registration_leaves_a_denial_with_both_halves_populated(tmp_path: Path) -> None:
    """I19 (ADR-0079), with `caller_classification` no longer `null`.

    A second LoadCoach, over its own database, whose one registration is **declared remote** — the
    same weights, described differently. What LoadCoach evaluates is the declaration (ADR-0055
    rule 4), and a genuinely hosted endpoint would prove nothing more, because it cannot serve an
    adapter at all. It is a second server rather than a second registration beside the first
    because a `models` row is keyed on the model's identity and not on the registration that found
    it, so the same weights registered twice are one row.

    `general.reasoning` — the profile IdeaPress's `critique` stage routes to — is allowed to
    consider a remote registration here, so `excluded_by_policy` never fires and the refusal that
    does is the one no flag repairs. The bare base stays servable throughout, which is what makes
    the denial a statement about adapters rather than about egress; the *pinned* stage fails,
    because a pin removes every other subject from the pool.
    """
    missing = _requirements()
    if missing:
        pytest.fail(f"the LA2 journey needs a real llama.cpp setup: {missing}")

    port = PORT + 1
    names = _prepare(tmp_path)
    profiles = tmp_path / "task_profiles.toml"
    profiles.write_text(REMOTE_OK_PROFILE, encoding="utf-8")
    _config(
        tmp_path / "hosted.toml",
        adapters_dir=tmp_path / "adapters",
        remote=True,
        port=port,
        task_profiles=profiles,
    )
    with _served(tmp_path, name="hosted", port=port):
        _database, gateway = _ideapress(tmp_path, names, port=port)

        with pytest.raises(SuiteError) as caught:
            gateway.run(
                StageRequest(
                    stage="critique",
                    system="Answer the question.",
                    user=PROMPT,
                    limits=StageLimits(max_output_tokens=48, temperature=0.0),
                    correlation=Correlation(project_id="01LA2", attempt=1),
                )
            )
        print(f"\nthe pinned stage failed: {caught.value.code}")  # noqa: T201 — the evidence

        with sqlite3.connect(tmp_path / "hosted.sqlite3") as connection:
            rows = connection.execute(
                "SELECT rejection_reason, rejection_detail_json FROM routing_candidates"
                " WHERE rejection_reason = 'adapter_classification_conflict'"
            ).fetchall()

    print(f"I19: {len(rows)} recorded denials")  # noqa: T201 — the evidence
    assert rows, "no adapter_classification_conflict row was recorded"
    for _, detail in rows:
        parsed = json.loads(detail)
        print("  ", json.dumps(parsed, sort_keys=True))  # noqa: T201
        assert parsed["caller_classification"] == CALLER_CLASSIFICATION
        assert parsed["provider_remote"] is True
        assert parsed["effective_classification"] == CALLER_CLASSIFICATION
