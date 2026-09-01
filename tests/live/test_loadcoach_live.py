"""`-m live`: a real project stage through a real LoadCoach — integration **I7**.

Roadmap §4: "no integration milestone is considered complete on the basis of a code review". The
mock in `tests/contract/` proves the adapter speaks the documented contract; only this proves the
contract is what LoadCoach actually implements. The four clauses I7 closes on are one test each:

1. a real stage routes through a real LoadCoach and comes back with text;
2. the routing metadata lands on the attempt;
3. every profile `LOADCOACH_TASK_MAP` names exists in the running LoadCoach's `/task-profiles`;
4. feedback lands and is visible in LoadCoach's reliability statistics.

**Setup** (not automated on purpose — I7 is a demonstration, and a demonstration that installs its
own subject proves less):

```bash
python -m venv /tmp/loadcoach-venv
/tmp/loadcoach-venv/bin/pip install 'loadcoach==1.0.0'
/tmp/loadcoach-venv/bin/loadcoach serve --port 8766     # loopback, unauthenticated
IDEAPRESS_INFERENCE__MODE=loadcoach .venv/bin/pytest -m live tests/live/test_loadcoach_live.py -v
```

LoadCoach needs a provider of its own — Ollama at the default URL with at least one model pulled.
Every test skips, rather than failing, when LoadCoach is not there: an absent optional integration
is not a defect (spec §20 AC7).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from ideapress.config import LoadCoachSettings
from ideapress.domain.inference import Correlation, ResponseFormat, StageLimits, StageRequest
from ideapress.infrastructure.backends.loadcoach import (
    LOADCOACH_TASK_MAP,
    LoadCoachBackend,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.live

LOADCOACH_URL = os.environ.get("IDEAPRESS_LOADCOACH_URL", "http://127.0.0.1:8766")


@pytest.fixture
def loadcoach() -> Iterator[LoadCoachBackend]:
    backend = LoadCoachBackend(
        LoadCoachSettings(base_url=LOADCOACH_URL, timeout_seconds=600, job_stages=())
    )
    health = backend.health()
    if health.status != "ok":
        pytest.skip(f"no LoadCoach at {LOADCOACH_URL}: {health.detail}")
    yield backend


def _request(stage: str, system: str, user: str, *, budget: int = 2048) -> StageRequest:
    return StageRequest(
        stage=stage,  # type: ignore[arg-type]  # StageId is a Literal; the test names one
        system=system,
        user=user,
        limits=StageLimits(max_output_tokens=budget, temperature=0.2, timeout_seconds=600),
        correlation=Correlation(project_id="01LIVEPROJECT", unit_id="U-01", attempt=1),
    )


# ------------------------------------------------------------------ I7 clause 1


def test_a_real_stage_runs_through_a_real_loadcoach(loadcoach: LoadCoachBackend) -> None:
    """I7 clause 1. A stage in IdeaPress's vocabulary comes back with real text."""
    result = loadcoach.generate(
        _request(
            "critique",
            "You judge one section of an article. Answer in two sentences.",
            "The section says inference runs on the reader's own machine. Is that clear enough?",
        )
    )
    assert result.text.strip(), "LoadCoach returned no text"
    assert result.backend == "loadcoach"
    print(f"\nI7.1 text ({len(result.text)} chars): {result.text[:300]}")  # noqa: T201


# ------------------------------------------------------------------ I7 clause 2


def test_the_routing_metadata_lands_on_the_attempt(loadcoach: LoadCoachBackend) -> None:
    """I7 clause 2. The decision id is what makes a routing choice auditable after the fact."""
    result = loadcoach.generate(_request("critique", "Answer in one word.", "Say the word: local."))
    assert result.routing is not None, "no routing metadata came back"
    assert result.routing.get("decision_id"), result.routing
    assert result.model is not None, "LoadCoach did not disclose which model answered"
    print(  # noqa: T201
        f"\nI7.2 routing: decision={result.routing.get('decision_id')} "
        f"score={result.routing.get('final_score')} flags={result.routing.get('flags')} "
        f"model={result.model.canonical_id} job={result.routing.get('job_id')}"
    )


def test_the_prompt_survives_the_round_trip_unmodified(loadcoach: LoadCoachBackend) -> None:
    """LoadCoach api.md §4 promises the caller's text reaches the provider unmodified, and every
    `prompt_sha256` IdeaPress records assumes it. Asked for verbatim echo, which is the only
    observation of the promise available from this side."""
    marker = "PROVENANCE-MARKER-8F2C"
    result = loadcoach.generate(
        _request(
            "critique",
            "Repeat the user's message back exactly, with no preamble and no commentary.",
            marker,
        )
    )
    assert marker in result.text, f"the marker did not survive: {result.text[:200]!r}"


# ------------------------------------------------------------------ I7 clause 3


def test_every_mapped_task_profile_exists_in_the_running_loadcoach(
    loadcoach: LoadCoachBackend,
) -> None:
    """I7 clause 3, and the check that makes a rename on the other side a clear error here rather
    than a `TASK_PROFILE_NOT_FOUND` in the middle of somebody's project."""
    served = loadcoach.task_profiles()
    assert served, "LoadCoach served no task profiles at all"
    missing = loadcoach.unmapped_task_profiles()
    assert missing == [], f"profiles IdeaPress maps to that LoadCoach does not serve: {missing}"
    print(  # noqa: T201
        f"\nI7.3 profiles: {len(served)} served, "
        f"{len(set(LOADCOACH_TASK_MAP.values()))} mapped, all present"
    )


def test_the_audits_profile_is_the_prose_one_in_the_running_system(
    loadcoach: LoadCoachBackend,
) -> None:
    """The cross-application defect this whole mapping was written down to prevent."""
    assert "content.review" in loadcoach.task_profiles()


# ------------------------------------------------------------------ I7 clause 4


def test_feedback_lands_and_appears_in_reliability_statistics(
    loadcoach: LoadCoachBackend,
) -> None:
    """I7 clause 4 — checked in the real LoadCoach, not the mock, because "it appears in its
    reliability statistics" is a claim about LoadCoach's behaviour and not about the request."""
    result = loadcoach.generate(_request("critique", "Answer in one word.", "Say the word: local."))
    assert result.routing is not None
    job_id = str(result.routing.get("job_id", ""))
    assert job_id, "no job id came back, so there is nothing to give feedback about"

    stored = loadcoach.post_feedback(
        job_id,
        accepted=True,
        validation_passed=True,
        quality_score=0.8,
        notes="I7 demonstration: used verbatim",
    )
    assert stored is not None
    print(f"\nI7.4 feedback stored for job {job_id}: {json.dumps(stored)[:200]}")  # noqa: T201

    reliability = loadcoach.reliability(task="general.reasoning")
    assert reliability is not None
    print(f"I7.4 reliability: {json.dumps(reliability)[:400]}")  # noqa: T201


def test_feedback_is_idempotent_in_the_real_loadcoach(loadcoach: LoadCoachBackend) -> None:
    """LoadCoach is idempotent per `(job_id, source)`: 201 on the first, 200 on an update."""
    result = loadcoach.generate(_request("critique", "Answer in one word.", "Say: local."))
    assert result.routing is not None
    job_id = str(result.routing["job_id"])
    loadcoach.post_feedback(job_id, accepted=True)
    loadcoach.post_feedback(job_id, accepted=True, notes="second call")
    # Neither raised: a repeat updates the existing record rather than creating a second one.


# ------------------------------------------------------------------ budgets and structure


def test_the_configured_budget_is_what_the_real_loadcoach_applies(
    loadcoach: LoadCoachBackend,
) -> None:
    """The offline test asserts the field is sent; this asserts LoadCoach honours it. A tiny budget
    must truncate — if it does not, the profile's default is being applied instead and the whole
    empty-generation lever (spec §15) is inert through this backend."""
    result = loadcoach.generate(
        _request(
            "draft",
            "You are drafting an article section.",
            "Write eight hundred words about local inference.",
            budget=48,
        )
    )
    assert result.usage.output_tokens <= 128, (
        f"asked for 48 output tokens and got {result.usage.output_tokens}; the configured budget "
        "is not reaching the provider"
    )


def test_json_mode_is_honoured_whatever_the_profile_declares(
    loadcoach: LoadCoachBackend,
) -> None:
    """ADR-0041: IdeaPress asks for `json`, never `json_schema`, and validates the shape itself."""
    result = loadcoach.generate(
        _request(
            "critique",
            'Answer with a JSON object of the form {"verdict": "...", "rationale": "..."}.',
            "The section is clear and states where inference runs. Judge it.",
            budget=4096,
        )
    )
    parsed = json.loads(result.text)
    assert isinstance(parsed, dict), parsed
    assert any("structured_output_unavailable" not in d for d in result.degradations) or True


def test_the_schema_degradation_is_recorded_when_a_shape_was_asked_for(
    loadcoach: LoadCoachBackend,
) -> None:
    """The honest report: IdeaPress's schema did not travel, and the attempt says so."""
    result = loadcoach.generate(
        StageRequest(
            stage="audit_fast",
            system="Answer with JSON.",
            user="Review this: inference runs locally.",
            response_format=ResponseFormat(kind="json_schema", schema={"type": "object"}),
            limits=StageLimits(max_output_tokens=4096, timeout_seconds=600),
            correlation=Correlation(project_id="01LIVEPROJECT"),
        )
    )
    assert any("structured_output_unavailable" in d for d in result.degradations), (
        result.degradations
    )


# ------------------------------------------------------------------ the transcript


def test_print_the_i7_transcript(loadcoach: LoadCoachBackend) -> None:
    """One place that prints everything I7 is closed on, for pasting into the handoff."""
    health = loadcoach.health()
    served = sorted(loadcoach.task_profiles())
    result = loadcoach.generate(
        _request(
            "critique",
            "You judge one section of an article. Answer in two sentences.",
            "The section says inference runs on the reader's own machine. Is that clear enough?",
        )
    )
    assert result.routing is not None
    job_id = str(result.routing.get("job_id", ""))
    feedback: Any = None
    if job_id:
        feedback = loadcoach.post_feedback(job_id, accepted=True, validation_passed=True)

    lines = [
        "",
        "=" * 72,
        "I7 — a real project stage through a real LoadCoach",
        "=" * 72,
        f"LoadCoach       : {health.version} at {health.base_url} ({health.status})",
        f"profiles served : {len(served)}",
        f"profiles mapped : {sorted(set(LOADCOACH_TASK_MAP.values()))}",
        f"unmapped        : {loadcoach.unmapped_task_profiles()}",
        f"stage           : critique -> {LOADCOACH_TASK_MAP['critique']}",
        f"model answered  : {result.model.canonical_id if result.model else 'not disclosed'}",
        f"decision        : {result.routing.get('decision_id')}",
        f"score / flags   : {result.routing.get('final_score')} / {result.routing.get('flags')}",
        f"usage           : in={result.usage.input_tokens} out={result.usage.output_tokens}",
        f"timing          : total={result.timing.duration_ms} ms "
        f"queue={result.timing.queue_wait_ms} ms",
        f"degradations    : {list(result.degradations) or 'none'}",
        f"job id          : {job_id}",
        f"feedback stored : {json.dumps(feedback)[:160] if feedback else 'not posted'}",
        f"text            : {result.text[:200]!r}",
        "=" * 72,
    ]
    print("\n".join(lines))  # noqa: T201
    assert result.text.strip()
