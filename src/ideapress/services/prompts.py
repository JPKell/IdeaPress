"""ideapress.services.prompts — IdeaPress's prompt pack, on `setspec.prompts`.

ADR-0012: prompts are versioned JSON records, never Python string literals. ADR-0028: the loader,
the renderer and the hashing come from the package; IdeaPress supplies only its own pack. This
module is the one-function shim that adoption asks for — it names where the pack lives so the call
sites do not each have to know.

Two tests hold the rule: one greps the source for inline prompt strings, and one rebuilds the
manifest and asserts nothing drifted. A prompt edited without regenerating the manifest fails at
load rather than silently changing what a model was asked.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mirrorwall import ComponentHealth, ComponentStatus
from setspec.prompts import PromptPackInvalid, load_pack

if TYPE_CHECKING:
    from collections.abc import Mapping

    from setspec.prompts import PromptLibrary, RenderedPrompt

__all__ = ["PACK_ROOT", "library", "prompts_health_component", "render"]

PACK_ROOT: Final = Path(__file__).resolve().parent.parent / "prompts"
"""Where IdeaPress's pack lives. Package data, present in the built wheel."""


@lru_cache(maxsize=1)
def library() -> PromptLibrary:
    """Return the loaded prompt pack, reading it once per process.

    Returns:
        The library. Loading validates every record against the manifest's hashes.

    Raises:
        PromptPackInvalid: A record is malformed, or the manifest does not describe the pack — a
            prompt edited without regenerating the manifest fails here rather than silently
            changing what a model was asked.
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


def prompts_health_component() -> ComponentHealth:
    """Report the ``prompts`` health component (spec §17).

    Returns:
        ``ok`` when the pack loads and its manifest matches; ``unavailable`` when it does not,
        because a workflow cannot run a prompt it cannot hash.
    """
    try:
        pack = library()
    except PromptPackInvalid as exc:
        return ComponentHealth(name="prompts", status=ComponentStatus.UNAVAILABLE, detail=str(exc))
    identifiers = list(pack.ids())
    return ComponentHealth(
        name="prompts",
        status=ComponentStatus.OK,
        detail=f"{pack.pack_id} {pack.pack_version}, {len(identifiers)} prompt(s).",
        data={
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "count": len(identifiers),
        },
    )
