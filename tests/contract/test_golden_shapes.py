"""The contract mock must produce the shapes a **real** LoadCoach produces — M8-04/05/16's fix.

The vendored OpenAPI snapshot types every response body as a bare object (see
`test_snapshot_coverage.py`), so it cannot say what a body looks like and a mock cannot be
validated against it. The only authority is a running service, so `loadcoach_golden_v1.json` holds
shapes **recorded** from LoadCoach 1.0.0 — keys and value types, never values.

Three defects this milestone had one cause: the mock encoded the implementer's assumption, the
adapter matched the mock, and the real service did something else. Version negotiation read flat
keys where the body nests them (M8-04). The profile list was keyed on `id` where the body says
`profile_id` (M8-05). A declined stage arrives as HTTP 200 with a `failed` job record, and the
adapter's benign defaults turned it into a successful empty generation (M8-16). Each was invisible
offline because the mock agreed.

These tests are the guard. They compare the mock's *shape* to the recorded one and fail on a key
the real service has that the mock does not — which is exactly the gap each defect lived in.

Regenerating: run a real LoadCoach and re-record. A golden edited by hand to make a test pass is
the assumption coming back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.contract.loadcoach_mock import MockLoadCoach

from ideapress.config import LoadCoachSettings
from ideapress.infrastructure.backends.loadcoach import LoadCoachBackend

GOLDEN = Path(__file__).parent / "loadcoach_golden_v1.json"


def _golden() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return loaded


def _shape(value: Any, depth: int = 0) -> Any:
    """Keys and value types, never values. The same function that recorded the goldens."""
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def _missing_keys(real: Any, mock: Any, path: str = "") -> list[str]:
    """Keys the real service returns that the mock does not, recursively.

    One-directional on purpose. A mock carrying an **extra** key is harmless — the adapter ignores
    what it does not read. A mock **missing** a key is the defect: the adapter is never exercised
    against a field the real body carries, so a wrong assumption about it survives every test.
    """
    if isinstance(real, dict) and isinstance(mock, dict):
        gaps: list[str] = []
        for key, value in real.items():
            here = f"{path}.{key}" if path else key
            if key not in mock:
                gaps.append(here)
            else:
                gaps.extend(_missing_keys(value, mock[key], here))
        return gaps
    if isinstance(real, list) and isinstance(mock, list) and real and mock:
        return _missing_keys(real[0], mock[0], f"{path}[]")
    return []


def _mock_body(path_suffix: str) -> Any:
    """Ask the mock for one endpoint's body, the way the adapter would."""
    mock = MockLoadCoach(answers=["A local answer."])
    client = mock.client()
    try:
        return client.get(f"http://127.0.0.1:8766/api/v1{path_suffix}").json()
    finally:
        client.close()


# ------------------------------------------------------------------ the guard


def test_the_goldens_were_recorded_not_written() -> None:
    """A sanity check on the fixture itself: recorded shapes carry type names, not values."""
    golden = _golden()
    assert golden, "no goldens recorded"
    assert golden["GET /version"]["status"] == 200
    assert golden["GET /version"]["shape"]["api"]["current"] == "str"


@pytest.mark.parametrize("endpoint", ["GET /version", "GET /task-profiles"])
def test_the_mock_carries_every_key_the_real_service_returns(endpoint: str) -> None:
    """The M8-04 and M8-05 guard, generalised.

    Both defects were a key the real body has and the mock did not, so nothing offline could see
    them. This fails on the next one.
    """
    golden = _golden()[endpoint]
    suffix = endpoint.split(" ", 1)[1]
    gaps = _missing_keys(golden["shape"], _shape(_mock_body(suffix)))
    assert gaps == [], f"{endpoint}: the mock is missing keys a real LoadCoach returns: {gaps}"


def test_the_mock_generate_body_carries_every_key_the_real_one_does() -> None:
    """The endpoint all three defects touched."""
    mock = MockLoadCoach(answers=["A local answer."])
    client = mock.client()
    try:
        backend = LoadCoachBackend(LoadCoachSettings(), client=client)
        from ideapress.domain.inference import Correlation, StageLimits, StageRequest

        request = StageRequest(
            stage="critique",
            system="s",
            user="u",
            limits=StageLimits(max_output_tokens=64),
            correlation=Correlation(project_id="01P", unit_id="U-01"),
        )
        body, _ = backend._body_for(request)  # noqa: SLF001 — the mock needs a valid body
        response = client.post("http://127.0.0.1:8766/api/v1/generate", json=body)
    finally:
        client.close()
    gaps = _missing_keys(_golden()["POST /generate"]["shape"], _shape(response.json()))
    assert gaps == [], f"the mock's /generate body is missing real keys: {gaps}"


def test_the_recorded_decline_is_the_shape_the_adapter_now_refuses() -> None:
    """M8-16's evidence, kept.

    A declined stage came back as HTTP 422 with an error envelope on this run, and as HTTP 200
    with a `failed` job record on another — both are real, and the adapter handles both. The
    golden holds the 4xx one; `test_loadcoach_degradation.py` holds the 200 one.
    """
    declined = _golden()["POST /generate (declined)"]
    assert declined["status"] >= 400
    assert declined["shape"]["error"]["code"] == "str"


def test_a_missing_key_is_actually_detected() -> None:
    """The guard on the guard. A comparison that cannot fail is worth nothing, and this one is
    subtle enough to be worth proving."""
    real = {"api": {"current": "str", "supported": ["str"]}, "application": {"version": "str"}}
    assert _missing_keys(real, {"api": {"current": "str", "supported": ["str"]}}) == ["application"]
    assert _missing_keys(
        real, {"api": {"supported": ["str"]}, "application": {"version": "str"}}
    ) == ["api.current"]
    assert _missing_keys(real, real) == []


def test_an_extra_key_in_the_mock_is_not_a_failure() -> None:
    """One-directional on purpose: the adapter ignores what it does not read."""
    real = {"api": {"current": "str"}}
    assert _missing_keys(real, {"api": {"current": "str", "extra": "str"}, "more": "str"}) == []
