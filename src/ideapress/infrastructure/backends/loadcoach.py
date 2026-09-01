"""ideapress.infrastructure.backends.loadcoach — the optional LoadCoach backend.

The **only** module in IdeaPress that knows a LoadCoach task identifier, an endpoint path or a
field name of LoadCoach's API. Workflow code above the port keeps speaking IdeaPress's stage
vocabulary and never learns that a queue exists; `.importlinter`'s `no-other-applications` contract
keeps `from loadcoach import …` impossible under `src/`, so this adapter speaks HTTP against the
documented `/api/v1` and nothing else.

Three decisions shape everything here, each recorded rather than assumed:

* **LoadCoach chooses the model** (ADR-0040, `docs/adr/0040-routing-backend-owns-model-choice.md`).
  The adapter reports ``routes_internally``, so the gateway resolves no `[models.stages]` binding
  and unloads nothing. A model override is sent only when the user asked for one with
  `[inference.loadcoach] honour_stage_bindings`, and a pin that was not honoured is a degradation,
  not a failure.
* **IdeaPress's schema does not travel** (ADR-0041,
  `docs/adr/0041-caller-schemas-do-not-travel-through-a-router.md`).
  LoadCoach applies the *task profile's* schema, which for `content.review` cannot express
  ADR-0039's attestation at all. So this adapter asks for ``json``, reports
  ``structured_output=False`` honestly, and lets IdeaPress validate the shape as it always does.
* **The prompt is forwarded unmodified**, which LoadCoach guarantees
  (LoadCoach api.md §4) and this adapter must
  not undermine. `system` and `user` go across verbatim, with nothing prepended, so the attempt's
  `prompt_sha256` provenance stays true.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Final

import httpx

from ideapress.__about__ import __version__
from ideapress.domain.inference import (
    BackendCapabilities,
    BackendHealth,
    BackendModel,
    ModelIdentity,
    StageEvent,
    StageResult,
    Timing,
    TokenUsage,
)
from ideapress.domain.stages import MODEL_STAGES
from ideapress.errors import (
    BackendUnavailable,
    BackendVersionMismatch,
    ContentRejected,
    ContextLimitExceeded,
    ProviderTimeout,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from ideapress.config import LoadCoachSettings
    from ideapress.domain.inference import StageRequest

__all__ = ["LOADCOACH_TASK_MAP", "SUPPORTED_API_MAJOR", "LoadCoachBackend"]

logger = logging.getLogger(__name__)

LOADCOACH_TASK_MAP: Final[Mapping[str, str]] = {
    "requirements": "structured.extract",
    "research_synthesis": "content.research_synthesis",
    "outline": "content.outline",
    "draft": "content.article_draft",
    "repair": "content.rewrite",
    "audit_fast": "content.review",
    "audit_deep": "content.review",
    "fact_check": "content.fact_check",
    "critique": "general.reasoning",
    "revise": "content.edit",
    "project_review": "general.reasoning",
}
"""IdeaPress stage → LoadCoach task profile, transcribed from workflows §6.

This mapping exists in exactly one module, and a test greps the tree to keep it that way: a task
identifier appearing anywhere else is coupling, and coupling to another application's vocabulary is
risk I2's named failure mode.

The audits map to **`content.review`**, not `code.review`. The suite audit found the earlier
mapping annotated "generic review profile", which `code.review` is not: it weights measured
`code_review` capability at 0.45, imposes `min_capability_scores = {code_review: 0.35}` as a hard
constraint, and declares a code-review JSON schema with `required_fields = ["findings", "summary"]`.
Routing an article audit through it would have filtered candidate models on their ability to review
*code* and forced a code-review shape onto prose findings — a cross-application defect invisible
from either side alone.
"""

SUPPORTED_API_MAJOR: Final[int] = 1
"""The `/api/v1` major this adapter speaks. A different major is refused, never downgraded to."""

_VERSION_CACHE_SECONDS: Final[float] = 300.0
_JOB_POLL_SECONDS: Final[float] = 0.25
_REFUSAL_MARKERS: Final[tuple[str, ...]] = (
    "i cannot help",
    "i can't help",
    "i cannot assist",
    "i won't",
    "i will not",
    "as an ai",
)
_REFUSAL_MAX_CHARS: Final[int] = 400


def task_for(stage: str) -> str:
    """Return the LoadCoach task profile a stage routes through.

    Args:
        stage: An IdeaPress stage identifier from workflows §2.

    Returns:
        The task profile identifier.

    Raises:
        ModelNotConfigured: ``stage`` uses no model, or is not a stage at all. The map is total
            over the model-using stages by construction and a test asserts it; this refuses the
            caller that asks for a gate stage rather than inventing a profile for one.
    """
    from ideapress.errors import ModelNotConfigured

    try:
        return LOADCOACH_TASK_MAP[stage]
    except KeyError:
        message = (
            f"The {stage!r} stage has no LoadCoach task profile. Only the model-using stages "
            f"route through LoadCoach: {', '.join(sorted(LOADCOACH_TASK_MAP))}."
        )
        raise ModelNotConfigured(message, details={"stage": stage}) from None


def idempotency_key_for(request: StageRequest) -> str:
    """Build the per-attempt idempotency key for one submission.

    Args:
        request: The stage request about to be submitted.

    Returns:
        A key of the form ``ideapress-<32 hex>``, stable for an identical request and different for
        any other.

    The digest covers the attempt's **coordinates and its content** — project, unit, stage, attempt,
    round, and the rendered `system` and `user` text. Coordinates alone would be wrong in a way that
    is hard to see: LoadCoach reserves a key for `queue.idempotency_ttl_hours` (24 by default) and
    replays the original job for a repeat, so a project resumed within a day would replay a stale
    answer for a `repair` whose findings had changed since. Including the prompt makes a retry of
    the *same* request idempotent — which is all the key is for — while a genuinely different
    request is genuinely new work.
    """
    correlation = request.correlation
    material = "\x1f".join(
        (
            correlation.project_id,
            correlation.unit_id or "",
            request.stage,
            str(correlation.attempt),
            str(correlation.round),
            request.system,
            request.user,
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"ideapress-{digest}"


def _detect_refusal(text: str) -> str | None:
    """Return the model's refusal, if this short answer is one; never classifies a long one."""
    if len(text) > _REFUSAL_MAX_CHARS:
        return None
    lowered = text.lower()
    return text.strip() if any(marker in lowered for marker in _REFUSAL_MARKERS) else None


def _as_int(value: object) -> int:
    """A token count, with anything unreported or non-numeric read as 0."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _as_optional_float(value: object) -> float | None:
    """A millisecond timing, or ``None`` when LoadCoach did not report one.

    ``None`` means *not reported*, never zero: LoadCoach sends the string ``"unsupported"`` for a
    measurement it could not take (ADR-0016), and coercing that to ``0.0`` would put a fabricated
    number into a provenance record.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class LoadCoachBackend:
    """The inference port over a running LoadCoach.

    Honest about what it is: it does not enforce the caller's schema (ADR-0041), it does not choose
    the model (ADR-0040), and it cannot control residency — LoadCoach owns all three, which is the
    point of using it.
    """

    name = "loadcoach"

    def __init__(
        self,
        settings: LoadCoachSettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """Bind to a configured LoadCoach.

        Args:
            settings: The `[inference.loadcoach]` section.
            client: An already-built HTTP client, injected by tests. When absent one is built from
                ``settings``; the base URL and timeout are the configured ones.
        """
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        token = os.environ.get(settings.api_key_env, "") if settings.api_key_env else ""
        self._token = token or None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(float(settings.timeout_seconds), connect=10.0),
        )
        self._version: dict[str, Any] | None = None
        self._version_checked_at: float = 0.0

    # ---------------------------------------------------------------- transport

    def _headers(self, *, request_id: str | None = None) -> dict[str, str]:
        """The headers every call carries.

        ``X-Client-Name`` is what attributes jobs, idempotency keys and feedback to IdeaPress on an
        unauthenticated loopback bind (LoadCoach api.md §12.4); ``X-Request-ID`` is what makes one
        trace span both applications.
        """
        headers = {
            "X-Client-Name": "ideapress",
            "X-Request-ID": request_id or str(uuid.uuid4()),
            "User-Agent": f"ideapress/{__version__}",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Make one API call and return its decoded body.

        Args:
            method: HTTP method.
            path: Path below ``/api/v1``.
            json_body: The request body, when there is one.
            request_id: Correlation id to propagate.

        Returns:
            The decoded JSON object.

        Raises:
            BackendUnavailable: LoadCoach did not answer, or answered with a server error.
            ProviderTimeout: It accepted the request and did not answer in time.
            ContentRejected: LoadCoach refused the request (4xx that is not a timeout).
        """
        url = f"{self._base_url}/api/v1{path}"
        try:
            response = self._client.request(
                method, url, json=json_body, headers=self._headers(request_id=request_id)
            )
        except httpx.TimeoutException as exc:
            message = (
                f"LoadCoach at {self._base_url} accepted the request and did not answer in time."
            )
            raise ProviderTimeout(
                message, details={"backend": self.name, "base_url": self._base_url, "path": path}
            ) from exc
        except httpx.HTTPError as exc:
            message = f"LoadCoach at {self._base_url} did not answer: {exc}"
            raise BackendUnavailable(
                message, details={"backend": self.name, "base_url": self._base_url, "path": path}
            ) from exc
        return self._decode(response, path=path)

    def _decode(self, response: httpx.Response, *, path: str) -> dict[str, Any]:
        """Turn one HTTP response into a body, or into IdeaPress's own error vocabulary.

        Raises:
            BackendUnavailable: A 5xx, or a body that is not a JSON object.
            ContextLimitExceeded: LoadCoach reported the request too large for any candidate.
            ContentRejected: Any other 4xx, carrying LoadCoach's own error code and message so the
                user reads what LoadCoach said rather than a paraphrase of it.
        """
        details: dict[str, Any] = {
            "backend": self.name,
            "base_url": self._base_url,
            "path": path,
            "status_code": response.status_code,
        }
        if response.status_code >= 500:
            message = f"LoadCoach failed with HTTP {response.status_code} on {path}."
            raise BackendUnavailable(message, details=details)
        if response.status_code >= 400:
            code, detail = self._error_of(response)
            details["loadcoach_code"] = code
            message = f"LoadCoach refused the request ({code}): {detail}"
            if code in {"CONTEXT_LIMIT_EXCEEDED", "INSUFFICIENT_RESOURCES"}:
                raise ContextLimitExceeded(message, details=details)
            raise ContentRejected(message, details=details)
        try:
            body = response.json()
        except ValueError as exc:
            message = f"LoadCoach returned a body that is not JSON on {path}."
            raise BackendUnavailable(message, details=details) from exc
        if not isinstance(body, dict):
            message = f"LoadCoach returned {type(body).__name__}, not an object, on {path}."
            raise BackendUnavailable(message, details=details)
        return body

    @staticmethod
    def _error_of(response: httpx.Response) -> tuple[str, str]:
        """Read LoadCoach's standard error envelope, tolerating one that is not standard."""
        try:
            body = response.json()
        except ValueError:
            return ("HTTP_ERROR", response.text[:300])
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                return (str(error.get("code", "HTTP_ERROR")), str(error.get("message", "")))
            if "detail" in body:
                return ("VALIDATION_ERROR", str(body["detail"])[:300])
        return ("HTTP_ERROR", str(body)[:300])

    # ---------------------------------------------------------------- negotiation

    def version(self) -> dict[str, Any]:
        """Negotiate the API version on first contact, cached with a TTL.

        Returns:
            LoadCoach's ``GET /version`` body.

        Raises:
            BackendUnavailable: LoadCoach did not answer.
            BackendVersionMismatch: Its API major is not the one this adapter speaks. Names both
                versions and refuses; there is no silent downgrade, because an adapter that
                guesses at a contract it does not know is how a wrong field becomes a wrong
                provenance record.

        ``GET /version`` is never authenticated on LoadCoach's side — negotiation precedes
        credentials (ADR-0026 §5) — so this succeeds even when a token is missing or wrong, and the
        version mismatch is reported as a version mismatch rather than as a 401.
        """
        now = time.monotonic()
        if self._version is not None and (now - self._version_checked_at) < _VERSION_CACHE_SECONDS:
            return self._version
        body = self._call("GET", "/version")
        majors = self._api_majors(body)
        if SUPPORTED_API_MAJOR not in majors:
            theirs = ", ".join(str(m) for m in sorted(majors)) or "none reported"
            message = (
                f"LoadCoach at {self._base_url} serves API major {theirs}; this IdeaPress "
                f"{__version__} speaks major {SUPPORTED_API_MAJOR}. Upgrade whichever is older; "
                "IdeaPress will not guess at a contract it does not know."
            )
            raise BackendVersionMismatch(
                message,
                details={
                    "backend": self.name,
                    "base_url": self._base_url,
                    "ideapress_api_major": SUPPORTED_API_MAJOR,
                    "loadcoach_api_majors": sorted(majors),
                    "loadcoach_version": body.get("version"),
                    "ideapress_version": __version__,
                },
            )
        self._version = body
        self._version_checked_at = now
        return body

    @staticmethod
    def _api_majors(body: Mapping[str, Any]) -> set[int]:
        """Read the API majors from ``GET /version``, accepting either documented spelling."""
        majors: set[int] = set()
        versions = body.get("api_versions")
        if isinstance(versions, (list, tuple)):
            for entry in versions:
                text = str(entry).lstrip("vV")
                head = text.split(".", 1)[0]
                if head.isdigit():
                    majors.add(int(head))
        single = body.get("api_version")
        if single is not None:
            text = str(single).lstrip("vV")
            head = text.split(".", 1)[0]
            if head.isdigit():
                majors.add(int(head))
        return majors

    def task_profiles(self) -> set[str]:
        """The task profile identifiers this LoadCoach actually serves.

        Returns:
            Every profile id from ``GET /task-profiles``.

        Raises:
            BackendUnavailable: LoadCoach did not answer.

        This is what makes :data:`LOADCOACH_TASK_MAP` checkable against the *running* system rather
        than against a document: a profile renamed on LoadCoach's side surfaces here, at backend
        test time, instead of as a `TASK_PROFILE_NOT_FOUND` in the middle of somebody's project.
        """
        body = self._call("GET", "/task-profiles")
        profiles = body.get("task_profiles", body.get("items", body.get("profiles")))
        found: set[str] = set()
        if isinstance(profiles, dict):
            found = {str(key) for key in profiles}
        elif isinstance(profiles, (list, tuple)):
            for entry in profiles:
                if isinstance(entry, dict):
                    identifier = entry.get("id", entry.get("task", entry.get("name")))
                    if identifier is not None:
                        found.add(str(identifier))
                else:
                    found.add(str(entry))
        return found

    def unmapped_task_profiles(self) -> list[str]:
        """Task profiles this adapter names that the running LoadCoach does not serve.

        Returns:
            The sorted difference, empty when every mapped profile exists. Empty is the passing
            case; anything else names exactly which stage would fail and why.

        Raises:
            BackendUnavailable: LoadCoach did not answer.
        """
        served = self.task_profiles()
        return sorted({task for task in LOADCOACH_TASK_MAP.values() if task not in served})

    # ---------------------------------------------------------------- port

    def capabilities(self) -> BackendCapabilities:
        """What LoadCoach can do for IdeaPress, reported honestly.

        ``structured_output`` is **False** and ``json_mode`` is True: LoadCoach enforces the task
        profile's schema, never the caller's, so claiming otherwise would be the exact pretence
        workflows §6.2 forbids (ADR-0041). ``routes_internally`` is True, which is what tells the
        gateway to resolve no binding and unload nothing (ADR-0040).
        """
        return BackendCapabilities(
            streaming=True,
            structured_output=False,
            json_mode=True,
            token_counts=True,
            model_selection=True,
            discloses_model=True,
            residency_control=False,
            routes_internally=True,
        )

    def health(self) -> BackendHealth:
        """Whether LoadCoach answers, and which version it is. Never raises.

        An unreachable LoadCoach is a reported status, never an exception and never a startup
        failure (spec §20 AC7) — opening projects and exporting committed content need no model.
        A version mismatch is reported here too, as ``degraded`` with both versions in the detail,
        because "it answers but we cannot talk to it" is a different thing from "it is down" and
        the user fixes them differently.
        """
        if not self._base_url:
            return BackendHealth(
                backend=self.name,
                status="not_configured",
                detail="inference.loadcoach.base_url is empty.",
            )
        started = time.perf_counter()
        try:
            version = self.version()
        except BackendVersionMismatch as exc:
            return BackendHealth(
                backend=self.name,
                status="degraded",
                detail=str(exc),
                base_url=self._base_url,
                is_remote=self._is_remote,
            )
        except (BackendUnavailable, ProviderTimeout, ContentRejected) as exc:
            return BackendHealth(
                backend=self.name,
                status="unavailable",
                detail=str(exc),
                base_url=self._base_url,
                is_remote=self._is_remote,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        return BackendHealth(
            backend=self.name,
            status="ok",
            detail=f"LoadCoach {version.get('version', 'unknown')}",
            base_url=self._base_url,
            is_remote=self._is_remote,
            latency_ms=round(latency_ms, 2),
            version=str(version.get("version", "")) or None,
        )

    @property
    def _is_remote(self) -> bool:
        """Whether this LoadCoach is off-machine, which the UI labels as egress (risk S4)."""
        host = httpx.URL(self._base_url).host if self._base_url else ""
        return host not in {"127.0.0.1", "localhost", "::1", ""}

    def list_models(self) -> Sequence[BackendModel]:
        """List the models LoadCoach can route to.

        Raises:
            BackendUnavailable: LoadCoach did not answer.
        """
        body = self._call("GET", "/models")
        entries = body.get("items", body.get("models", []))
        models: list[BackendModel] = []
        if isinstance(entries, (list, tuple)):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
                canonical = str(
                    entry.get("canonical_id") or (identity or {}).get("canonical_id") or ""
                )
                name = canonical or str(entry.get("name", entry.get("model_ref", "")))
                if not name:
                    continue
                models.append(
                    BackendModel(
                        name=name,
                        identity=_identity_of({"canonical_id": canonical} if canonical else {}),
                        max_context=(
                            int(entry["served_context"])
                            if isinstance(entry.get("served_context"), int)
                            else None
                        ),
                    )
                )
        return models

    def _body_for(self, request: StageRequest) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Build the request body, and the degradations building it implied.

        Args:
            request: The stage request.

        Returns:
            The body for ``/generate`` or ``/jobs``, and the degradations to record on the attempt.

        Only fields LoadCoach's committed OpenAPI snapshot declares are sent: ``GenerateBody`` is
        ``extra="forbid"``, so an undeclared field is a 422 rather than something ignored. The
        prompt goes across verbatim in ``system`` and ``prompt``, which is what LoadCoach forwards
        to the provider unmodified and what the attempt's `prompt_sha256` records.
        """
        degradations: list[str] = []
        body: dict[str, Any] = {
            "task": task_for(request.stage),
            "system": request.system,
            "prompt": request.user,
            "idempotency_key": idempotency_key_for(request),
            # Budgets travel here and only here. Omitting `max_output_tokens` would silently hand
            # the stage the *task profile's* default — 2048 for `structured.extract`, against
            # IdeaPress's configured `workflow.structured_output_tokens` of 8192 — and the stage
            # would fail as an empty generation with nothing naming the cause.
            "sampling": {
                "temperature": request.limits.temperature,
                "max_output_tokens": request.limits.max_output_tokens,
            },
        }
        if request.limits.seed is not None:
            body["sampling"]["seed"] = request.limits.seed

        response_format = request.response_format
        if response_format is not None and response_format.kind in {"json", "json_schema"}:
            body["response_format"] = "json"
            if response_format.kind == "json_schema":
                degradations.append(
                    "structured_output_unavailable: LoadCoach applies the task profile's schema, "
                    "not the caller's, so no schema was enforced for IdeaPress's shape; the "
                    "answer was requested as JSON and validated by IdeaPress (ADR-0041)"
                )

        if self._settings.honour_stage_bindings and request.model_hint:
            body["overrides"] = {"model": request.model_hint}
        return body, tuple(degradations)

    def generate(self, request: StageRequest) -> StageResult:
        """Run one bounded model task to completion.

        Args:
            request: The stage request, in IdeaPress's vocabulary.

        Returns:
            The result, carrying routing metadata, usage, timings and every degradation that
            applied.

        Raises:
            BackendUnavailable: LoadCoach did not answer.
            BackendVersionMismatch: Its API major is not one this adapter speaks.
            ProviderTimeout: It accepted the request and did not answer in time.
            ContextLimitExceeded: The request exceeds what any candidate model serves.
            ContentRejected: LoadCoach refused the request.

        Interactive stages go through synchronous ``/generate``; the long ones named by
        `[inference.loadcoach] job_stages` go through ``/jobs`` with ``class = "interactive"``
        left off, so a person waiting on a `critique` is never queued behind somebody's `draft`.
        """
        self.version()
        body, degradations = self._body_for(request)
        request_id = request.correlation.request_id
        if request.stage in self._settings.job_stages:
            payload = self._run_as_job(body, request_id=request_id)
        else:
            payload = self._call("POST", "/generate", json_body=body, request_id=request_id)
        return self._to_result(payload, request=request, degradations=degradations)

    def _run_as_job(self, body: Mapping[str, Any], *, request_id: str | None) -> Mapping[str, Any]:
        """Submit a job, wait for it, and return the finished job.

        Args:
            body: The generate body, which ``/jobs`` accepts as-is plus its own fields.
            request_id: Correlation id to propagate.

        Returns:
            The terminal job record.

        Raises:
            BackendUnavailable: LoadCoach did not answer, or the job failed.
            ProviderTimeout: The job did not reach a terminal state inside the configured timeout.
        """
        submission = dict(body)
        submission["class"] = "normal"
        submission["idempotent"] = True
        job = self._call("POST", "/jobs", json_body=submission, request_id=request_id)
        job_id = str(job.get("job_id", job.get("id", "")))
        if not job_id:
            message = "LoadCoach accepted a job and returned no job id."
            raise BackendUnavailable(
                message, details={"backend": self.name, "base_url": self._base_url}
            )
        deadline = time.monotonic() + float(self._settings.timeout_seconds)
        while time.monotonic() < deadline:
            job = self._call("GET", f"/jobs/{job_id}", request_id=request_id)
            state = str(job.get("status", job.get("state", "")))
            if state in {"completed", "failed", "cancelled"}:
                if state != "completed":
                    message = (
                        f"LoadCoach job {job_id} ended {state}: "
                        f"{job.get('error', {}) or 'no reason reported'}"
                    )
                    raise BackendUnavailable(
                        message,
                        details={
                            "backend": self.name,
                            "job_id": job_id,
                            "state": state,
                        },
                    )
                return job
            time.sleep(_JOB_POLL_SECONDS)
        message = (
            f"LoadCoach job {job_id} did not finish within "
            f"{self._settings.timeout_seconds} s. It is still queued or running; the project is "
            "resumable and the job was not cancelled."
        )
        raise ProviderTimeout(
            message, details={"backend": self.name, "job_id": job_id, "base_url": self._base_url}
        )

    def _to_result(
        self,
        payload: Mapping[str, Any],
        *,
        request: StageRequest,
        degradations: tuple[str, ...],
    ) -> StageResult:
        """Translate LoadCoach's response into the port's result.

        Args:
            payload: A ``/generate`` response or a terminal job record — the same shape.
            request: The request that produced it, for the pin check.
            degradations: What building the request already implied.

        Returns:
            The stage result, with routing metadata attached and every degradation LoadCoach
            reported folded in alongside IdeaPress's own.
        """
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        text = str((output or {}).get("text") or "")
        structured = (output or {}).get("structured")
        model_body = payload.get("model") if isinstance(payload.get("model"), dict) else {}
        routing_body = payload.get("routing")
        routing: dict[str, Any] | None = (
            dict(routing_body) if isinstance(routing_body, dict) else None
        )
        if routing is not None:
            # The job id and the key that made the submission idempotent are provenance,
            # not routing — but `routing` is the one mapping the port carries through to
            # the attempt record, and feedback (P7 AC4) needs the job id to reach the job
            # it is about. Carried here rather than by widening the port for one backend.
            routing["job_id"] = str(payload.get("job_id", ""))
            routing["idempotency_key"] = idempotency_key_for(request)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}

        collected = [*degradations, *_reported_degradations(payload)]
        collected.extend(_routing_degradations(routing))
        collected.extend(
            self._pin_degradation(request=request, model_body=model_body or {}),
        )
        queue_wait_ms = _as_optional_float((timing or {}).get("queue_wait_ms"))
        if queue_wait_ms:
            collected.append(
                f"queue_wait: LoadCoach queued this attempt for {queue_wait_ms:.0f} ms "
                "before it ran"
            )

        return StageResult(
            text=text,
            structured=structured,
            model=_identity_of(model_body or {}),
            usage=TokenUsage(
                input_tokens=_as_int((usage or {}).get("input_tokens")),
                output_tokens=_as_int((usage or {}).get("output_tokens")),
                thinking_tokens=(
                    _as_int((usage or {}).get("thinking_tokens"))
                    if isinstance((usage or {}).get("thinking_tokens"), (int, float))
                    else None
                ),
            ),
            timing=Timing(
                duration_ms=_as_optional_float((timing or {}).get("total_ms")),
                ttft_ms=_as_optional_float((timing or {}).get("ttft_ms")),
                queue_wait_ms=queue_wait_ms,
            ),
            backend=self.name,
            routing=routing,
            degradations=tuple(collected),
            finish_reason=str(payload.get("finish_reason") or "stop"),
            refusal_reason=_detect_refusal(text),
        )

    def _pin_degradation(
        self, *, request: StageRequest, model_body: Mapping[str, Any]
    ) -> list[str]:
        """Report a model pin that LoadCoach did not honour (ADR-0040 §5).

        A pin is a request, not a guarantee: LoadCoach falling back to a working model beats a
        failed stage. But the user asked for something specific and did not get it, so it is said
        out loud on the attempt rather than left to be inferred from the provenance record.
        """
        if not (self._settings.honour_stage_bindings and request.model_hint):
            return []
        answered = str(model_body.get("canonical_id") or "")
        if not answered:
            return []
        wanted = request.model_hint
        if wanted in answered or answered.split("@", 1)[0].endswith(wanted.split("/")[-1]):
            return []
        return [
            f"model_override_not_honoured: asked LoadCoach for {wanted}, {answered} answered. "
            "Routing chose otherwise; the pin is a request, not a guarantee."
        ]

    def stream(self, request: StageRequest) -> Iterator[StageEvent]:
        """Run one bounded model task, yielding frames as they arrive.

        Args:
            request: The stage request.

        Yields:
            ``token`` frames as they arrive, then one ``completed`` frame carrying the same
            :class:`~ideapress.domain.inference.StageResult` :meth:`generate` would have returned,
            or one ``failed`` frame carrying an error code.

        Raises:
            BackendUnavailable: LoadCoach did not answer at all.
            BackendVersionMismatch: Its API major is not one this adapter speaks.

        Every frame but ``token`` carries LoadCoach's SetSpec event envelope; ``token`` is bare,
        which is the one documented exception (ADR-0025 §3) and the reason the two are parsed
        differently here.
        """
        self.version()
        body, degradations = self._body_for(request)
        url = f"{self._base_url}/api/v1/generate/stream"
        try:
            with self._client.stream(
                "POST",
                url,
                json=body,
                headers=self._headers(request_id=request.correlation.request_id),
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    self._decode(response, path="/generate/stream")
                yield from self._events_of(response, request=request, degradations=degradations)
        except httpx.TimeoutException as exc:
            message = f"LoadCoach at {self._base_url} stopped sending events before finishing."
            raise ProviderTimeout(
                message, details={"backend": self.name, "base_url": self._base_url}
            ) from exc
        except httpx.HTTPError as exc:
            message = f"LoadCoach at {self._base_url} did not answer: {exc}"
            raise BackendUnavailable(
                message, details={"backend": self.name, "base_url": self._base_url}
            ) from exc

    def _events_of(
        self,
        response: httpx.Response,
        *,
        request: StageRequest,
        degradations: tuple[str, ...],
    ) -> Iterator[StageEvent]:
        """Translate one SSE body into port events. The terminal frame is `result` or `error`."""
        event_name = ""
        for raw in response.iter_lines():
            line = raw.rstrip("\r")
            if not line:
                event_name = ""
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if event_name == "token":
                yield StageEvent(kind="token", text=str(data.get("delta", "")))
            elif event_name == "result":
                yield StageEvent(
                    kind="completed",
                    result=self._to_result(
                        _unwrapped(data), request=request, degradations=degradations
                    ),
                )
                return
            elif event_name == "error":
                error = _unwrapped(_unwrapped(data), key="error")
                yield StageEvent(
                    kind="failed",
                    error_code=str(error.get("code", "BACKEND_UNAVAILABLE")),
                    error_message=str(error.get("message", "LoadCoach reported a failure.")),
                )
                return

    def resident_models(self) -> Sequence[str]:
        """Empty, always: residency is LoadCoach's, and it is not IdeaPress's to observe here.

        An empty sequence means "cannot observe", not "nothing is resident", and
        :meth:`capabilities` says so with ``residency_control=False``. LoadCoach's own admission
        control is the reference implementation of the one-model-at-a-time policy (ADR-0038 §1);
        IdeaPress reaching in from outside would be a second, blind controller of the same card.
        """
        return ()

    def unload(self, model_reference: str) -> bool:
        """Refuse honestly: LoadCoach owns residency and exposes no unload to callers.

        Returns:
            ``False``, always. The gateway never calls this — ``routes_internally`` short-circuits
            the switch entirely (ADR-0040) — and returning ``True`` would put an eviction that
            never happened into a provenance record.
        """
        logger.debug("backend.unload_unsupported", extra={"model_canonical_id": model_reference})
        return False

    def post_feedback(
        self,
        job_id: str,
        *,
        accepted: bool,
        validation_passed: bool | None = None,
        quality_score: float | None = None,
        edited: bool = False,
        notes: str | None = None,
        request_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Post caller feedback for one job (LoadCoach api.md §6).

        Args:
            job_id: The job the feedback is about.
            accepted: Whether IdeaPress committed the unit this job contributed to.
            validation_passed: Whether IdeaPress's deterministic validation passed.
            quality_score: A 0–1 score, when there is a meaningful one.
            edited: Whether the text was revised before being committed.
            notes: A short free-text note.
            request_id: Correlation id to propagate.

        Returns:
            The stored feedback record.

        Raises:
            BackendUnavailable: LoadCoach did not answer.
            ContentRejected: LoadCoach refused the feedback.

        Idempotent on LoadCoach's side per ``(job_id, source)``, and `source` is taken from the
        `X-Client-Name` header rather than the body, so one caller cannot overwrite another's.
        IdeaPress does not rely on that alone — :mod:`ideapress.services.feedback` records what it
        has already sent — but it means a retry is safe rather than duplicating.
        """
        body: dict[str, Any] = {"source": "ideapress", "accepted": accepted, "edited": edited}
        if validation_passed is not None:
            body["validation"] = {"passed": validation_passed, "detail": None}
        if quality_score is not None:
            body["quality_score"] = max(0.0, min(1.0, quality_score))
        if notes:
            body["notes"] = notes[:4000]
        return self._call("POST", f"/jobs/{job_id}/feedback", json_body=body, request_id=request_id)

    def reliability(self, *, task: str | None = None) -> Mapping[str, Any]:
        """Read LoadCoach's production evidence, for the I7 demonstration.

        Args:
            task: Restrict to one task profile.

        Returns:
            ``GET /reliability``'s body — per (model, task profile) window statistics.

        Raises:
            BackendUnavailable: LoadCoach did not answer.

        Not used to make any decision. Reliability-informed stage hints are deferred to post-1.0 by
        the development plan, and this exists so the demonstration can show feedback arriving where
        it was meant to arrive.
        """
        path = f"/reliability?task={task}" if task else "/reliability"
        return self._call("GET", path)


def _identity_of(model_body: Mapping[str, Any]) -> ModelIdentity | None:
    """Parse ``provider/name@sha256:digest`` into the port's identity, or ``None``.

    ``None`` is an honest "LoadCoach did not disclose which model answered" — never a guess, which
    would put the wrong model into a provenance record a person is meant to trust.
    """
    canonical = str(model_body.get("canonical_id") or "")
    if not canonical:
        return None
    reference, _, digest = canonical.partition("@")
    kind, _, name = reference.partition("/")
    if not name:
        kind, name = "loadcoach", reference
    return ModelIdentity(
        provider_kind=kind, provider_model_name=name, artifact_digest=digest or None
    )


def _reported_degradations(payload: Mapping[str, Any]) -> list[str]:
    """LoadCoach's own degradations, carried through verbatim rather than re-worded."""
    reported = payload.get("degradations")
    if not isinstance(reported, (list, tuple)):
        return []
    out: list[str] = []
    for entry in reported:
        if isinstance(entry, dict):
            code = entry.get("code", entry.get("kind", "degraded"))
            detail = entry.get("detail", entry.get("message", ""))
            out.append(f"{code}: {detail}" if detail else str(code))
        else:
            out.append(str(entry))
    return out


def _routing_degradations(routing: Mapping[str, Any] | None) -> list[str]:
    """Turn the routing flags that mean something to IdeaPress into recorded degradations.

    ``assumed_context`` is the one workflows §6.2 names: the served context could not be
    established and was taken from the model's advertised maximum, so a context-overflow failure
    later is a consequence rather than a surprise. ``low_evidence`` is surfaced too, because a user
    wondering why a model was chosen is entitled to know the decision rested on little.
    """
    if not routing:
        return []
    flags = routing.get("flags")
    if not isinstance(flags, (list, tuple)):
        return []
    known = {
        "assumed_context": (
            "assumed_context: LoadCoach could not establish the served context and used the "
            "model's advertised maximum; a later context overflow is a consequence of this"
        ),
        "low_evidence": (
            "low_evidence: LoadCoach chose this model with little measured capability evidence"
        ),
    }
    return [known[str(flag)] for flag in flags if str(flag) in known]


def assert_task_map_is_total() -> None:
    """Refuse a task map that has drifted from the stage list.

    Raises:
        ConfigurationError: The map is not exactly the model-using stages of workflows §2 — a
            stage with no profile would fail mid-project, and a profile for a stage that no longer
            exists is dead configuration that reads as coverage.

    Called by the backend's own tests and by ``ideapress doctor``, so the two cannot disagree.
    """
    from baseaicore import ConfigurationError

    mapped = set(LOADCOACH_TASK_MAP)
    missing = sorted(MODEL_STAGES - mapped)
    extra = sorted(mapped - MODEL_STAGES)
    if missing or extra:
        message = (
            "LOADCOACH_TASK_MAP does not match the model-using stages in workflows §2: "
            f"missing {missing or 'none'}, unknown {extra or 'none'}."
        )
        raise ConfigurationError(message, details={"missing": missing, "unknown": extra})


def _unwrapped(frame: Mapping[str, Any], *, key: str = "payload") -> dict[str, Any]:
    """Return ``frame[key]`` when it is an object, else ``frame`` itself.

    Every SSE frame but ``token`` carries the SetSpec event envelope, so the response object sits
    under ``payload`` (ADR-0025 §3). A frame that is already bare — an error body, a mock that does
    not envelope — is returned unchanged rather than being read as empty, because an adapter that
    silently produced a blank result from a well-formed frame would be indistinguishable from a
    model that said nothing.
    """
    inner = frame.get(key)
    return dict(inner) if isinstance(inner, dict) else dict(frame)
