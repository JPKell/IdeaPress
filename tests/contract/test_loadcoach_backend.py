"""P7's LoadCoach adapter, against a mock driven by LoadCoach's own OpenAPI snapshot.

Every request the adapter makes is validated against the producer's request schema before it is
answered — `additionalProperties: false` included, because `GenerateBody` really is
`extra="forbid"` and a field IdeaPress invents is a 422 in production, not something ignored.

The properties here are the ones an integration gets wrong quietly:

* the prompt LoadCoach receives is the prompt IdeaPress rendered, byte for byte, because
  `prompt_sha256` provenance rests on it;
* the configured output budget travels, because a budget that silently reverts to the *task
  profile's* default produces an empty generation with nothing naming the cause;
* the task map is total, single-homed and checked against what the running LoadCoach serves;
* no model override is sent unless the user asked for one (ADR-0040);
* IdeaPress's schema is not claimed to be enforced when it is not (ADR-0041).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.contract.loadcoach_mock import (
    MockLoadCoach,
    SchemaViolation,
    assert_snapshot_matches_distribution,
    load_snapshot,
    validate,
)

from ideapress.config import LoadCoachSettings
from ideapress.domain.inference import (
    Correlation,
    ResponseFormat,
    StageLimits,
    StageRequest,
)
from ideapress.domain.stages import MODEL_STAGES
from ideapress.errors import BackendVersionMismatch
from ideapress.infrastructure.backends.loadcoach import (
    LOADCOACH_TASK_MAP,
    LoadCoachBackend,
    assert_task_map_is_total,
    idempotency_key_for,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SYSTEM_TEXT = "You are drafting one section of an article. Follow every hard requirement."
USER_TEXT = "Write a 600-word section on local inference privacy.\n\nRequirement R-001: …"


def _request(
    stage: str = "critique",
    *,
    response_format: ResponseFormat | None = None,
    limits: StageLimits | None = None,
    model_hint: str | None = None,
) -> StageRequest:
    return StageRequest(
        stage=stage,  # type: ignore[arg-type]  # StageId is a Literal; tests parametrise over it
        system=SYSTEM_TEXT,
        user=USER_TEXT,
        response_format=response_format,
        limits=limits or StageLimits(max_output_tokens=8192, temperature=0.2),
        model_hint=model_hint,
        correlation=Correlation(project_id="01PROJECT", unit_id="U-01", attempt=1),
    )


@pytest.fixture
def mock() -> MockLoadCoach:
    return MockLoadCoach(answers=["A local answer."])


@pytest.fixture
def backend(mock: MockLoadCoach) -> Iterator[LoadCoachBackend]:
    client = mock.client()
    yield LoadCoachBackend(LoadCoachSettings(), client=client)
    client.close()


# ------------------------------------------------------------------ the snapshot


def test_the_vendored_snapshot_matches_an_installed_loadcoach_if_there_is_one() -> None:
    """The vendored copy is a workaround for a missing package artifact, not a private fork.

    `loadcoach 1.0.0` ships no OpenAPI snapshot and no `api_snapshot()`, which Testing Standards
    §8.4 requires of every application (M8-01). Until it does, the contract tests run against a
    vendored copy — and this fails the moment an installed distribution disagrees with it, so the
    copy cannot quietly become something LoadCoach never published.
    """
    assert_snapshot_matches_distribution()


def test_the_snapshot_is_the_v1_contract_this_adapter_speaks() -> None:
    document = load_snapshot()
    assert document["openapi"].startswith("3.")
    for path in ("/api/v1/generate", "/api/v1/version", "/api/v1/task-profiles"):
        assert path in document["paths"], path


def test_the_mock_rejects_a_body_the_producer_would_reject() -> None:
    """The validator is load-bearing, so prove it fails rather than trusting that it passes."""
    document = load_snapshot()
    schema = document["components"]["schemas"]["GenerateBody"]
    with pytest.raises(SchemaViolation, match="required property 'task' is missing"):
        validate({"prompt": "x"}, schema, document)
    with pytest.raises(SchemaViolation, match="forbids additional properties"):
        validate({"task": "general.reasoning", "constraints": {}}, schema, document)


# ------------------------------------------------------------------ the task map


def test_the_task_map_is_total_over_the_model_using_stages() -> None:
    """Workflows §2 is the only stage list; the map covers exactly its model-using rows."""
    assert set(LOADCOACH_TASK_MAP) == set(MODEL_STAGES)
    assert_task_map_is_total()


def test_the_audits_route_through_content_review_not_code_review() -> None:
    """The named cross-application defect: `code.review` filters candidates on *code* ability."""
    assert LOADCOACH_TASK_MAP["audit_fast"] == "content.review"
    assert LOADCOACH_TASK_MAP["audit_deep"] == "content.review"
    assert "code.review" not in set(LOADCOACH_TASK_MAP.values())


def test_the_task_map_lives_in_exactly_one_module() -> None:
    """A LoadCoach task identifier outside the adapter is coupling (risk I2)."""
    src = Path(__file__).resolve().parents[2] / "src"
    holders = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "LOADCOACH_TASK_MAP" in path.read_text(encoding="utf-8")
    )
    assert holders == ["ideapress/infrastructure/backends/loadcoach.py"], holders


def test_every_mapped_profile_exists_on_the_running_loadcoach(backend: LoadCoachBackend) -> None:
    """Checked against `GET /task-profiles`, so a rename surfaces here and not mid-project."""
    assert backend.unmapped_task_profiles() == []


def test_a_renamed_profile_is_reported_by_name(mock: MockLoadCoach) -> None:
    """The failure mode this check exists for: LoadCoach renames a profile IdeaPress relies on."""
    mock._profiles.remove("content.review")  # noqa: SLF001 — simulating the other side's rename
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client)
    assert backend.unmapped_task_profiles() == ["content.review"]
    client.close()


@pytest.mark.parametrize("stage", sorted(MODEL_STAGES))
def test_every_model_using_stage_submits_its_mapped_profile(
    stage: str, backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    backend.generate(_request(stage))
    submission = [r for r in mock.requests if r.path in {"/api/v1/generate", "/api/v1/jobs"}][-1]
    assert submission.body["task"] == LOADCOACH_TASK_MAP[stage]


# ------------------------------------------------------------------ prompt passthrough


def test_the_prompt_loadcoach_receives_is_the_prompt_ideapress_rendered(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """Spec §11 contract 5, and the whole basis of the attempt's `prompt_sha256` provenance.

    Asserted as byte equality against the recorded request rather than as "contains", because a
    prefix the adapter helpfully added would still contain the original and would still falsify
    every provenance hash IdeaPress has ever written.
    """
    backend.generate(_request())
    body = mock.requests[-1].body
    assert body["system"] == SYSTEM_TEXT
    assert body["prompt"] == USER_TEXT
    sent = hashlib.sha256((body["system"] + body["prompt"]).encode()).hexdigest()
    rendered = hashlib.sha256((SYSTEM_TEXT + USER_TEXT).encode()).hexdigest()
    assert sent == rendered


def test_the_adapter_sends_no_field_the_producer_forbids(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """`GenerateBody` is `extra="forbid"`; LoadCoach's own api.md §4 example is not valid against
    it, so the adapter is built from the snapshot and this asserts it stayed that way (M8-02)."""
    backend.generate(_request())
    document = load_snapshot()
    permitted = set(document["components"]["schemas"]["GenerateBody"]["properties"])
    assert set(mock.requests[-1].body) <= permitted


def test_the_headers_attribute_the_call_to_ideapress(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """`X-Client-Name` is what scopes the idempotency key and attributes feedback (api.md §12)."""
    backend.generate(_request())
    headers = mock.requests[-1].headers
    assert headers["x-client-name"] == "ideapress"
    assert headers["x-request-id"]


# ------------------------------------------------------------------ budgets travel


def test_the_configured_output_budget_reaches_loadcoach(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """The silent failure this catches: omitting `max_output_tokens` hands the stage the *task
    profile's* default — 2048 for `structured.extract` against a configured 8192 — and the stage
    fails as an empty generation with nothing naming the cause."""
    backend.generate(_request(limits=StageLimits(max_output_tokens=16384, temperature=0.0)))
    sampling = mock.requests[-1].body["sampling"]
    assert sampling["max_output_tokens"] == 16384
    assert sampling["temperature"] == 0.0


@pytest.mark.parametrize("budget", [1024, 8192, 131072])
def test_every_budget_travels_unchanged(
    budget: int, backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    backend.generate(_request(limits=StageLimits(max_output_tokens=budget)))
    assert mock.requests[-1].body["sampling"]["max_output_tokens"] == budget


# ------------------------------------------------------------------ ADR-0040


def test_no_model_override_is_sent_by_default(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """ADR-0040: a binding sent as an override would bypass routing while every stage succeeded."""
    backend.generate(_request(model_hint="ollama/gemma4:12b"))
    assert "overrides" not in mock.requests[-1].body


def test_an_override_is_sent_when_the_user_asked_for_one(mock: MockLoadCoach) -> None:
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(honour_stage_bindings=True), client=client)
    backend.generate(_request(model_hint="ollama/qwen3.5:9b-q8_0"))
    assert mock.requests[-1].body["overrides"] == {"model": "ollama/qwen3.5:9b-q8_0"}
    client.close()


def test_an_unhonoured_pin_is_a_degradation_not_a_failure() -> None:
    """ADR-0040 §5: a pin is a request. LoadCoach routing elsewhere beats a failed stage — but the
    user asked for something specific and is told they did not get it."""
    mock = MockLoadCoach(answers=["x"], model_canonical_id="ollama/gemma4:12b@sha256:aaaa")
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(honour_stage_bindings=True), client=client)
    result = backend.generate(_request(model_hint="ollama/qwen3.5:9b-q8_0"))
    assert any("model_override_not_honoured" in d for d in result.degradations)
    assert "qwen3.5:9b-q8_0" in " ".join(result.degradations)
    assert "gemma4:12b" in " ".join(result.degradations)
    client.close()


def test_an_honoured_pin_records_no_degradation() -> None:
    mock = MockLoadCoach(answers=["x"], model_canonical_id="ollama/qwen3.5:9b-q8_0@sha256:bb")
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(honour_stage_bindings=True), client=client)
    result = backend.generate(_request(model_hint="ollama/qwen3.5:9b-q8_0"))
    assert not any("model_override_not_honoured" in d for d in result.degradations)
    client.close()


def test_the_backend_reports_that_it_routes_internally(backend: LoadCoachBackend) -> None:
    assert backend.capabilities().routes_internally is True
    assert backend.capabilities().residency_control is False


def test_unload_refuses_honestly(backend: LoadCoachBackend) -> None:
    """Returning True would put an eviction that never happened into a provenance record."""
    assert backend.unload("ollama/gemma4:12b") is False
    assert list(backend.resident_models()) == []


# ------------------------------------------------------------------ ADR-0041


def test_a_schema_request_becomes_json_mode_with_the_reason_recorded(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """ADR-0041: LoadCoach applies the *task profile's* schema, which for `content.review` cannot
    express ADR-0039's `requirements_assessment` at all. So ask for JSON and say so."""
    result = backend.generate(
        _request("audit_fast", response_format=ResponseFormat(kind="json_schema", schema={"a": 1}))
    )
    assert mock.requests[-1].body["response_format"] == "json"
    assert any("structured_output_unavailable" in d for d in result.degradations)
    assert "task profile's schema" in " ".join(result.degradations)


def test_plain_json_mode_records_no_degradation(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """Asking for valid JSON *is* honoured, so nothing is degraded and nothing claims it was."""
    result = backend.generate(_request("critique", response_format=ResponseFormat(kind="json")))
    assert mock.requests[-1].body["response_format"] == "json"
    assert not any("structured_output_unavailable" in d for d in result.degradations)


def test_the_backend_does_not_claim_to_enforce_a_schema(backend: LoadCoachBackend) -> None:
    capabilities = backend.capabilities()
    assert capabilities.structured_output is False
    assert capabilities.json_mode is True


# ------------------------------------------------------------------ idempotency


def test_a_submission_carries_a_per_attempt_idempotency_key(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    backend.generate(_request())
    key = mock.requests[-1].body["idempotency_key"]
    assert key.startswith("ideapress-")
    assert len(key) <= 128


def test_the_same_request_produces_the_same_key_and_a_different_one_does_not() -> None:
    """What the key is for: a retried submission replays rather than creating a second job."""
    assert idempotency_key_for(_request()) == idempotency_key_for(_request())
    assert idempotency_key_for(_request()) != idempotency_key_for(_request("draft"))


def test_a_changed_prompt_produces_a_new_key() -> None:
    """The subtle one. LoadCoach reserves a key for 24 h and replays the original job, so a key
    over coordinates alone would replay a stale answer for a `repair` whose findings had changed
    since the project was interrupted."""
    first = _request("repair")
    second = StageRequest(
        stage="repair",
        system=first.system,
        user=first.user + "\n\nAlso fix: the second finding.",
        limits=first.limits,
        correlation=first.correlation,
    )
    assert idempotency_key_for(first) != idempotency_key_for(second)


def test_a_retried_submission_replays_rather_than_duplicating(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    request = _request()
    first = backend.generate(request)
    second = backend.generate(request)
    assert first.text == second.text
    assert mock.replayed_keys, "the second submission did not carry the first's key"


# ------------------------------------------------------------------ version negotiation


def test_version_is_negotiated_on_first_contact(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    backend.generate(_request())
    assert mock.requests[0].path == "/api/v1/version"


def test_the_version_call_is_cached_across_generations(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    backend.generate(_request())
    backend.generate(_request("draft"))
    assert [r.path for r in mock.requests].count("/api/v1/version") == 1


def test_the_running_services_own_version_shape_negotiates() -> None:
    """The shape LoadCoach 1.0.0 actually answers with: `api.supported = ["v1"]`.

    Regression guard for M8-04 — the adapter read a flat `api_versions` key that the real service
    does not have, and every offline test passed because the mock had the same misconception.
    """
    mock = MockLoadCoach(answers=["x"], api_versions=("v1",))
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client)
    assert backend.health().status == "ok"
    assert backend.health().version == "1.0.0"
    client.close()


def test_a_major_mismatch_names_both_versions_and_refuses() -> None:
    """No silent downgrade: an adapter guessing at a contract it does not know is how a wrong
    field becomes a wrong provenance record."""
    mock = MockLoadCoach(version="2.4.0", api_versions=("2.0",))
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client)
    with pytest.raises(BackendVersionMismatch) as caught:
        backend.generate(_request())
    message = str(caught.value)
    assert "major 2" in message
    assert "major 1" in message
    assert caught.value.details["loadcoach_api_majors"] == [2]
    client.close()


def test_a_mismatch_is_degraded_health_not_an_outage() -> None:
    """ "It answers but we cannot talk to it" is a different thing from "it is down"."""
    mock = MockLoadCoach(api_versions=("2.0",))
    client = mock.client()
    health = LoadCoachBackend(LoadCoachSettings(), client=client).health()
    assert health.status == "degraded"
    assert "major 2" in health.detail
    client.close()


def test_health_reports_the_running_version(backend: LoadCoachBackend) -> None:
    health = backend.health()
    assert health.status == "ok"
    assert health.version == "1.0.0"
    assert health.is_remote is False


def test_a_remote_loadcoach_is_flagged_as_egress(mock: MockLoadCoach) -> None:
    """Risk S4: the user is told plainly, per backend, where their content would go."""
    client = mock.client(base_url="http://10.0.0.9:8766")
    backend = LoadCoachBackend(LoadCoachSettings(base_url="http://10.0.0.9:8766"), client=client)
    assert backend.health().is_remote is True
    client.close()


# ------------------------------------------------------------------ results


def test_routing_metadata_lands_on_the_result(backend: LoadCoachBackend) -> None:
    """P7 AC2 — visible per attempt in the UI, which means it must survive the port first."""
    result = backend.generate(_request())
    assert result.routing is not None
    assert result.routing["decision_id"]
    assert result.routing["final_score"] == 0.71


def test_usage_and_timings_are_carried_and_unsupported_is_not_zero(
    backend: LoadCoachBackend,
) -> None:
    """ADR-0016: LoadCoach sends `"unsupported"` for a measurement it could not take."""
    result = backend.generate(_request())
    assert result.usage.input_tokens == 812
    assert result.usage.output_tokens == 1104
    assert result.usage.thinking_tokens is None
    assert result.timing.duration_ms == 18422.0


def test_the_model_that_answered_is_recorded(backend: LoadCoachBackend) -> None:
    result = backend.generate(_request())
    assert result.model is not None
    assert result.model.provider_kind == "ollama"
    assert result.model.provider_model_name == "qwen3.5:9b-q8_0"
    assert result.model.artifact_digest == "sha256:1f3a9c4e2b70"


def test_an_undisclosed_model_is_none_rather_than_a_guess() -> None:
    mock = MockLoadCoach(answers=["x"], model_canonical_id="")
    client = mock.client()
    result = LoadCoachBackend(LoadCoachSettings(), client=client).generate(_request())
    assert result.model is None
    client.close()


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("assumed_context", "assumed_context"), ("low_evidence", "low_evidence")],
)
def test_routing_flags_that_matter_become_degradations(flag: str, expected: str) -> None:
    """Workflows §6.2: `assumed_context` means a later context overflow is a consequence, not a
    surprise."""
    mock = MockLoadCoach(answers=["x"], routing_flags=[flag])
    client = mock.client()
    result = LoadCoachBackend(LoadCoachSettings(), client=client).generate(_request())
    assert any(expected in d for d in result.degradations)
    client.close()


def test_a_queue_wait_is_recorded_and_visible() -> None:
    mock = MockLoadCoach(answers=["x"], queue_wait_ms=4200.0)
    client = mock.client()
    result = LoadCoachBackend(LoadCoachSettings(), client=client).generate(_request())
    assert result.timing.queue_wait_ms == 4200.0
    assert any("queue_wait" in d for d in result.degradations)
    client.close()


def test_loadcoachs_own_degradations_are_carried_through_verbatim() -> None:
    mock = MockLoadCoach(
        answers=["x"],
        degradations=[{"code": "cancellation_deferred_to_completion", "detail": "no streaming"}],
    )
    client = mock.client()
    result = LoadCoachBackend(LoadCoachSettings(), client=client).generate(_request())
    assert any("cancellation_deferred_to_completion" in d for d in result.degradations)
    client.close()


# ------------------------------------------------------------------ jobs and streaming


def test_a_long_stage_goes_through_the_job_queue(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """`/generate` for interactive work and `/jobs` for background work (api.md §12.2)."""
    backend.generate(_request("draft"))
    paths = [r.path for r in mock.requests]
    assert "/api/v1/jobs" in paths
    assert "/api/v1/generate" not in paths


def test_an_interactive_stage_does_not_queue(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    backend.generate(_request("critique"))
    paths = [r.path for r in mock.requests]
    assert "/api/v1/generate" in paths
    assert "/api/v1/jobs" not in paths


def test_streaming_yields_tokens_then_one_completed_frame(backend: LoadCoachBackend) -> None:
    """The terminal frame carries the same result `generate` would have returned."""
    events = list(backend.stream(_request()))
    assert [event.kind for event in events][-1] == "completed"
    assert all(event.kind == "token" for event in events[:-1])
    streamed = "".join(event.text for event in events if event.kind == "token")
    assert streamed.strip() == "A local answer."
    assert events[-1].result is not None
    assert events[-1].result.routing is not None


def test_a_streamed_result_is_unwrapped_from_its_setspec_envelope(
    backend: LoadCoachBackend,
) -> None:
    """Every frame but `token` carries the envelope (ADR-0025 §3); `token` is bare."""
    events = list(backend.stream(_request()))
    result = events[-1].result
    assert result is not None
    assert result.text == "A local answer."
    assert result.usage.input_tokens == 812


# ------------------------------------------------------------------ feedback


def test_feedback_posts_the_documented_body(backend: LoadCoachBackend, mock: MockLoadCoach) -> None:
    backend.post_feedback("01J9K0001", accepted=True, validation_passed=True, quality_score=0.8)
    body = mock.requests[-1].body
    assert body["accepted"] is True
    assert body["source"] == "ideapress"
    assert body["validation"] == {"passed": True, "detail": None}
    assert body["quality_score"] == 0.8


def test_a_quality_score_is_clamped_to_the_producers_range(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    """`quality_score` is 0–1 on LoadCoach's side; sending 1.4 would be a 422."""
    backend.post_feedback("01J9K0001", accepted=True, quality_score=1.4)
    assert mock.requests[-1].body["quality_score"] == 1.0


def test_feedback_validates_against_the_producers_schema(
    backend: LoadCoachBackend, mock: MockLoadCoach
) -> None:
    backend.post_feedback("01J9K0001", accepted=False, notes="rejected: coverage")
    document = load_snapshot()
    validate(mock.requests[-1].body, document["components"]["schemas"]["FeedbackBody"], document)
