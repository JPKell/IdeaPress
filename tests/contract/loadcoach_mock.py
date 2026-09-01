"""A LoadCoach driven by its own committed OpenAPI snapshot, not by memory.

Testing Standards §8.4 wants a consumer's contract tests driven by the producer's committed
snapshot rather than by a hand-written double, because a hand-written double encodes what the
consumer's author *believed* the contract was — which is precisely the thing under test.

**Every request this mock receives is validated against the snapshot's request schema before it is
answered**, `additionalProperties: false` included. That is not decoration: LoadCoach's
`GenerateBody` really is `extra="forbid"`, so a field IdeaPress invents is a 422 in production and
must be a failure here.

Responses are the documented ones from [LoadCoach api.md §4–§6], which the snapshot itself types
only as "an object" — so the *requests* are schema-enforced and the *responses* are fixtures. That
asymmetry is the snapshot's, not a shortcut: it is also the reason `test_loadcoach_live.py` exists.

**Where the snapshot comes from.** Testing Standards §8.4 says the producer ships it as package
data, loadable as `loadcoach.api_snapshot()`. `loadcoach 1.0.0` does not: the wheel contains no
`__init__.py`, no `api_snapshot`, and no `openapi*.json` (verified against the published
distribution, M8-01). So the snapshot is vendored beside this file and
:func:`assert_snapshot_matches_distribution` fails the moment a `loadcoach` that *does* ship one
disagrees with the copy — the vendored file cannot rot silently, and it stops being needed the day
the standard is met.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "SNAPSHOT_PATH",
    "MockLoadCoach",
    "RecordedRequest",
    "assert_snapshot_matches_distribution",
    "load_snapshot",
]

SNAPSHOT_PATH = Path(__file__).parent / "loadcoach_openapi_v1.json"

LOADCOACH_VERSION = "1.0.0"
DEFAULT_MODEL_CANONICAL_ID = "ollama/qwen3.5:9b-q8_0@sha256:1f3a9c4e2b70"


def load_snapshot() -> dict[str, Any]:
    """Return LoadCoach's committed OpenAPI document.

    Returns:
        The parsed snapshot, preferring one shipped by an installed ``loadcoach`` distribution and
        falling back to the vendored copy.
    """
    shipped = _distribution_snapshot()
    if shipped is not None:
        return shipped
    loaded = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _distribution_snapshot() -> dict[str, Any] | None:
    """The snapshot an installed ``loadcoach`` ships, or ``None`` when it ships none.

    Returns:
        The parsed document, or ``None``. Never raises: an absent producer distribution is the
        normal state of this repository's default CI path, which is the whole point of the
        standalone gold standard.
    """
    try:  # pragma: no cover - exercised only where the extra is installed
        import loadcoach  # type: ignore[import-not-found]  # test-only, via [loadcoach-contract]
    except ImportError:
        return None
    snapshot = getattr(loadcoach, "api_snapshot", None)
    if callable(snapshot):  # pragma: no cover - loadcoach 1.0.0 exposes none
        loaded = snapshot()
        return dict(loaded) if isinstance(loaded, dict) else None
    return None


def assert_snapshot_matches_distribution() -> None:
    """Fail if an installed ``loadcoach`` ships a snapshot the vendored copy disagrees with.

    Raises:
        AssertionError: The producer's shipped snapshot and the vendored copy differ. The vendored
            copy is then stale and must be refreshed from the distribution — this is the mechanism
            that stops a workaround for a missing package artifact from becoming a private fork of
            somebody else's contract.
    """
    shipped = _distribution_snapshot()
    if shipped is None:
        return
    vendored = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert shipped == vendored, (
        "the installed loadcoach ships an OpenAPI snapshot that differs from the vendored copy; "
        f"refresh {SNAPSHOT_PATH.name} from the distribution"
    )


class SchemaViolation(AssertionError):
    """A request did not satisfy the producer's own schema for it."""


def _resolve(schema: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one ``$ref`` against the document; other schemas pass through unchanged."""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return dict(schema)
    node: Any = document
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return dict(node)


def validate(
    instance: Any, schema: Mapping[str, Any], document: Mapping[str, Any], path: str = ""
) -> None:
    """Validate ``instance`` against ``schema``, raising on the first violation.

    Args:
        instance: The decoded request body, or part of one.
        schema: The (possibly ``$ref``-bearing) schema node.
        document: The whole OpenAPI document, for resolving references.
        path: Where in the body this node sits, for the error message.

    Raises:
        SchemaViolation: The instance does not satisfy the schema. Covers the subset of JSON Schema
            LoadCoach's snapshot actually uses — types, ``required``, ``enum``, ``pattern``,
            numeric and length bounds, ``anyOf``, arrays, and crucially
            ``additionalProperties: false``, which is what makes an invented field a failure here
            as it is in production.
    """
    schema = _resolve(schema, document)
    where = path or "<body>"

    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate(instance, option, document, path)
            except SchemaViolation:
                continue
            return
        message = f"{where}: {instance!r} matches none of the permitted shapes"
        raise SchemaViolation(message)

    declared = schema.get("type")
    if declared is not None:
        _check_type(instance, declared, where)

    if declared == "object" or isinstance(instance, dict):
        _check_object(instance, schema, document, where)
    if declared == "array" and isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                validate(item, item_schema, document, f"{where}[{index}]")

    if "enum" in schema and instance not in schema["enum"]:
        message = f"{where}: {instance!r} is not one of {schema['enum']}"
        raise SchemaViolation(message)
    if "pattern" in schema and isinstance(instance, str):
        if not re.search(schema["pattern"], instance):
            message = f"{where}: {instance!r} does not match {schema['pattern']!r}"
            raise SchemaViolation(message)
    if "maxLength" in schema and isinstance(instance, str) and len(instance) > schema["maxLength"]:
        message = f"{where}: longer than maxLength {schema['maxLength']}"
        raise SchemaViolation(message)
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            message = f"{where}: {instance} is below minimum {schema['minimum']}"
            raise SchemaViolation(message)
        if "maximum" in schema and instance > schema["maximum"]:
            message = f"{where}: {instance} is above maximum {schema['maximum']}"
            raise SchemaViolation(message)


def _check_type(instance: Any, declared: Any, where: str) -> None:
    """Raise unless ``instance`` has the declared JSON type."""
    kinds = declared if isinstance(declared, list) else [declared]
    matchers: dict[str, Any] = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "null": type(None),
    }
    for kind in kinds:
        if kind == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return
        if (
            kind == "number"
            and isinstance(instance, (int, float))
            and not isinstance(instance, bool)
        ):
            return
        expected = matchers.get(str(kind))
        if expected is not None and isinstance(instance, expected):
            if expected is not bool and isinstance(instance, bool):
                continue
            return
    message = f"{where}: {type(instance).__name__} is not {declared}"
    raise SchemaViolation(message)


def _check_object(
    instance: Any, schema: Mapping[str, Any], document: Mapping[str, Any], where: str
) -> None:
    """Check required keys, per-property schemas and ``additionalProperties: false``."""
    if not isinstance(instance, dict):
        return
    for key in schema.get("required", []):
        if key not in instance:
            message = f"{where}: required property {key!r} is missing"
            raise SchemaViolation(message)
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(instance) - set(properties))
        if unexpected:
            message = (
                f"{where}: {', '.join(unexpected)} — LoadCoach forbids additional properties on "
                "this body, so this would be a 422 in production"
            )
            raise SchemaViolation(message)
    for key, value in instance.items():
        sub = properties.get(key)
        if isinstance(sub, dict):
            validate(value, sub, document, f"{where}.{key}")


class RecordedRequest:
    """One request the mock received, kept so a test can assert what was actually sent.

    Attributes:
        method: HTTP method.
        path: Path below the base URL.
        body: The decoded JSON body, or ``None``.
        headers: The request headers.
    """

    def __init__(self, method: str, path: str, body: Any, headers: Mapping[str, str]) -> None:
        self.method = method
        self.path = path
        self.body = body
        self.headers = dict(headers)

    def __repr__(self) -> str:
        return f"RecordedRequest({self.method} {self.path})"


class MockLoadCoach:
    """A LoadCoach whose request validation comes from LoadCoach's own snapshot.

    Attributes:
        requests: Every request received, in order.
        jobs: Job records by id, so a poll returns what the submission created.
    """

    def __init__(
        self,
        *,
        answers: Sequence[str] | None = None,
        version: str = LOADCOACH_VERSION,
        api_versions: Sequence[str] = ("1.0",),
        task_profiles: Sequence[str] | None = None,
        model_canonical_id: str = DEFAULT_MODEL_CANONICAL_ID,
        routing_flags: Sequence[str] = (),
        degradations: Sequence[Any] = (),
        queue_wait_ms: float = 0.0,
    ) -> None:
        """Configure a LoadCoach to answer with ``answers``, in order, repeating the last.

        Args:
            answers: The texts to return, one per generation.
            version: The application version ``GET /version`` reports.
            api_versions: The API versions it reports, for negotiation tests.
            task_profiles: What ``GET /task-profiles`` serves; defaults to every profile the
                adapter's task map names, so the totality check passes unless a test breaks it.
            model_canonical_id: Which model it says answered.
            routing_flags: Flags on the routing decision, for the degradation tests.
            degradations: LoadCoach's own reported degradations.
            queue_wait_ms: Reported queue wait, for the queue-visibility test.
        """
        from ideapress.infrastructure.backends.loadcoach import LOADCOACH_TASK_MAP

        self.document = load_snapshot()
        self.requests: list[RecordedRequest] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.feedback: dict[str, list[Any]] = {}
        self._answers = list(answers or ["an answer"])
        self._answer_index = 0
        self._version = version
        self._api_versions = list(api_versions)
        self._profiles = list(
            task_profiles if task_profiles is not None else sorted(set(LOADCOACH_TASK_MAP.values()))
        )
        self._model_canonical_id = model_canonical_id
        self._routing_flags = list(routing_flags)
        self._degradations = list(degradations)
        self._queue_wait_ms = queue_wait_ms
        self._job_sequence = 0
        self.replayed_keys: list[str] = []
        self._by_idempotency_key: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ wiring

    def client(self, base_url: str = "http://127.0.0.1:8766") -> httpx.Client:
        """An :class:`httpx.Client` whose transport is this mock."""
        return httpx.Client(
            base_url=base_url, transport=httpx.MockTransport(self.handle), timeout=30.0
        )

    def next_answer(self) -> str:
        """The next scripted answer, repeating the final one once they run out."""
        if self._answer_index < len(self._answers):
            answer = self._answers[self._answer_index]
            self._answer_index += 1
            return answer
        return self._answers[-1] if self._answers else ""

    # ------------------------------------------------------------------ routing

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request, validating its body against the producer's schema first."""
        path = request.url.path
        body: Any = None
        if request.content:
            body = json.loads(request.content)
        self.requests.append(RecordedRequest(request.method, path, body, request.headers))

        if request.method == "GET" and path == "/api/v1/version":
            return self._json({"version": self._version, "api_versions": self._api_versions})
        if request.method == "GET" and path == "/api/v1/task-profiles":
            return self._json({"task_profiles": [{"id": name} for name in self._profiles]})
        if request.method == "GET" and path == "/api/v1/models":
            return self._json(
                {"items": [{"canonical_id": self._model_canonical_id, "served_context": 32768}]}
            )
        if request.method == "GET" and path == "/api/v1/reliability":
            return self._json({"items": [{"task": "content.review", "samples": 1}]})
        if request.method == "POST" and path == "/api/v1/generate":
            self._validate_body(body, "GenerateBody")
            return self._json(self._completion(body))
        if request.method == "POST" and path == "/api/v1/generate/stream":
            self._validate_body(body, "GenerateBody")
            return self._stream(body)
        if request.method == "POST" and path == "/api/v1/jobs":
            self._validate_body(body, "JobBody")
            return self._submit_job(body)
        if request.method == "GET" and path.startswith("/api/v1/jobs/"):
            return self._job_state(path)
        if request.method == "POST" and path.endswith("/feedback"):
            self._validate_body(body, "FeedbackBody")
            job_id = path.split("/")[-2]
            self.feedback.setdefault(job_id, []).append(body)
            first = len(self.feedback[job_id]) == 1
            return self._json({"job_id": job_id, "stored": True}, status=201 if first else 200)
        return self._json({"error": {"code": "NOT_FOUND", "message": path}}, status=404)

    def _validate_body(self, body: Any, schema_name: str) -> None:
        """Validate one request body against the named component schema."""
        schema = self.document["components"]["schemas"][schema_name]
        validate(body, schema, self.document, f"<{schema_name}>")

    @staticmethod
    def _json(payload: Mapping[str, Any], *, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=dict(payload))

    # ------------------------------------------------------------------ bodies

    def _completion(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """The documented `/generate` response for one submitted body (api.md §4)."""
        key = body.get("idempotency_key")
        if isinstance(key, str) and key in self._by_idempotency_key:
            self.replayed_keys.append(key)
            return self._by_idempotency_key[key]
        self._job_sequence += 1
        job_id = f"01J9K{self._job_sequence:04d}"
        payload: dict[str, Any] = {
            "job_id": job_id,
            "status": "completed",
            "output": {"text": self.next_answer(), "structured": None, "tool_calls": []},
            "reasoning": {"available": False, "summary": None, "source": None},
            "model": {
                "canonical_id": self._model_canonical_id,
                "model_ref": "01J9KMODEL",
                "runtime_profile_hash": "8f2c",
                "served_context": 32768,
                "served_context_source": "configured",
                "target_gpu_index": 0,
            },
            "routing": {
                "decision_id": f"01J9KDEC{self._job_sequence:03d}",
                "rank": 1,
                "final_score": 0.71,
                "flags": list(self._routing_flags),
                "explanation_url": f"/api/v1/jobs/{job_id}/explanation",
            },
            "usage": {"input_tokens": 812, "output_tokens": 1104, "thinking_tokens": "unsupported"},
            "timing": {
                "total_ms": 18422,
                "provider_ms": 18310,
                "loadcoach_overhead_ms": 112,
                "ttft_ms": 640,
                "queue_wait_ms": self._queue_wait_ms,
            },
            "validation": {"performed": False, "passed": None, "attempts": 1},
            "attempts": [{"attempt": 1, "model": self._model_canonical_id, "outcome": "completed"}],
            "degradations": list(self._degradations),
        }
        if isinstance(key, str):
            self._by_idempotency_key[key] = payload
        return payload

    def _submit_job(self, body: Mapping[str, Any]) -> httpx.Response:
        """Accept a job and return `202` with it, as api.md §5 states."""
        payload = self._completion(body)
        self.jobs[str(payload["job_id"])] = payload
        return self._json({"job_id": payload["job_id"], "status": "queued"}, status=202)

    def _job_state(self, path: str) -> httpx.Response:
        """Return a submitted job, completed. The adapter polls until a terminal state."""
        job_id = path.rsplit("/", 1)[-1]
        job = self.jobs.get(job_id)
        if job is None:
            return self._json({"error": {"code": "NOT_FOUND", "message": job_id}}, status=404)
        return self._json(job)

    def _stream(self, body: Mapping[str, Any]) -> httpx.Response:
        """An SSE body: enveloped `routing`, bare `token` frames, then an enveloped `result`."""
        payload = self._completion(body)
        text = str(payload["output"]["text"])

        def frames() -> Iterator[bytes]:
            envelope = {
                "schema": "event.envelope",
                "schema_version": "1.0",
                "payload": payload["routing"],
            }
            yield f"event: routing\ndata: {json.dumps(envelope)}\n\n".encode()
            for index, chunk in enumerate(text.split(" ")):
                token = json.dumps({"delta": chunk + " ", "index": index})
                yield f"event: token\ndata: {token}\n\n".encode()
            result = {
                "schema": "event.envelope",
                "schema_version": "1.0",
                "payload": payload,
            }
            yield f"event: result\ndata: {json.dumps(result)}\n\n".encode()

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"".join(frames())
        )
