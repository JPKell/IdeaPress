"""`-m live`: how reliably does an audit actually attest? — ADR-0039's required measurement.

[ADR-0039](../../docs/adr/0039-audit-gated-blocking-requirements.md) was accepted as option (b):
an audit's explicit `met` may satisfy a blocking requirement that has no deterministic check, and
its silence may not. Its Consequences make one obligation of M8 — *measure attestation reliability
on the default models before building further on the coverage gate* — because option (b) is only
sound if the model's verdicts are actually informative. A model that says `met` to everything is a
rubber stamp with extra steps; a model that omits the array is a gate nothing can pass.

**This is an experiment, not a feature.** It changes no behaviour and asserts almost nothing. It
runs `audit_fast` N times over one fixture unit carrying three check-less requirements of known
status and counts what comes back:

| Requirement | Known status | A useful model says |
|---|---|---|
| R-001 | plainly **met** — the text states it in as many words | `met` |
| R-002 | plainly **violated** — the text states the opposite | `not_met` |
| R-003 | genuinely **unjudgeable** from the text alone | `cannot_judge`, or omits it |

What matters is R-001's `met` rate (can a true requirement pass?), R-002's `not_met` rate (does a
false one get caught, or rubber-stamped?), and how often the array comes back empty or absent (does
the mechanism reach the model at all?). The numbers go in the handoff with a recommendation.

**The gate is not changed on the strength of this run.** A bad number is a finding for the human,
which is what ADR-0039's option (a) is kept on the record for.

```bash
IDEAPRESS_ATTESTATION_RUNS=20 .venv/bin/pytest -m live -s \\
    tests/live/test_attestation_reliability.py
```
"""

from __future__ import annotations

import os
from collections import Counter
from typing import TYPE_CHECKING

import pytest

from ideapress.config import load_settings
from ideapress.domain.requirements import CompiledBy, Requirement, SourceReference
from ideapress.infrastructure.backends.ollama import OllamaBackend
from ideapress.services.inference import InferenceGateway
from ideapress.services.review import run_audit

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.live

RUNS = int(os.environ.get("IDEAPRESS_ATTESTATION_RUNS", "20"))
MODEL = os.environ.get("IDEAPRESS_ATTESTATION_MODEL", "ollama/qwen3.5:9b-q8_0")

UNIT_TEXT = """
Everything runs on your own machine. The model is loaded from local storage, inference happens on
your own GPU, and no part of what you write is uploaded to any server at any point. There is no
account to create and no network connection is required after the model has been downloaded.

The trade is that you supply the hardware. A 16 GB card runs the default models comfortably; a
smaller one will need a smaller model, and the difference in quality is real.
""".strip()

# The three requirements, each check-less on purpose: `checks=()` is what routes them to the audit
# rather than to Python, which is the mechanism under measurement.
_COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.1.0")


def _requirement(key: str, text: str, quote: str) -> Requirement:
    """One check-less blocking requirement: `checks=()` is what routes it to the audit."""
    return Requirement(
        key=key,
        text=text,
        blocking=True,
        source=SourceReference(document="brief", quote=quote),
        compiled_by=_COMPILED_BY,
        checks=(),
        unit_keys=("U-01",),
    )


REQUIREMENTS: tuple[Requirement, ...] = (
    _requirement(
        "R-001",
        "The unit must be explicit about where inference happens.",
        "inference runs on the reader's own machine",
    ),
    _requirement(
        "R-002",
        "The unit must state that the reader needs no hardware of their own.",
        "needs no hardware of their own",
    ),
    _requirement(
        "R-003",
        "The unit must be consistent with the pricing page published in March 2024.",
        "consistent with the pricing page",
    ),
)

KNOWN_STATUS = {"R-001": "met", "R-002": "not_met", "R-003": "cannot_judge"}


@pytest.fixture
def gateway() -> InferenceGateway:
    settings = load_settings().settings
    backend = OllamaBackend(settings.inference.ollama)
    if backend.health().status != "ok":
        pytest.skip("no Ollama at the configured URL")
    bindings = settings.models.stages.model_copy(update={"audit_fast": MODEL})
    return InferenceGateway(backend=backend, bindings=bindings, execution=settings.execution)


def _verdicts(gateway: InferenceGateway, budget: int) -> dict[str, str]:
    """One `audit_fast` run's verdicts, with an omitted key read as `cannot_judge`.

    Returns:
        A verdict for each of the three requirements. An absent key becomes ``cannot_judge``,
        which is exactly how the coverage gate reads it (ADR-0039) — so the measurement counts
        what the gate would count, not what the model literally emitted.
    """
    outcome = run_audit(
        gateway,
        stage="audit_fast",
        project_id="01ATTESTATION",
        unit_key="U-01",
        content=UNIT_TEXT,
        requirements=REQUIREMENTS,
        structured_output_tokens=budget,
    )
    attested = dict(outcome.report.requirement_verdicts)
    return {key: attested.get(key, "cannot_judge") for key in KNOWN_STATUS}


def test_measure_attestation_reliability(gateway: InferenceGateway) -> None:
    """Run the experiment and print the table. Asserts only that the mechanism is reachable.

    The single assertion is deliberately weak: that at least one run produced *some* verdict. It is
    there so a total failure of the mechanism — a prompt that no longer asks for the array, a
    schema that no longer permits it — fails rather than printing zeros nobody reads. Everything
    else is a number for the handoff.
    """
    settings = load_settings().settings
    budget = settings.workflow.structured_output_tokens

    per_requirement: dict[str, Counter[str]] = {key: Counter() for key in KNOWN_STATUS}
    empty_arrays = 0
    errors: list[str] = []

    for index in range(RUNS):
        try:
            verdicts = _verdicts(gateway, budget)
        except Exception as exc:  # noqa: BLE001 — a failed run is data, not a broken experiment
            errors.append(f"run {index + 1}: {type(exc).__name__}: {exc}")
            continue
        if all(value == "cannot_judge" for value in verdicts.values()):
            empty_arrays += 1
        for key, verdict in verdicts.items():
            per_requirement[key][verdict] += 1

    _report(per_requirement, empty_arrays=empty_arrays, errors=errors, budget=budget)

    attested_at_all = sum(
        count
        for counter in per_requirement.values()
        for verdict, count in counter.items()
        if verdict != "cannot_judge"
    )
    assert attested_at_all > 0, (
        "no run produced a single explicit verdict; the attestation mechanism is not reaching the "
        "model at all (check the audit prompt version and FINDINGS_SCHEMA)"
    )


def _report(
    per_requirement: dict[str, Counter[str]],
    *,
    empty_arrays: int,
    errors: Sequence[str],
    budget: int,
) -> None:
    """Print the measurement in the shape the handoff wants it."""
    completed = RUNS - len(errors)
    lines = [
        "",
        "=" * 78,
        "P7-B — ADR-0039 attestation reliability",
        "=" * 78,
        f"model                : {MODEL}",
        "stage                : audit_fast (prompt 1.1.0)",
        f"output budget        : {budget} tokens",
        f"runs requested       : {RUNS}",
        f"runs completed       : {completed}",
        f"runs that errored    : {len(errors)}",
        f"runs attesting none  : {empty_arrays} ({_percent(empty_arrays, completed)} of completed)",
        "",
        f"{'requirement':<12}{'known':<14}{'met':>6}{'not_met':>9}{'cannot_judge':>14}"
        f"{'agrees':>9}",
        "-" * 78,
    ]
    for key, known in KNOWN_STATUS.items():
        counter = per_requirement[key]
        agrees = counter[known]
        lines.append(
            f"{key:<12}{known:<14}{counter['met']:>6}{counter['not_met']:>9}"
            f"{counter['cannot_judge']:>14}{_percent(agrees, completed):>9}"
        )
    lines += [
        "-" * 78,
        "",
        "Reading it: R-001's `met` rate is whether a true requirement can pass at all. R-002's",
        "`not_met` rate is whether a false one is caught rather than rubber-stamped — a low number",
        "here is the finding that matters, because it is the case where option (b) lets a model",
        "clear a gate it should have held. R-003 is the honesty check: `cannot_judge` is the",
        "correct answer and `met` is the dangerous one.",
    ]
    if errors:
        lines += ["", "errors:", *[f"  {error}" for error in errors[:10]]]
    lines.append("=" * 78)
    print("\n".join(lines))  # noqa: T201


def _percent(count: int, total: int) -> str:
    return f"{(100.0 * count / total):.0f}%" if total else "n/a"
