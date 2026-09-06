"""A stage's adapter pin travels as LoadCoach's `adapter` override, and a refusal fails the stage.

Gate D of Phase 10 (ADR-0083, ADR-0064 rule 4, ADR-0065 rule 2). Every body here is validated
against LoadCoach's own OpenAPI snapshot before the mock answers it, so an invented field is a
failure in this suite exactly as it is in production — `GenerateBody` is `extra="forbid"`.

The property that matters is negative and silent: **a pinned stage never comes back with the bare
model's text.** Each of the four roads to that outcome — a pin LoadCoach does not have, a pin it
cannot honour, a pin against a server too old to carry it, and a pin answered by the wrong subject
— is asserted here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from tests.contract.loadcoach_mock import MockLoadCoach
from tests.contract.test_loadcoach_backend import _request

from ideapress.config import LoadCoachSettings
from ideapress.errors import AdapterNotFound, AdapterProfileMismatch, BackendVersionMismatch
from ideapress.infrastructure.backends.loadcoach import LoadCoachBackend

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def registered() -> Iterator[tuple[MockLoadCoach, LoadCoachBackend]]:
    """A LoadCoach 1.1 with two adapters registered."""
    mock = MockLoadCoach(answers=["A pinned answer."], adapters=["house-voice", "terse-editor"])
    client = mock.client()
    yield mock, LoadCoachBackend(LoadCoachSettings(), client=client)
    client.close()


def _sent(mock: MockLoadCoach, path: str = "/api/v1/generate") -> dict[str, Any]:
    """The last body sent to ``path``."""
    bodies = [r.body for r in mock.requests if r.path == path and isinstance(r.body, dict)]
    assert bodies, f"nothing was sent to {path}"
    return dict(bodies[-1])


def test_a_pinned_stage_sends_the_adapter_override(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """No flag gates it: configuring a pin is configuring it on (ADR-0083)."""
    mock, backend = registered

    result = backend.generate(_request("critique", adapter_hint="house-voice"))

    body = _sent(mock)
    assert body["overrides"] == {"adapter": "house-voice"}
    assert result.text == "A pinned answer."


def test_a_pin_does_not_need_honour_stage_bindings(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """The flag means "give up routing", which an adapter pin does not do (ADR-0083).

    Asserted rather than assumed, because the tempting economy this record rejects is exactly
    hanging the pin off that flag.
    """
    mock, backend = registered
    assert LoadCoachSettings().honour_stage_bindings is False

    backend.generate(_request("critique", adapter_hint="house-voice", model_hint="ollama/x"))

    assert _sent(mock)["overrides"] == {"adapter": "house-voice"}


def test_both_pins_together_name_one_subject(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """The one combination in which an adapter pin surrenders routing.

    It does so because the model pin already did.
    """
    mock, _ = registered
    client = mock.client()
    backend = LoadCoachBackend(
        LoadCoachSettings(honour_stage_bindings=True),
        client=client,
    )

    backend.generate(
        _request("critique", adapter_hint="house-voice", model_hint="ollama/gemma4:12b")
    )

    assert _sent(mock)["overrides"] == {
        "model": "ollama/gemma4:12b",
        "adapter": "house-voice",
    }
    client.close()


def test_an_unpinned_request_carries_no_overrides_at_all(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """The compatibility claim at the wire: 1.0's body, plus the classification and nothing else."""
    mock, backend = registered

    backend.generate(_request("critique"))

    body = _sent(mock)
    assert "overrides" not in body
    assert body["data_classification"] == "public"
    assert set(body) == {
        "task",
        "system",
        "prompt",
        "idempotency_key",
        "sampling",
        "data_classification",
    }


def test_the_declared_classification_travels_on_every_request() -> None:
    """One value for the installation, on the pinned and the unpinned request alike."""
    mock = MockLoadCoach(answers=["x"], adapters=["house-voice"])
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client, data_classification="internal")

    backend.generate(_request("critique"))
    backend.generate(_request("draft", adapter_hint="house-voice"))

    sent = [
        r.body
        for r in mock.requests
        if r.path in {"/api/v1/generate", "/api/v1/jobs"} and isinstance(r.body, dict)
    ]
    assert [b["data_classification"] for b in sent] == ["internal", "internal"]
    client.close()


def test_the_answering_adapter_is_read_from_the_response_not_the_pin(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """What was asked for and what answered are different facts (workflows §8)."""
    _, backend = registered

    result = backend.generate(_request("critique", adapter_hint="terse-editor"))

    assert result.adapter is not None
    assert result.adapter.name == "terse-editor"
    assert result.adapter.artifact_digest is not None
    assert result.adapter.artifact_digest.startswith("sha256:")
    assert "+terse-editor@sha256:" in (result.adapter.subject_canonical_id or "")


def test_an_unpinned_answer_names_no_adapter(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """`None`, not a subject with empty fields — the two are different facts."""
    _, backend = registered

    assert backend.generate(_request("critique")).adapter is None


def test_an_unknown_adapter_fails_the_stage_and_lists_what_exists(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """ADR-0064 rule 4: refused by name, never quietly served by the bare model."""
    _, backend = registered

    with pytest.raises(AdapterNotFound) as caught:
        backend.generate(_request("critique", adapter_hint="no-such-adapter"))

    assert "no-such-adapter" in caught.value.message
    assert caught.value.details["available"] == ["house-voice", "terse-editor"]


def test_a_profile_mismatch_fails_the_stage(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """The other permanent refusal: no candidate could honour the pin under this profile."""
    mock, backend = registered
    mock.fail_next("PROFILE_MISMATCH", "no candidate could serve this adapter")

    with pytest.raises(AdapterProfileMismatch):
        backend.generate(_request("critique", adapter_hint="house-voice"))


def test_an_answer_from_the_wrong_subject_is_refused_rather_than_recorded() -> None:
    """The guard for the one failure that is silent: a pinned stage answered by the base.

    LoadCoach 1.1 refuses an unhonourable pin itself, so this cannot happen in a correct exchange.
    It is checked anyway because the text has already been written by the time anyone could
    notice, and because a wrong provenance record is worse than a failed stage.
    """

    class SilentlyDegrading(MockLoadCoach):
        """Accepts the pin and answers with the bare base — the shape of a server that degraded."""

        def _adapter_block(self, body: Any) -> dict[str, Any] | None:
            return None

    mock = SilentlyDegrading(answers=["the base's prose"], adapters=["house-voice"])
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client)

    with pytest.raises(AdapterProfileMismatch) as caught:
        backend.generate(_request("critique", adapter_hint="house-voice"))

    assert caught.value.details["requested_adapter"] == "house-voice"
    assert caught.value.details["answered_adapter"] is None
    client.close()


def test_a_pin_against_a_loadcoach_1_0_is_refused_by_name() -> None:
    """Not dropped to make the request fit: a dropped pin is answered by the bare model."""
    mock = MockLoadCoach(answers=["x"], version="1.0.0")
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client)

    with pytest.raises(BackendVersionMismatch) as caught:
        backend.generate(_request("critique", adapter_hint="house-voice"))

    assert caught.value.details["loadcoach_version"] == "1.0.0"
    assert caught.value.details["required_loadcoach_version"] == "1.1"
    client.close()


def test_a_declared_classification_against_a_loadcoach_1_0_is_refused_by_name() -> None:
    """Dropping it would send work under a classification nobody declared."""
    mock = MockLoadCoach(answers=["x"], version="1.0.0")
    client = mock.client()
    backend = LoadCoachBackend(
        LoadCoachSettings(), client=client, data_classification="confidential"
    )

    with pytest.raises(BackendVersionMismatch):
        backend.generate(_request("critique"))
    client.close()


def test_an_undeclared_installation_still_talks_to_a_loadcoach_1_0() -> None:
    """The default is the lowest level, whose join is the adapter's own value.

    So an undeclared installation sends 1.0 traffic and needs no 1.1 server.
    """
    mock = MockLoadCoach(answers=["x"], version="1.0.0")
    client = mock.client()
    backend = LoadCoachBackend(LoadCoachSettings(), client=client)

    assert backend.generate(_request("critique")).text == "x"
    assert "data_classification" not in _sent(mock)
    client.close()


def test_a_pinned_job_stage_carries_the_override_through_the_queue(
    registered: tuple[MockLoadCoach, LoadCoachBackend],
) -> None:
    """`draft` is a `job_stages` member, so its pin travels on `/jobs`, not `/generate`."""
    mock, backend = registered
    assert "draft" in LoadCoachSettings().job_stages

    backend.generate(_request("draft", adapter_hint="house-voice"))

    body = _sent(mock, "/api/v1/jobs")
    assert body["overrides"] == {"adapter": "house-voice"}
    assert body["data_classification"] == "public"
