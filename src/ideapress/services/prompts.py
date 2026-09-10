"""ideapress.services.prompts — IdeaPress's prompt pack, on `setspec.prompts`, and its overrides.

ADR-0012: prompts are versioned JSON records, never Python string literals. ADR-0028: the loader,
the renderer and the hashing come from the package; IdeaPress supplies only its own pack. This
module is the one-function shim that adoption asks for — it names where the pack lives so the call
sites do not each have to know.

Two tests hold the rule: one greps the source for inline prompt strings, and one rebuilds the
manifest and asserts nothing drifted. A prompt edited without regenerating the manifest fails at
load rather than silently changing what a model was asked.

**Overrides** (prompt standards §6, row W9). A record at
``$XDG_CONFIG_HOME/ideapress/prompts/<prompt_id>.json`` replaces the shipped record of the same
``prompt_id`` when the pack is loaded, and every attempt that rendered it is marked
``prompt_source: user_override`` (:func:`prompt_source`, written by
``services/stages.record_attempt``) — its output is not comparable with the shipped prompt's. The
pack is read once per process, so an override written or removed takes effect when IdeaPress next
starts. A malformed override is a startup failure, exactly as a malformed shipped record is: the
operator asked for it to be used. :func:`shipped_library` is the pack as installed, before any
override — what ``prompts list|show --shipped`` print and what WeightRoomGym's editor diffs against.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mirrorwall import ComponentHealth, ComponentStatus
from setspec.prompts import PromptNotFound, PromptPackInvalid, load_pack

from ideapress.config import prompt_override_dir

if TYPE_CHECKING:
    from collections.abc import Mapping

    from setspec.prompts import PromptLibrary, RenderedPrompt

__all__ = [
    "PACK_ROOT",
    "library",
    "prompt_source",
    "prompts_health_component",
    "render",
    "shipped_library",
]

PACK_ROOT: Final = Path(__file__).resolve().parent.parent / "prompts"
"""Where IdeaPress's pack lives. Package data, present in the built wheel."""


@lru_cache(maxsize=1)
def library() -> PromptLibrary:
    """Return the loaded prompt pack, the operator's overrides applied, reading it once per process.

    Returns:
        The library. Loading validates every shipped record against the manifest's hashes, and
        every override against the record schema.

    Raises:
        PromptPackInvalid: A record is malformed, the manifest does not describe the pack — a
            prompt edited without regenerating the manifest fails here rather than silently
            changing what a model was asked — or an override is not a valid prompt record.
    """
    return load_pack(PACK_ROOT, override_root=prompt_override_dir())


@lru_cache(maxsize=1)
def shipped_library() -> PromptLibrary:
    """Return the pack as installed, before any override, reading it once per process.

    Raises:
        PromptPackInvalid: As :func:`library`, for the shipped records alone.
    """
    return load_pack(PACK_ROOT)


def render(
    prompt_id: str, variables: Mapping[str, Any], *, version: str | None = None
) -> RenderedPrompt:
    """Render one prompt record.

    Args:
        prompt_id: The record's identifier, e.g. ``stages.hello``.
        variables: Every variable the record declares required.
        version: Pin a version; ``None`` takes the latest in the pack.

    Returns:
        The rendered prompt, carrying the ``prompt_id``, ``version`` and ``sha256`` that get
        recorded on the attempt that used it — which is what makes provenance checkable rather
        than asserted.

    Raises:
        PromptNotFound: No record with that identifier.
        PromptVariableError: A required variable was not supplied.
    """
    return library().render(prompt_id, variables, version=version)


def prompt_source(prompt_id: str | None, version: str | None) -> str | None:
    """Where the record an attempt rendered came from (prompt standards §6).

    Args:
        prompt_id: The attempt's prompt, or ``None`` for a deterministic step.
        version: Its version.

    Returns:
        ``pack`` or ``user_override`` — the loaded record's own ``source`` — or ``None`` when the
        attempt rendered no prompt, or one this process's pack does not hold (a record from outside
        the pack). Never a guess.
    """
    if prompt_id is None:
        return None
    try:
        return library().get(prompt_id, version=version).source
    except (PromptNotFound, PromptPackInvalid):
        return None


def prompts_health_component() -> ComponentHealth:
    """Report the ``prompts`` health component (spec §17).

    Returns:
        ``ok`` when the pack loads and its manifest matches; ``unavailable`` when it does not,
        because a workflow cannot run a prompt it cannot hash. ``data.overridden`` names every
        prompt an override replaces.
    """
    try:
        pack = library()
    except PromptPackInvalid as exc:
        return ComponentHealth(name="prompts", status=ComponentStatus.UNAVAILABLE, detail=str(exc))
    identifiers = list(pack.ids())
    overridden = list(pack.overridden_ids)
    detail = f"{pack.pack_id} {pack.pack_version}, {len(identifiers)} prompt(s)."
    if overridden:
        detail += f" Overridden: {', '.join(overridden)}."
    return ComponentHealth(
        name="prompts",
        status=ComponentStatus.OK,
        detail=detail,
        data={
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "count": len(identifiers),
            "overridden": overridden,
        },
    )
