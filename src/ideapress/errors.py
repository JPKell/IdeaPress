"""ideapress.errors — the error vocabulary from spec §13, and its HTTP mapping.

Every code the application raises is declared here, once, as a :class:`~baseaicore.SuiteError`
subclass. The web layer maps codes to statuses; the CLI maps them to exit codes and messages. A
code that appears in a route but not in this module is a defect the ``test_error_vocabulary``
test catches, because api.md §7 promises the caller a fixed set.
"""

from __future__ import annotations

from typing import ClassVar, Final

from baseaicore import ConfigurationError, NotFoundError, SuiteError, ValidationError

__all__ = [
    "ERROR_CODES",
    "BackendUnavailable",
    "BackendVersionMismatch",
    "ContentRejected",
    "ContextLimitExceeded",
    "ExportFailed",
    "ModelNotConfigured",
    "ProjectNotFound",
    "ProviderTimeout",
    "RequirementsUnmet",
    "RevisionLimitReached",
    "SchemaVersionUnsupported",
    "StageAlreadyRunning",
    "StagePreconditionFailed",
    "UnitNotFound",
    "ValidationFailed",
]


class BackendUnavailable(SuiteError):
    """The configured inference backend could not be reached.

    Never raised at startup: spec §20 AC7 requires an unreachable backend to be a stage-level
    failure, never a startup failure.
    """

    code: ClassVar[str] = "BACKEND_UNAVAILABLE"


class BackendVersionMismatch(SuiteError):
    """The backend's API major version is not one this adapter speaks. Names both versions."""

    code: ClassVar[str] = "BACKEND_VERSION_MISMATCH"


class ModelNotConfigured(SuiteError):
    """A model-using stage has no `[models.stages]` binding. Names the stage and the setting."""

    code: ClassVar[str] = "MODEL_NOT_CONFIGURED"


class ProviderTimeout(SuiteError):
    """The backend accepted the request and did not answer within the configured timeout."""

    code: ClassVar[str] = "PROVIDER_TIMEOUT"


class ContextLimitExceeded(SuiteError):
    """Assembled context does not fit the budget, and nothing droppable remains.

    Carries ``required_tokens`` and ``budget_tokens`` in its details, always: workflows §7 says the
    stage fails "with numbers" rather than silently truncating the contract, and a message without
    both figures is the silent truncation with extra steps (risk T3).
    """

    code: ClassVar[str] = "CONTEXT_LIMIT_EXCEEDED"


class ValidationFailed(ValidationError):
    """One or more deterministic checks failed. Lists every failing check with its class."""

    code: ClassVar[str] = "VALIDATION_FAILED"


class RequirementsUnmet(SuiteError):
    """A blocking requirement is not covered. Lists the requirement keys and why."""

    code: ClassVar[str] = "REQUIREMENTS_UNMET"


class StagePreconditionFailed(SuiteError):
    """The stage cannot run from the project's current state. Names the state and what is needed."""

    code: ClassVar[str] = "STAGE_PRECONDITION_FAILED"


class RevisionLimitReached(SuiteError):
    """Revision stopped at its bound. Reports the rounds used and which stop applied."""

    code: ClassVar[str] = "REVISION_LIMIT_REACHED"


class ContentRejected(SuiteError):
    """The model declined the task.

    A distinct outcome from a failure (spec §13, risk M1): the workflow did not break, and the
    model's stated reason is surfaced verbatim so the user can rephrase or change model.
    """

    code: ClassVar[str] = "CONTENT_REJECTED"


class ProjectNotFound(NotFoundError):
    """No project with that identifier."""

    code: ClassVar[str] = "PROJECT_NOT_FOUND"


class UnitNotFound(NotFoundError):
    """No unit with that identifier in this project."""

    code: ClassVar[str] = "UNIT_NOT_FOUND"


class StageAlreadyRunning(SuiteError):
    """A stage task is already running for this project. Only one runs at a time (api.md §3)."""

    code: ClassVar[str] = "STAGE_ALREADY_RUNNING"


class ExportFailed(SuiteError):
    """An export could not be written or rendered. Names the format and the cause."""

    code: ClassVar[str] = "EXPORT_FAILED"


class SchemaVersionUnsupported(SuiteError):
    """A payload declares a schema version this build does not support."""

    code: ClassVar[str] = "SCHEMA_VERSION_UNSUPPORTED"


ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        BackendUnavailable.code,
        BackendVersionMismatch.code,
        ModelNotConfigured.code,
        ProviderTimeout.code,
        ContextLimitExceeded.code,
        ValidationFailed.code,
        RequirementsUnmet.code,
        StagePreconditionFailed.code,
        RevisionLimitReached.code,
        ContentRejected.code,
        ProjectNotFound.code,
        UnitNotFound.code,
        StageAlreadyRunning.code,
        ExportFailed.code,
        SchemaVersionUnsupported.code,
    }
)
"""Spec §13's fifteen codes. The application-level codes below come from `baseaicore` and are
shared with every other component, so they are not part of IdeaPress's own vocabulary."""

SHARED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {ConfigurationError.code, ValidationError.code, NotFoundError.code, "INTERNAL_ERROR"}
)
