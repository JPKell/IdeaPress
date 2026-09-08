"""ideapress.services.research_tools — the registry, the containment and the executor.

Everything about *how* a tool call is authorized, contained and refused belongs to ToolYard
(ADR-0053) and is not re-implemented here. What this module owns is the three things that package
deliberately does not: which handlers this application registers, where a project's readable root
is, and how the fetch reaches a resolver without ToolYard opening a socket of its own.

**Containment is `PathContainment`, not `TieredSandbox`** (ADR-0116 decision 2). Neither registered
tool declares `requires_isolation` — `read_file` opens a file and `http_fetch` opens a connection;
neither runs a subprocess — so the executor's containment port has to answer path resolution and
nothing else. A `TieredSandbox` would probe the host by launching a canary and would put a
subprocess launcher inside an application with nothing to run in one. If a tool needing isolation
were ever registered here, `PathContainment` reports `IsolationTier.UNAVAILABLE` and the executor
refuses it with `isolation_unavailable` rather than running it unisolated — ADR-0018's rule, upheld
by construction rather than by a check somebody remembers to write.

**`http_fetch` is registered only when a host is named.** `toolyard.http_fetch_tool([])` falls back
to loopback, which is the right default for a package and the wrong one for the application holding
the user's private drafts: a URL in a brief would reach whatever else is listening on the machine.
So an empty `[research] allowed_hosts` leaves the tool out of the registry entirely, and a URL then
produces a recorded `REFUSED` / `unknown_tool` result. Two independent facts keep an unconfigured
installation off the network — the tool is not registered, and it is not in the allowlist.

**The read root is the project's `sources/` directory, not the project directory.** Exports are
written into the project directory itself, and a research stage that could ingest its own exports
would build notes out of the document it is meant to be researching for. The subdirectory is the
operator's drop box; nothing in IdeaPress writes to it.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from toolyard import (
    EgressClass,
    PathContainment,
    SandboxPaths,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    http_fetch_tool,
    read_file_tool,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx
    from toolyard import Resolver, ToolCallStore

    from ideapress.config import ResearchSettings

__all__ = ["ResearchPlant", "resolve_host"]


def resolve_host(host: str) -> Sequence[str]:
    """Resolve a hostname to its addresses, for `http_fetch`'s ADR-0026 §3 checks.

    ToolYard's `.importlinter` forbids `socket` in every module of that package, forever, so the
    resolver is the application's to supply and has no default there. This is that one line, kept
    to exactly what :data:`toolyard.Resolver` documents so the injected test double and the shipped
    behaviour differ in nothing but the addresses they return.

    Args:
        host: The hostname from the URL the brief named.

    Returns:
        Every address the host resolves to. ToolYard checks each one, so a name resolving to a mix
        of public and loopback addresses is judged on all of them, not on the first.
    """
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


@dataclass(frozen=True, slots=True)
class ResearchPlant:
    """One stage run's registry, containment and executor.

    Built per stage run rather than per process: the read root is one project's, and a stage that
    is never started pays for nothing. That is the one place this differs from PromptCadence's
    process-lifetime `ToolPlant`, whose sandbox probe is expensive and whose workspaces are
    per-trajectory rather than per-application-entity.

    Attributes:
        registry: The tools that could be built. `read_file` always; `http_fetch` only when a host
            is configured.
        allowlist: `[research] allowed_tools`, passed through exactly as configured and never
            narrowed to what was registered. A tool named here and absent from the registry is
            refused `unknown_tool` rather than `not_allowlisted`, so the record distinguishes
            "you did not allow it" from "it could not be built".
        workspace: The project's `sources/` directory. It is the sandbox's *write* root because
            :class:`toolyard.SandboxPaths` requires one and `read_file` reads from the write root
            as well as the read roots — and nothing can write through it, because no mutating tool
            is registered here and none ever will be.
        timeout_seconds: Per call.
    """

    registry: ToolRegistry
    allowlist: frozenset[str]
    workspace: SandboxPaths
    timeout_seconds: float

    @classmethod
    def build(
        cls,
        settings: ResearchSettings,
        *,
        sources_dir: Path,
        resolver: Resolver | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> ResearchPlant:
        """Assemble the plant for one project's stage run.

        Args:
            settings: `[research]`, validated.
            sources_dir: The project's `sources/` directory. Created if missing, so a project that
                has never had one still runs the stage (and finds nothing to read).
            resolver: How `http_fetch` resolves a hostname, or ``None`` for the real one. Injected
                because ADR-0026 §3's literal-IP rule is only testable against a resolution the
                test controls.
            transport: `http_fetch`'s httpx transport, or ``None`` for the real one. Injected so
                the whole fetch path — allowlist, redirects, media types, size caps — is exercised
                without opening a socket, which is what keeps spec §20 #11 true now that a network
                tool ships.

        Returns:
            The plant.

        Raises:
            OSError: The `sources/` directory could not be created. Left to propagate: a stage that
                cannot establish its own containment root must not run with a different one.
        """
        root = sources_dir.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

        registry = ToolRegistry()
        registry.register(*read_file_tool(max_bytes=settings.max_file_bytes))
        if settings.allowed_hosts:
            registry.register(
                *http_fetch_tool(
                    settings.allowed_hosts,
                    resolve=resolver if resolver is not None else resolve_host,
                    max_bytes=settings.max_fetch_bytes,
                    read_timeout_seconds=settings.timeout_seconds,
                    transport=transport,
                )
            )
        return cls(
            registry=registry,
            allowlist=frozenset(settings.allowed_tools),
            workspace=SandboxPaths(write_root=root),
            timeout_seconds=settings.timeout_seconds,
        )

    def executor(self, store: ToolCallStore | None) -> ToolExecutor:
        """Build the executor for one call.

        Args:
            store: Where the record goes — the stage's
                :class:`~ideapress.infrastructure.tool_calls.CollectingToolCallStore`, flushed onto
                the transaction the attempt commits in. ``None`` records nothing and is for a
                caller with no database; the record is built either way, so the path a test
                exercises is the path production runs.

        Returns:
            An executor over this plant's registry and containment, narrowed to the configured
            allowlist.
        """
        return ToolExecutor(
            self.registry,
            PathContainment(),
            allowlist=self.allowlist,
            store=store,
            default_timeout_seconds=self.timeout_seconds,
        )

    def context(self, invocation_id: str, *, egress_approved: bool) -> ToolContext:
        """Build the trusted half of one invocation.

        Args:
            invocation_id: This application's identifier for the call, minted before the call and
                never by a model. It is also what the egress decision rendered *before* the call
                carries as its `source_ref`, which is how the two join (ADR-0073).
            egress_approved: Whether Commissioner approved this call's target. **The only thing
                that raises the ceiling.** `True` passes :attr:`toolyard.EgressClass.NETWORK`,
                which is the *enforcement* of a verdict Commissioner rendered and recorded;
                `False` leaves it closed and ToolYard refuses a `NETWORK`-declaring tool with
                `egress_not_permitted` — a structured result through the ordinary path, never an
                exception (ADR-0054: Commissioner records, the caller enforces).

        Returns:
            The context.
        """
        return ToolContext(
            invocation_id,
            workspace=self.workspace,
            timeout_seconds=self.timeout_seconds,
            max_egress=EgressClass.NETWORK if egress_approved else EgressClass.NONE,
        )
