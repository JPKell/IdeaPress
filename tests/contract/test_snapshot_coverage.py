"""What the vendored LoadCoach snapshot can and cannot tell us — M8-04/05/16's common cause.

Three defects in this milestone had one shape: the adapter read a response body one way, the mock
agreed, and a real LoadCoach did something else. The instinct afterwards is "validate the mock
against the OpenAPI snapshot". This suite exists because that does not work, and because *why* it
does not work should be an assertion rather than a thing someone rediscovers.

LoadCoach 1.0.0's OpenAPI document types **every** response body as a bare object —
`additionalProperties: true`, no properties. It describes which endpoints exist and what they
accept. It says nothing whatever about what they return. So a mock cannot be checked against it,
and agreement with it is worth nothing.

What follows from that is recorded here and acted on in `loadcoach_golden.py`: the only authority
on a response shape is a running LoadCoach, so the fixtures are captured from one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SNAPSHOT = Path(__file__).parent / "loadcoach_openapi_v1.json"


def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return loaded


def _resolve(schema: Any, root: dict[str, Any]) -> Any:
    """Follow `$ref` chains to the schema they name."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 10:
        target: Any = root
        for part in str(schema["$ref"]).lstrip("#/").split("/"):
            target = target[part]
        schema = target
        seen += 1
    return schema


def _response_schemas() -> dict[str, Any]:
    """Every operation's success-response schema, by ``VERB /path``."""
    document = _document()
    found: dict[str, Any] = {}
    for path, operations in document.get("paths", {}).items():
        for verb, operation in operations.items():
            if verb not in {"get", "post", "put", "delete", "patch"}:
                continue
            responses = operation.get("responses", {})
            body = responses.get("200") or responses.get("201") or {}
            schema = (body.get("content", {}).get("application/json", {}) or {}).get("schema", {})
            found[f"{verb.upper()} {path}"] = _resolve(schema, document)
    return found


def test_the_snapshot_describes_no_response_shape_at_all() -> None:
    """The finding, asserted.

    If this ever fails it is **good news**: LoadCoach has started describing what it returns, and
    the mocks for those endpoints should be validated against it. Failing loudly is how anyone
    finds out; a silent improvement nobody notices leaves the fixtures hand-written forever.
    """
    described = {
        name: schema
        for name, schema in _response_schemas().items()
        if isinstance(schema, dict) and schema.get("properties")
    }
    assert described == {}, (
        "LoadCoach's snapshot now describes response shapes for "
        f"{sorted(described)} — validate the contract mock against it for those endpoints and "
        "shrink the golden fixtures accordingly."
    )


def test_the_endpoints_this_adapter_depends_on_are_all_in_the_blind_spot() -> None:
    """Named, so the list is a fact rather than a shrug.

    Every one of these is a body `_to_result`, `version()` or `task_profiles()` reads. All three of
    this milestone's live defects were in exactly these.
    """
    schemas = _response_schemas()
    depended_on = [
        "POST /api/v1/generate",
        "GET /api/v1/version",
        "GET /api/v1/task-profiles",
        "POST /api/v1/jobs",
        "GET /api/v1/jobs/{job_id}",
        "POST /api/v1/jobs/{job_id}/feedback",
    ]
    for name in depended_on:
        assert name in schemas, f"{name} left the snapshot; the adapter still calls it"
        schema = schemas[name]
        assert not (isinstance(schema, dict) and schema.get("properties")), (
            f"{name} is now described — validate the mock against it"
        )


def test_the_snapshot_still_describes_which_endpoints_exist() -> None:
    """It is not useless. A path the adapter calls that the snapshot does not list is a real
    finding, and that check keeps working."""
    paths = _document().get("paths", {})
    assert "/api/v1/generate" in paths
    assert "/api/v1/task-profiles" in paths
    assert len(paths) >= 25
