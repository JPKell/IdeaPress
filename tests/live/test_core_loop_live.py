"""`-m live`: P4's acceptance criteria against a real Ollama model.

AC1 is "a unit is drafted, validated and committed against a real Ollama model", and no offline
test can make that claim. The scripted tests prove the *machinery*; this proves the machinery works
against a model that was not told what to say.
"""

from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING

import pytest

from ideapress.config import load_settings
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.live

BRIEF = """
# Local inference for writers

The article must state that inference runs entirely on the reader's own machine. It must state
that no document content is uploaded anywhere. Keep each section short.
""".strip()


@pytest.fixture
def runtime() -> Iterator[Runtime]:
    built = build_runtime(load_settings().settings)
    if built.backend is None or built.backend.health().status != "ok":
        built.close()
        pytest.skip("no Ollama at the configured URL")
    yield built
    built.close()


def _wait(runtime: Runtime, task_id: str, *, timeout: float = 900.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(task_id):
            return runtime.runner.run_state(task_id) or "unknown"
        time.sleep(0.5)
    message = f"stage {task_id} did not finish within {timeout}s"
    raise AssertionError(message)


def test_a_unit_is_drafted_validated_and_committed_against_a_real_model(
    runtime: Runtime,
) -> None:
    """P4 AC1 and AC3, end to end, with nothing scripted."""
    from ideapress.services.stage_bodies import start_plan, start_stage
    from ideapress.services.unit_reports import unit_detail, unit_list

    project = runtime.projects.create(title="Local inference for writers", brief=BRIEF)
    plan = start_plan(runtime, project_id=project.id)
    assert _wait(runtime, plan.run_id) == "completed", "the plan stage failed"

    from ideapress.services.stage_reports import plan_report

    report = plan_report(runtime, project_id=project.id)
    assert report["requirements"], "the compiler produced nothing from a brief full of constraints"
    # Normalised, the same way `ground_requirement` compares: the brief is hard-wrapped and an
    # honest quote spanning a line break is not a raw substring of it.
    from ideapress.domain.requirements import normalise_for_matching

    material = normalise_for_matching(BRIEF)
    for requirement in report["requirements"]:
        assert normalise_for_matching(requirement["quote"]) in material, requirement["quote"]

    first_unit = report["units"][0]["key"]
    draft = start_stage(runtime, project_id=project.id, stage="draft", units=[first_unit])
    assert _wait(runtime, draft.run_id) == "completed"

    units = {unit["unit_key"]: unit for unit in unit_list(runtime, project_id=project.id)}
    state = units[first_unit]["state"]
    assert state in {"committed", "paused"}, state
    if state == "paused":
        reason = units[first_unit]["paused_reason"] or ""
        # A pause is a correct outcome, and there is exactly one shape of it P4 can legitimately
        # produce on its own: a blocking requirement the compiler could not express as a literal
        # check, which only P5's review stage can settle. Any other pause is a real finding.
        assert "no deterministic check" in reason, reason
        pytest.skip(f"awaiting the review stage: {reason}")

    detail = unit_detail(runtime, project_id=project.id, unit_key=first_unit)
    assert detail["content"].strip()
    assert detail["content_hash"].startswith("sha256:")
    assert detail["version"] == 1

    # AC3: the provenance names what produced it.
    attempt = detail["attempts"][0]
    assert attempt["backend"] == "ollama"
    assert attempt["model_canonical_id"].startswith("ollama/")
    assert "@sha256:" in attempt["model_canonical_id"], "a real digest, not a moving tag"
    assert attempt["prompt_id"] == "stages.draft.write"
    assert attempt["prompt_sha256"].startswith("sha256:")
    assert attempt["output_tokens"] > 0
    assert detail["validation"], "every check that ran is recorded"
    assert detail["coverage"], "and so is what covered each requirement"
    # Printed for the evidence transcript: a live run is read by a person, not only asserted.
    sys.stdout.write(
        json.dumps({"unit": first_unit, "provenance": attempt}, indent=2, default=str) + "\n"
    )
