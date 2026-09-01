"""ideapress.config — typed settings, source-tracked, per Configuration Standards.

Precedence, lowest to highest: built-in defaults, ``config.toml``, ``IDEAPRESS_``-prefixed
environment variables, then explicit overrides (the CLI's highest layer). Overriding is per leaf
field, not per section (configuration standards §1): setting one field of ``[server]`` never
discards its siblings.

The merge is performed here rather than by ``pydantic-settings``'s source machinery for the same
reason both siblings do it: ``ideapress config show`` has to report *which* layer produced every
leaf value, which is easiest to get right by building the merged dict and tracking provenance
alongside it, then handing the result to pydantic once for validation.

Spec §5 says **nothing** is required at startup. Every refusal in this module is therefore about a
configuration that would be unsafe or incoherent — never about a backend being unreachable, which
is a runtime condition the application is designed to survive (spec §20 AC7).
"""

from __future__ import annotations

import difflib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

from baseaicore import ConfigurationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from ideapress.domain.stages import MODEL_STAGES, STAGES

__all__ = [
    "ENV_PREFIX",
    "EXAMPLE_CONFIG_TOML",
    "LOOPBACK_HOSTS",
    "ConfigurationError",
    "ExecutionSettings",
    "InferenceSettings",
    "InsecureBindingError",
    "LoadedSettings",
    "LoggingSettings",
    "ModelsSettings",
    "OpenAICompatibleSettings",
    "ProvidersSettings",
    "ServerSettings",
    "Settings",
    "StageBindings",
    "StorageSettings",
    "WorkflowSettings",
    "config_dir",
    "data_dir",
    "load_settings",
    "resolve_config_path",
    "state_dir",
]

ENV_PREFIX: Final = "IDEAPRESS_"
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_ALL_INTERFACES_HOST = "0.0.0.0"  # noqa: S104 — compared against, never bound to, by this module
_RESERVED_ENV_SUFFIXES: Final[frozenset[str]] = frozenset({"CONFIG", "DATA_DIR", "LOG_LEVEL"})
_DEFAULT_PORT: Final = 8767

InferenceMode = Literal["ollama", "loadcoach", "openai_compatible"]


class InsecureBindingError(ConfigurationError):
    """A configured bind combination would expose the user's private work unsafely.

    Raised by :func:`load_settings` before anything opens a socket (configuration standards §4).
    IdeaPress holds drafts, briefs and source material, so the refusals are the same as the two
    siblings' and lift only by a deliberate acknowledgement, never by accident.
    """

    code: ClassVar[str] = "INSECURE_BINDING"


def _split_csv(value: Any) -> Any:
    """Accept a comma-separated string for a tuple field, as environment variables must (§3)."""
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return value


class ServerSettings(BaseModel):
    """Bind address and HTTP-level limits."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        default="127.0.0.1",
        description=(
            "Interface to bind. Loopback by default; anything else requires allowed_hosts "
            "(ADR-0026)."
        ),
        examples=["127.0.0.1"],
    )
    port: int = Field(default=_DEFAULT_PORT, ge=1, le=65535, examples=[_DEFAULT_PORT])
    allow_lan_exposure: bool = Field(
        default=False,
        description=(
            "Acknowledges a deliberate bind to every interface (0.0.0.0). Without it such a bind "
            "refuses to start."
        ),
    )
    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description=(
            "Host header values accepted on a non-loopback bind, against DNS rebinding. "
            "Comma-separated in the environment."
        ),
        examples=[["ideapress.local"]],
    )
    max_body_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        description="Largest request body accepted, before it is buffered. Briefs can be long.",
    )

    _split_hosts = field_validator("allowed_hosts", mode="before")(_split_csv)


class StorageSettings(BaseModel):
    """Where the database and the project artifact directory live."""

    model_config = ConfigDict(extra="forbid")

    database_url: str | None = Field(
        default=None,
        description="SQLAlchemy URL. Defaults to a SQLite file under the XDG data directory.",
    )
    auto_migrate: bool = Field(
        default=True,
        description=(
            "Run pending migrations at startup. Defaults true on SQLite; a PostgreSQL URL turns "
            "it off, because a failed migration there cannot be rolled back automatically "
            "(database standards §5.1)."
        ),
    )
    project_dir: str | None = Field(
        default=None,
        description="Directory holding per-project artifacts and exports. Defaults under XDG data.",
    )
    statement_timeout_ms: int = Field(default=30_000, ge=0)

    @model_validator(mode="after")
    def _apply_data_dir_defaults(self) -> StorageSettings:
        """Fill the two path defaults from the XDG data directory, and relax auto_migrate on PG."""
        if self.database_url is None:
            self.database_url = f"sqlite:///{data_dir() / 'ideapress.sqlite3'}"
        if self.project_dir is None:
            self.project_dir = str(data_dir() / "projects")
        if "auto_migrate" not in self.model_fields_set and not self.database_url.startswith(
            "sqlite"
        ):
            self.auto_migrate = False
        return self


class OllamaSettings(BaseModel):
    """Direct Ollama, the default backend."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="http://127.0.0.1:11434")
    timeout_seconds: int = Field(default=300, ge=1)


class LoadCoachSettings(BaseModel):
    """The optional LoadCoach backend: queueing, routing by task profile, and feedback."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="http://127.0.0.1:8766")
    api_key_env: str = Field(
        default="",
        description="Name of the environment variable holding the token. Never the token itself.",
    )
    timeout_seconds: int = Field(default=600, ge=1)
    honour_stage_bindings: bool = Field(
        default=False,
        description=(
            "Send the stage's `[models.stages]` binding to LoadCoach as a model override. Off by "
            "default: LoadCoach chooses the model, which is what it is for. Turning it on pins "
            "the model and gives up routing, evidence and reliability for that stage (ADR-0040)."
        ),
    )
    job_stages: tuple[str, ...] = Field(
        default=("draft", "revise", "repair", "project_review"),
        description=(
            "Stages submitted through the asynchronous `/jobs` queue rather than synchronous "
            "`/generate`. The long ones; everything else is interactive and submitted with "
            '`class = "interactive"` so a person is never queued behind background work.'
        ),
    )

    @field_validator("job_stages")
    @classmethod
    def _known_model_stages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse a name that is not a model-using stage.

        Args:
            value: The configured stage identifiers.

        Returns:
            ``value`` when every entry is a model-using stage in workflows §2.

        Raises:
            ValueError: An entry names no model-using stage. Naming a gate stage, or misspelling
                one, would otherwise route nothing through the queue and say nothing about it —
                the same silent-no-op the `[models.stages]` startup check exists to prevent.
        """
        unknown = sorted(set(value) - MODEL_STAGES)
        if unknown:
            named = ", ".join(unknown)
            choices = ", ".join(sorted(MODEL_STAGES))
            message = (
                f"inference.loadcoach.job_stages names {named}, which is not a model-using "
                f"stage. Choose from: {choices}."
            )
            raise ValueError(message)
        return value


class OpenAICompatibleSettings(BaseModel):
    """Any OpenAI-compatible endpoint. Empty base_url means "not configured", not "localhost"."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="")
    api_key_env: str = Field(default="")
    timeout_seconds: int = Field(default=300, ge=1)
    model: str = Field(
        default="",
        description=(
            "The model name this endpoint serves. OpenAI-compatible servers expose one namespace "
            "with no provider prefix, so the `[models.stages]` bindings do not apply to it."
        ),
    )


class InferenceSettings(BaseModel):
    """Which backend runs stages, and what happens when it is not there."""

    model_config = ConfigDict(extra="forbid")

    mode: InferenceMode = Field(default="ollama")
    fallback_mode: str = Field(
        default="", description="Optional; empty means no fallback. Ignored when pin_backend."
    )
    pin_backend: bool = Field(
        default=False, description="True = never fall back; fail the stage instead."
    )
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    loadcoach: LoadCoachSettings = Field(default_factory=LoadCoachSettings)
    openai_compatible: OpenAICompatibleSettings = Field(default_factory=OpenAICompatibleSettings)

    @field_validator("fallback_mode")
    @classmethod
    def _known_fallback(cls, value: str) -> str:
        """Refuse a fallback naming a mode that does not exist — a silent no-fallback otherwise."""
        if value and value not in {"ollama", "loadcoach", "openai_compatible"}:
            message = (
                f"inference.fallback_mode is {value!r}, which is not an inference mode. "
                "Use 'ollama', 'loadcoach', 'openai_compatible', or '' for no fallback."
            )
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _fallback_is_not_self(self) -> InferenceSettings:
        """A backend cannot fall back to itself; that reads as configured resilience and is none."""
        if self.fallback_mode and self.fallback_mode == self.mode:
            message = (
                f"inference.fallback_mode is {self.fallback_mode!r}, the same as inference.mode. "
                "A backend cannot fall back to itself; leave it empty for no fallback."
            )
            raise ValueError(message)
        return self


class StageBindings(BaseModel):
    """Stage -> model bindings for standalone mode (`[models.stages]`, spec §12).

    Extra keys are forbidden and the model validator checks the key set against workflows §2, so a
    binding for a stage that does not exist — or a model-using stage with no binding — fails
    startup validation naming the stage. That check is the reason this class is not a bare dict.
    """

    model_config = ConfigDict(extra="forbid")

    requirements: str = "ollama/qwen3.5:9b-q8_0"
    research_synthesis: str = "ollama/qwen3.5:9b-q8_0"
    outline: str = "ollama/qwen3.5:9b-q8_0"
    draft: str = "ollama/gemma4:12b"
    repair: str = "ollama/qwen3.5:9b-q8_0"
    audit_fast: str = "ollama/qwen3.5:9b-q8_0"
    audit_deep: str = "ollama/qwen3.5:9b-q8_0"
    fact_check: str = "ollama/qwen3.5:9b-q8_0"
    critique: str = "ollama/qwen3.5:9b-q8_0"
    revise: str = "ollama/qwen3.5:9b-q8_0"
    project_review: str = "ollama/qwen3.5:9b-q8_0"


class ModelsSettings(BaseModel):
    """The `[models]` section, whose only member at 1.0 is `[models.stages]`."""

    model_config = ConfigDict(extra="forbid")

    stages: StageBindings = Field(default_factory=StageBindings)


class ExecutionSettings(BaseModel):
    """How many generations may be in flight, and what happens when the model must change.

    One model runs at a time, never two (ADR-0038). The card this suite targets holds one of the
    two default models with room for its context and not both, and Ollama will hold a second
    resident until ``keep_alive`` expires — silently, with no error IdeaPress could raise. These
    two keys are the policy; :mod:`ideapress.services.inference` is the one choke point that
    applies it.
    """

    model_config = ConfigDict(extra="forbid")

    max_concurrent_stages: int = Field(
        default=1,
        ge=1,
        description=(
            "Generations in flight at once. Only 1 is accepted: a higher value is refused at "
            "startup rather than silently honoured (ADR-0038)."
        ),
    )
    unload_before_model_switch: bool = Field(
        default=True,
        description=(
            "Unload the resident model before loading a different one. Turning this off lets two "
            "models contend for one GPU, which degrades to CPU or OOM without an error."
        ),
    )

    @field_validator("max_concurrent_stages")
    @classmethod
    def _refuse_concurrency(cls, value: int) -> int:
        """Refuse any value above 1, naming the reason (ADR-0038 §2).

        Args:
            value: The configured maximum.

        Returns:
            ``value`` when it is 1.

        Raises:
            ValueError: ``value`` is above 1. IdeaPress has no queue and must not grow one, and a
                second concurrent generation means two models resident on a single-GPU machine.
                The refusal is deliberate: silently clamping to 1 would leave a user believing
                they had configured parallelism.
        """
        if value > 1:
            message = (
                f"execution.max_concurrent_stages is {value}; only 1 is supported. IdeaPress runs "
                "one generation at a time so that one model is resident at a time (ADR-0038). "
                "Set it to 1, or use LoadCoach as the backend if you want a queue."
            )
            raise ValueError(message)
        return value


class WorkflowSettings(BaseModel):
    """The bounds every loop in workflows §5 runs under."""

    model_config = ConfigDict(extra="forbid")

    max_revision_rounds: int = Field(default=3, ge=0, le=100)
    diminishing_returns_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    max_attempts_per_stage: int = Field(default=3, ge=1, le=100)
    audit_escalation_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    require_clean_validation_to_commit: bool = Field(default=True)
    context_budget_tokens: int = Field(
        default=8_000,
        ge=256,
        description=(
            "Token budget for assembled context (workflows §7). Requirements and the unit "
            "specification are never dropped to fit it; overflow fails with both numbers."
        ),
    )
    allow_audit_gated_requirements: bool = Field(
        default=True,
        description=(
            "Whether an audit's explicit per-requirement attestation may satisfy a blocking "
            "requirement that has no deterministic check (ADR-0039). Silence never satisfies "
            "one either way. False forces a wholly mechanical gate: such a requirement pauses "
            "its unit until it gets a deterministic check or is demoted to advisory."
        ),
    )
    structured_output_tokens: int = Field(
        default=8_192,
        ge=1_024,
        le=131_072,
        description=(
            "Output-token budget for the structured stages (requirements, outline, audit_fast, "
            "audit_deep, critique, project_review), and — when raised above the 8192 default — "
            "the thinking floor for the text-writing stages (draft, repair, revise) as well. "
            "Includes the model's reasoning: a thinking model spends output tokens before its "
            "first word of answer, and 8192 is the measured floor for the default models "
            "(spec §15). Raise this when a unit pauses with an exhausted output budget."
        ),
    )


class ProvidersSettings(BaseModel):
    """Egress policy. Remote inference is opt-in, per stage, and labelled in the UI."""

    model_config = ConfigDict(extra="forbid")

    allow_remote: bool = Field(default=False)


class LoggingSettings(BaseModel):
    """Structured logging. Project content is never logged at INFO or above (spec §14)."""

    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    include_content: bool = Field(
        default=False,
        description=(
            "Store and log prompt and response text. Off by default: this is the user's private "
            "work, and hashes are enough for provenance."
        ),
    )
    format: Literal["text", "json"] = "text"


class Settings(BaseModel):
    """The complete, validated IdeaPress configuration.

    Constructed only by :func:`load_settings`, which resolves the precedence chain first — never
    call ``Settings(**raw)`` on unmerged input, or the file/env/CLI layering is bypassed.
    """

    model_config = ConfigDict(extra="forbid")

    server: ServerSettings = Field(default_factory=ServerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    inference: InferenceSettings = Field(default_factory=InferenceSettings)
    models: ModelsSettings = Field(default_factory=ModelsSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    """The result of resolving configuration: the settings, and where every value came from."""

    settings: Settings
    config_path: Path
    config_file_used: bool
    sources: dict[str, str]


def config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/ideapress``, falling back to ``~/.config/ideapress``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "ideapress"


def data_dir() -> Path:
    """Return ``$IDEAPRESS_DATA_DIR``, else ``$XDG_DATA_HOME/ideapress``, else the XDG default."""
    override = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "ideapress"


def state_dir() -> Path:
    """Return ``$XDG_STATE_HOME/ideapress``, falling back to ``~/.local/state/ideapress``."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "ideapress"


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the configuration file location per Configuration Standards §2.

    Args:
        explicit: A path from ``--config``, if the caller was given one.

    Returns:
        The path to read. Order: the explicit path, then ``IDEAPRESS_CONFIG``, then a
        project-local ``./ideapress.toml`` if one exists, then the XDG default. A missing file at
        the resolved path is not an error — :func:`load_settings` falls back to defaults, which is
        what makes "starts with zero configuration" true (spec §20 AC1).
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    local = Path.cwd() / "ideapress.toml"
    if local.is_file():
        return local
    return config_dir() / "config.toml"


def _read_env(prefix: str) -> dict[str, Any]:
    """Parse ``<prefix>SECTION__FIELD`` environment variables into a nested dict."""
    nested: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if suffix in _RESERVED_ENV_SUFFIXES:
            continue
        path = suffix.lower().split("__")
        node = nested
        for part in path[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):  # a leaf and a section cannot share a name
                break
            node = child
        else:
            node[path[-1]] = value

    log_level = os.environ.get(f"{prefix}LOG_LEVEL")
    if log_level and "level" not in nested.get("logging", {}):
        nested.setdefault("logging", {})["level"] = log_level
    return nested


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``, recursively, per leaf field rather than per section."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _known_dotted_keys() -> list[str]:
    """Every ``section`` and ``section.field`` name Settings recognizes, for typo suggestions."""
    known: list[str] = []
    for section_name, section_field in Settings.model_fields.items():
        known.append(section_name)
        section_model = section_field.annotation
        if isinstance(section_model, type) and issubclass(section_model, BaseModel):
            known.extend(f"{section_name}.{name}" for name in section_model.model_fields)
    return known


def _translate_validation_error(
    exc: PydanticValidationError, config_path: Path
) -> ConfigurationError:
    """Turn a pydantic ``ValidationError`` into a :class:`ConfigurationError` naming the field."""
    known_keys = _known_dotted_keys()
    problems: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            suggestion = difflib.get_close_matches(loc, known_keys, n=1)
            hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
            problems.append(f"unknown configuration key '{loc}'{hint}")
        else:
            problems.append(f"{loc}: {error['msg']}")
    message = f"Configuration invalid ({config_path}): " + "; ".join(problems)
    return ConfigurationError(message, details={"file": str(config_path), "problems": problems})


def check_stage_vocabulary(bindings: StageBindings) -> None:
    """Assert that `[models.stages]`, ``StageId`` and workflows §2 are one set.

    Args:
        bindings: The resolved `[models.stages]` section.

    Raises:
        ConfigurationError: A binding names a stage that does not exist in workflows §2, or a
            model-using stage has no binding. Both name the stage. `fact_check` was bound in
            configuration and mapped to a LoadCoach task profile while appearing in no stage list,
            and nothing caught it until three documents were read side by side; this is that audit,
            run every time the application starts.
    """
    bound = set(type(bindings).model_fields)
    unknown = sorted(bound - set(STAGES))
    if unknown:
        message = (
            f"[models.stages] binds {', '.join(unknown)}, which is not a stage in workflows §2. "
            f"The stages are: {', '.join(sorted(STAGES))}."
        )
        raise ConfigurationError(message, details={"unknown_stages": unknown})
    missing = sorted(MODEL_STAGES - bound)
    if missing:
        message = (
            f"[models.stages] has no binding for {', '.join(missing)}, which workflows §2 lists as "
            "a model-using stage. Every model-using stage needs a model."
        )
        raise ConfigurationError(message, details={"unbound_stages": missing})


def _validate_security(settings: Settings) -> None:
    """Refuse the bind combinations that would expose the user's private work (ADR-0026)."""
    server = settings.server
    if server.host == _ALL_INTERFACES_HOST and not server.allow_lan_exposure:
        raise InsecureBindingError(
            "server.host is '0.0.0.0' (all interfaces) but server.allow_lan_exposure is false. "
            "This application holds your drafts and source material; exposing it beyond this "
            "machine must be a deliberate act. Set server.allow_lan_exposure = true if intended.",
            details={"field": "server.allow_lan_exposure", "host": server.host},
        )
    if server.host not in LOOPBACK_HOSTS and not server.allowed_hosts:
        raise InsecureBindingError(
            "server.host is not loopback but server.allowed_hosts is empty. A non-loopback bind "
            "must name every hostname it will accept, or DNS rebinding can reach it from any page "
            "the user visits (ADR-0026).",
            details={"field": "server.allowed_hosts", "host": server.host},
        )


def _validate_egress(settings: Settings) -> None:
    """Refuse a remote backend that `providers.allow_remote = false` forbids (risk S4)."""
    inference = settings.inference
    modes = {inference.mode, inference.fallback_mode} - {""}
    if "openai_compatible" not in modes:
        return
    base_url = inference.openai_compatible.base_url
    if not base_url:
        message = (
            "inference mode 'openai_compatible' is selected but "
            "inference.openai_compatible.base_url is empty. Name the endpoint that will receive "
            "your content."
        )
        raise ConfigurationError(message, details={"field": "inference.openai_compatible.base_url"})
    if settings.providers.allow_remote:
        return
    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host not in LOOPBACK_HOSTS:
        raise InsecureBindingError(
            f"inference.openai_compatible.base_url points at {host!r}, which is not this machine, "
            "but providers.allow_remote is false. Your briefs and drafts would leave the machine. "
            "Set providers.allow_remote = true to accept that.",
            details={"field": "providers.allow_remote", "host": host},
        )


def _track_sources(
    file_data: dict[str, Any], env_data: dict[str, Any], cli_data: dict[str, Any]
) -> dict[str, str]:
    """Report, for every leaf field, which layer produced its effective value."""
    sources: dict[str, str] = {}
    for section_name, section_field in Settings.model_fields.items():
        section_model = section_field.annotation
        if not (isinstance(section_model, type) and issubclass(section_model, BaseModel)):
            continue
        for field_name in section_model.model_fields:
            path = f"{section_name}.{field_name}"
            if field_name in cli_data.get(section_name, {}):
                sources[path] = "cli"
            elif field_name in env_data.get(section_name, {}):
                sources[path] = f"env {ENV_PREFIX}{section_name.upper()}__{field_name.upper()}"
            elif field_name in file_data.get(section_name, {}):
                sources[path] = "file"
            else:
                sources[path] = "default"
    return sources


def load_settings(
    *,
    config_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> LoadedSettings:
    """Resolve configuration through the full precedence chain and validate it.

    Args:
        config_path: An explicit ``--config`` path. See :func:`resolve_config_path` for the
            fallback order when this is ``None``.
        cli_overrides: Explicit values from CLI flags, nested as the TOML file is
            (``{"server": {"port": 9000}}``). The highest-precedence layer.

    Returns:
        The validated :class:`LoadedSettings`, with a ``sources`` map naming the layer behind
        every leaf value.

    Raises:
        ConfigurationError: The file is not valid TOML, a key is unrecognized, a value fails a
            field's type or range, `[models.stages]` disagrees with workflows §2, or a bind or
            egress combination is unsafe (:class:`InsecureBindingError`, a subclass). Does **not**
            raise because a backend is unreachable: spec §20 AC7 requires that never to be a
            startup failure.
    """
    resolved_path = resolve_config_path(config_path)
    file_data: dict[str, Any] = {}
    file_used = False
    if resolved_path.is_file():
        try:
            with resolved_path.open("rb") as handle:
                file_data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"Configuration file {resolved_path} is not valid TOML: {exc}",
                details={"file": str(resolved_path)},
            ) from exc
        file_used = True

    env_data = _read_env(ENV_PREFIX)
    cli_data = cli_overrides or {}
    merged = _deep_merge(_deep_merge(file_data, env_data), cli_data)

    try:
        settings = Settings.model_validate(merged)
    except PydanticValidationError as exc:
        raise _translate_validation_error(exc, resolved_path) from exc

    check_stage_vocabulary(settings.models.stages)
    _validate_security(settings)
    _validate_egress(settings)

    sources = _track_sources(file_data, env_data, cli_data)
    return LoadedSettings(
        settings=settings, config_path=resolved_path, config_file_used=file_used, sources=sources
    )


EXAMPLE_CONFIG_TOML: Final = """\
# IdeaPress configuration.
# Every key below is optional; a fresh install with no file at all is fully functional and starts
# with no backend reachable.
# Precedence: defaults -> this file -> IDEAPRESS_* environment variables -> CLI flags.

[server]
host = "127.0.0.1"
port = 8767
allow_lan_exposure = false
allowed_hosts = []          # required when host is not loopback (ADR-0026)

[storage]
# database_url and project_dir default under the XDG data directory.
auto_migrate = true

[inference]
mode = "ollama"             # ollama | loadcoach | openai_compatible
fallback_mode = ""          # optional; empty means no fallback
pin_backend = false         # true = never fall back, fail the stage instead

[inference.ollama]
base_url = "http://127.0.0.1:11434"
timeout_seconds = 300

[execution]
# One generation in flight, and one model resident, at a time (ADR-0038). A value above 1 is
# refused at startup: IdeaPress has no queue and two models do not fit on a single-GPU machine.
max_concurrent_stages = 1
unload_before_model_switch = true

# Standalone stage -> model bindings. One key per model-using stage in workflows §2, spelled
# exactly as the stage is; a binding for a stage that does not exist, or a model-using stage with
# no binding, fails startup validation naming the stage.
[models.stages]
requirements       = "ollama/qwen3.5:9b-q8_0"
research_synthesis = "ollama/qwen3.5:9b-q8_0"
outline            = "ollama/qwen3.5:9b-q8_0"
draft              = "ollama/gemma4:12b"
repair             = "ollama/qwen3.5:9b-q8_0"
audit_fast         = "ollama/qwen3.5:9b-q8_0"
audit_deep         = "ollama/qwen3.5:9b-q8_0"
fact_check         = "ollama/qwen3.5:9b-q8_0"
critique           = "ollama/qwen3.5:9b-q8_0"
revise             = "ollama/qwen3.5:9b-q8_0"
project_review     = "ollama/qwen3.5:9b-q8_0"

[workflow]
max_revision_rounds = 3
diminishing_returns_threshold = 0.05
max_attempts_per_stage = 3
audit_escalation_threshold = 0.6
require_clean_validation_to_commit = true
# Whether an audit's explicit attestation may satisfy a blocking requirement that has no
# deterministic check (ADR-0039). Silence never satisfies one either way; false forces a wholly
# mechanical gate, pausing such units instead.
allow_audit_gated_requirements = true
# Output-token budget for the structured stages (requirements, outline, audits, critique,
# project review); raised above 8192 it also lifts the thinking floor of the text-writing
# stages (draft, repair, revise). A reasoning model spends output tokens thinking before its
# first word of answer; a unit paused for an exhausted output budget needs this raised.
# Accepted range: 1024-131072.
structured_output_tokens = 8192

[providers]
allow_remote = false        # a remote backend sends your drafts off this machine

[logging]
level = "INFO"
include_content = false     # your prompts and drafts are never logged by default
"""
