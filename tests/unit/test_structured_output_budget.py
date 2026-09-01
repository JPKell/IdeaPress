"""M7 finding 1c: the structured-output budget is a setting, not a constant.

``STRUCTURED_OUTPUT_TOKENS`` stays as the measured default, but every structured stage takes the
budget as a parameter and every caller that holds settings passes
``workflow.structured_output_tokens`` — so a user whose model thinks longer than the reference
machine's raises it in ``config.toml`` rather than editing code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ideapress.domain.inference import StageRequest, StageResult
from ideapress.services.requirements import STRUCTURED_OUTPUT_TOKENS, compile_requirements
from ideapress.services.review import run_audit, run_critique


@dataclass
class _RecordingGateway:
    """Stands in for the gateway; keeps every request so a test can read its limits."""

    text: str
    requests: list[StageRequest] = field(default_factory=list)

    def run(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        return StageResult(text=self.text)


def test_the_audit_budget_is_the_callers_setting() -> None:
    gateway = _RecordingGateway(text=json.dumps({"findings": []}))
    run_audit(
        gateway,  # type: ignore[arg-type]  # a recording stand-in is the point
        stage="audit_fast",
        project_id="p",
        unit_key="U-01",
        content="text",
        requirements=(),
        structured_output_tokens=12_345,
    )
    assert gateway.requests[0].limits.max_output_tokens == 12_345


def test_the_critique_budget_is_the_callers_setting() -> None:
    gateway = _RecordingGateway(text=json.dumps({"verdict": "acceptable", "rationale": "ok"}))
    run_critique(
        gateway,  # type: ignore[arg-type]
        project_id="p",
        unit_key="U-01",
        content="text",
        requirements=(),
        findings=(),
        rounds_used=0,
        max_rounds=3,
        structured_output_tokens=23_456,
    )
    assert gateway.requests[0].limits.max_output_tokens == 23_456


def test_the_compilation_budget_is_the_callers_setting() -> None:
    gateway = _RecordingGateway(text=json.dumps({"requirements": []}))
    compile_requirements(
        gateway,  # type: ignore[arg-type]
        project_id="p",
        brief="a brief",
        structured_output_tokens=34_567,
    )
    assert gateway.requests[0].limits.max_output_tokens == 34_567


def test_the_default_is_the_measured_floor() -> None:
    """Callers with no settings still get the measured 8192, unchanged from M6."""
    gateway = _RecordingGateway(text=json.dumps({"findings": []}))
    run_audit(
        gateway,  # type: ignore[arg-type]
        stage="audit_fast",
        project_id="p",
        unit_key="U-01",
        content="text",
        requirements=(),
    )
    assert gateway.requests[0].limits.max_output_tokens == STRUCTURED_OUTPUT_TOKENS == 8192


def test_the_text_stage_floor_follows_the_setting_when_raised() -> None:
    """The live M7 demonstration paused a *draft* whose message said to raise a budget no
    setting reached. The text-writing stages' thinking floor now follows the same knob."""
    from ideapress.services.unit_loop import output_budget_tokens

    # At the default, the formula is unchanged from M6: floor 8192 + 4 per target word.
    assert output_budget_tokens(target_words=80, structured_output_tokens=8192) == 8512
    assert output_budget_tokens(target_words=None, structured_output_tokens=8192) == 8192 + 1600
    # Raised, the setting becomes the floor; lowered, the measured floor holds.
    assert output_budget_tokens(target_words=80, structured_output_tokens=16384) == 16704
    assert output_budget_tokens(target_words=80, structured_output_tokens=1024) == 8512
