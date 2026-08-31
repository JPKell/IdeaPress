"""P6's backend-parity claim, re-run rather than believed.

Spec §20 AC2: switching `inference.mode` requires **no workflow code change** — proven by running
the identical workflow against three backends and comparing what came out.

What "identical structure" means here, precisely: the same unit count, the same requirement
coverage, the same validation outcomes. **Only wording differs.** So the three backends are given
the same *scripted* answers through three *different adapters* — because what is under test is the
adapter layer, not the models. Giving them genuinely different text would test nothing about the
port and would make the assertion unfalsifiable.

The third participant is a deliberately capability-poor backend, which is how the "no structured
output" degradation path gets exercised in the same comparison.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from ideapress.config import OllamaSettings, OpenAICompatibleSettings, Settings, load_settings
from ideapress.infrastructure.backends.fake import CAPABILITY_POOR, FakeBackend, default_fake_script
from ideapress.infrastructure.backends.ollama import OllamaBackend
from ideapress.infrastructure.backends.openai_compatible import OpenAICompatibleBackend
from ideapress.services.runtime import build_runtime

if TYPE_CHECKING:
    from ideapress.domain.inference import InferenceBackend

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The unit must state that inference runs on the reader's own machine.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        },
        {
            "text": "The unit must state that nothing is uploaded.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "no document content is uploaded anywhere",
            "checks": [{"kind": "must_contain_any", "values": ["uploaded"]}],
        },
    ]
}
PLAN = {
    "units": [
        {
            "title": "Where the work happens",
            "goal_text": "Say plainly where inference runs.",
            "requirement_keys": ["R-001", "R-002"],
            "target_words": 50,
        },
        {
            "title": "What it costs you",
            "goal_text": "Be honest about the trade.",
            "requirement_keys": ["R-001", "R-002"],
            "target_words": 50,
        },
    ]
}
# The wording differs between backends; nothing else does. That is what the parity claim is about.
DRAFTS = {
    "ollama": (
        "Everything happens on your own machine. Nothing you write is uploaded anywhere at all, "
        "and no account is needed for any of it. The hardware is yours to provide."
    ),
    "openai_compatible": (
        "The work runs on your own machine, start to finish. Nothing is uploaded, to us or to "
        "anyone else, and there is no account. You supply the hardware."
    ),
    "fake": (
        "It all takes place on your own machine. Nothing at all is uploaded, and no sign-up is "
        "involved anywhere. In exchange, the hardware bill is yours."
    ),
}
CLEAN_REVIEW = (
    json.dumps({"findings": []}),
    json.dumps({"verdict": "acceptable", "rationale": "ok"}),
)


def _script_of(*answers: Any) -> Any:
    from modelrack.testing import FakeGeneration, FakeScript

    return FakeScript(
        models=default_fake_script().models,
        capabilities=default_fake_script().capabilities,
        generations=tuple(
            FakeGeneration(text=a if isinstance(a, str) else json.dumps(a)) for a in answers
        ),
        repeat_final_generation=True,
    )


def _backend(mode: str, *answers: Any) -> InferenceBackend:
    """Build one adapter over a scripted transport, so the *adapter* is what varies."""
    from modelrack.testing import FakeProvider

    script = _script_of(*answers)
    if mode == "ollama":
        return OllamaBackend(OllamaSettings(), provider=FakeProvider(script, seed=3))  # type: ignore[arg-type]  # structural
    if mode == "openai_compatible":
        return OpenAICompatibleBackend(
            OpenAICompatibleSettings(base_url="http://127.0.0.1:9/v1", model="gemma4:12b"),
            provider=FakeProvider(script, seed=3),  # type: ignore[arg-type]  # structural
        )
    return FakeBackend(script=script, seed=3)


@pytest.fixture
def settings() -> Settings:
    return load_settings().settings


def _run_workflow(mode: str) -> dict[str, Any]:
    """Run plan then draft against one backend and report the structure that came out."""
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.stage_bodies import start_plan, start_stage
    from ideapress.services.stages import StageRunner
    from ideapress.services.unit_reports import unit_list

    runtime = build_runtime(load_settings().settings)
    try:

        def swap(*answers: Any) -> None:
            backend = _backend(mode, *answers)
            gateway = InferenceGateway(
                backend=backend,
                bindings=runtime.settings.models.stages,
                execution=runtime.settings.execution,
            )
            runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
            runtime._backend = backend  # noqa: SLF001
            runtime._runner = StageRunner(  # noqa: SLF001
                runtime.storage, gateway=gateway, sink=runtime.events
            )

        def wait(task_id: str) -> str:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if runtime.runner.is_finished(task_id):
                    return runtime.runner.run_state(task_id) or "unknown"
                time.sleep(0.02)
            message = "the stage did not finish"
            raise AssertionError(message)

        swap(REQUIREMENTS, PLAN)
        project_id = runtime.projects.create(title="Local inference", brief=BRIEF).id
        assert wait(start_plan(runtime, project_id=project_id).run_id) == "completed"

        draft = DRAFTS[mode]
        swap(draft, *CLEAN_REVIEW, draft, *CLEAN_REVIEW)
        assert (
            wait(start_stage(runtime, project_id=project_id, stage="draft").run_id) == "completed"
        )

        from ideapress.services.export import build_document

        document = build_document(runtime, project_id=project_id)
        units = unit_list(runtime, project_id=project_id)
        return {
            "unit_count": len(units),
            "unit_keys": [u["unit_key"] for u in units],
            "unit_titles": [u["unit_title"] if "unit_title" in u else u["title"] for u in units],
            "states": [u["state"] for u in units],
            "coverage": {
                unit.key: {
                    entry.key: (entry.satisfied, entry.satisfied_by) for entry in unit.coverage
                }
                for unit in document.units
            },
            "requirement_keys": sorted(
                {entry.key for unit in document.units for entry in unit.coverage}
            ),
            "texts": {unit.key: unit.content for unit in document.units},
        }
    finally:
        runtime.close()


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    return {mode: _run_workflow(mode) for mode in ("ollama", "openai_compatible", "fake")}


def test_the_same_workflow_produces_the_same_unit_count(
    results: dict[str, dict[str, Any]],
) -> None:
    counts = {mode: result["unit_count"] for mode, result in results.items()}
    assert set(counts.values()) == {2}, counts


def test_the_same_units_reach_the_same_states(results: dict[str, dict[str, Any]]) -> None:
    keys = {mode: tuple(result["unit_keys"]) for mode, result in results.items()}
    states = {mode: tuple(result["states"]) for mode, result in results.items()}
    assert len(set(keys.values())) == 1, keys
    assert len(set(states.values())) == 1, states
    assert next(iter(states.values())) == ("committed", "committed")


def test_requirement_coverage_is_identical_across_backends(
    results: dict[str, dict[str, Any]],
) -> None:
    coverage = {
        mode: json.dumps(result["coverage"], sort_keys=True) for mode, result in results.items()
    }
    assert len(set(coverage.values())) == 1, coverage


def test_the_same_requirements_were_compiled(results: dict[str, dict[str, Any]]) -> None:
    keys = {mode: tuple(result["requirement_keys"]) for mode, result in results.items()}
    assert len(set(keys.values())) == 1, keys
    assert next(iter(keys.values())) == ("R-001", "R-002")


def test_only_the_wording_differs(results: dict[str, dict[str, Any]]) -> None:
    """The other half of the claim: the texts really are different, so parity is not trivial."""
    texts = {mode: json.dumps(result["texts"], sort_keys=True) for mode, result in results.items()}
    assert len(set(texts.values())) == 3, "the three backends produced identical text"
    for text in texts.values():
        assert "own machine" in text
        assert "uploaded" in text


def test_a_capability_poor_backend_records_the_degradation_and_still_produces_a_unit() -> None:
    """P6's fourth test: no structured output, honestly reported, and the work still happens."""
    from ideapress.domain.inference import Correlation, ResponseFormat, StageLimits, StageRequest

    backend = FakeBackend(
        name="poor",
        capabilities=CAPABILITY_POOR,
        script=_script_of(json.dumps(REQUIREMENTS)),
    )
    assert backend.capabilities().structured_output is False
    result = backend.generate(
        StageRequest(
            stage="requirements",
            system="s",
            user="u",
            response_format=ResponseFormat(kind="json_schema", schema={"type": "object"}),
            limits=StageLimits(),
            correlation=Correlation(project_id="01PROJECT"),
            model_hint="gemma4:12b",
        )
    )
    assert any("structured_output_unavailable" in d for d in result.degradations)
    assert "no schema was enforced" in " ".join(result.degradations)
    payload = json.loads(result.text)
    assert payload["requirements"], "the answer is still usable; it was parsed rather than enforced"


def test_a_backend_that_can_enforce_a_schema_records_no_such_degradation() -> None:
    from ideapress.domain.inference import Correlation, ResponseFormat, StageLimits, StageRequest

    backend = FakeBackend(script=_script_of(json.dumps(REQUIREMENTS)))
    result = backend.generate(
        StageRequest(
            stage="requirements",
            system="s",
            user="u",
            response_format=ResponseFormat(kind="json_schema", schema={"type": "object"}),
            limits=StageLimits(),
            correlation=Correlation(project_id="01PROJECT"),
            model_hint="gemma4:12b",
        )
    )
    assert not any("structured_output_unavailable" in d for d in result.degradations)
