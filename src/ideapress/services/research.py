"""ideapress.services.research — workflows §2's stage 2, built on ToolYard (row M1, ADR-0116).

One pass over one project. Two kinds of target, both decided by Python from what the project
already holds — absolute URLs written verbatim in the brief, and regular files an operator dropped
in the project's own ``sources/`` directory. No model, no plan step, no per-turn selection: this is
not an agent loop, and the stage has no way to acquire a target nobody wrote down.

Per target, in this fixed order:

1. **The egress verdict, before anything else** (ADR-0073). A Commissioner decision is rendered and
   recorded for the URL's host — on the host as *configured*, never on whether it currently
   answers. A file read has no target and is not evaluated. The decision's ``source_ref`` is the
   **invocation id**, because the attempt row does not exist yet and cannot: deciding after the
   fetch would produce an identical row and reverse the guarantee.
2. **The call**, through ToolYard's executor, under a ceiling the verdict set. An approved verdict
   passes ``EgressClass.NETWORK``; a denied one leaves the ceiling closed and ToolYard refuses the
   fetch with ``egress_not_permitted``. That is the whole enforcement — Commissioner records, the
   caller enforces (ADR-0054), and here the caller enforces in one argument.
3. **The attempt and its record, in one transaction.** Every call becomes an attempt row and a
   ``tool_call_records`` row, whatever the outcome. A refused call is ``outcome = "refused"``; a
   failed one ``provider_error``; a timeout ``timeout``.
4. **The note, only if the call succeeded.** Workflows §2's gate is "every note cites an available
   source", so a note is written only when a source was actually available, and it carries its
   citation — the URL, or the resolved path.

Nothing here raises on a refusal. A stage that swallowed one into an exception would lose the
record that says which check said no, which is the one thing an operator needs (ADR-0053).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from baseaicore import DataClassification, new_id, sha256_of
from commissioner import EgressTarget, Verdict
from sqlalchemy import delete, select
from toolyard import ToolCallRequest, ToolStatus

from ideapress.config import LOOPBACK_HOSTS
from ideapress.domain.research import brief_urls, note_title_for
from ideapress.infrastructure.db.models import Source as SourceRow
from ideapress.infrastructure.tool_calls import CollectingToolCallStore
from ideapress.services.budget import pseudo_run_id
from ideapress.services.research_tools import ResearchPlant
from ideapress.services.stages import record_attempt

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    import httpx
    from toolyard import Resolver, ToolResult

    from ideapress.services.runtime import Runtime
    from ideapress.services.stages import StageTask

__all__ = ["SOURCES_DIRECTORY_NAME", "ResearchTarget", "project_notes", "research_body"]

SOURCES_DIRECTORY_NAME = "sources"
"""The subdirectory of a project's artifact directory that `read_file` may read.

Not the artifact directory itself: exports are written there, and a research stage that could
ingest its own exports would build notes out of the document it is researching for."""

_OUTCOME_BY_STATUS = {
    ToolStatus.OK: "completed",
    # ToolYard's own line: REFUSED is a rule declining, FAILED is the world answering badly
    # (toolyard spec §13). `refused` and `provider_error` keep that distinction in the attempt, so
    # "will retrying help?" is answerable without opening the tool-call row.
    ToolStatus.REFUSED: "refused",
    ToolStatus.FAILED: "provider_error",
    ToolStatus.TIMEOUT: "timeout",
}


class ResearchTarget:
    """One thing the stage will try to turn into a note.

    Attributes:
        kind: ``url`` or ``file`` — also the `sources.kind` the note is written under.
        citation: What the note cites: the URL, or the file's name.
        call: The tool call, already shaped. The arguments are the application's, not a model's.
        host: The URL's host, for the egress decision, or ``None`` for a file.
    """

    __slots__ = ("call", "citation", "host", "kind")

    def __init__(
        self, *, kind: str, citation: str, call: ToolCallRequest, host: str | None = None
    ) -> None:
        """Build one target."""
        self.kind = kind
        self.citation = citation
        self.call = call
        self.host = host


def discover_targets(brief: str, sources_dir: Path) -> tuple[ResearchTarget, ...]:
    """The stage's whole target set, in the order it will be worked through.

    Args:
        brief: The project's brief.
        sources_dir: The project's ``sources/`` directory. A missing directory yields no files;
            that is not an error, it is the ordinary state of a project nobody dropped a file into.

    Returns:
        URLs first, in the order the brief names them, then files in sorted order — deterministic,
        so two runs of the same project try the same things in the same sequence. **Refuses**, by
        omission: a directory entry that is not a regular file (a subdirectory, a symlink to one, a
        socket), because `read_file` would fail on it and the failure would say nothing useful.
    """
    from urllib.parse import urlsplit

    targets: list[ResearchTarget] = []
    for url in brief_urls(brief):
        host = (urlsplit(url).hostname or "").lower()
        targets.append(
            ResearchTarget(
                kind="url",
                citation=url,
                call=ToolCallRequest(name="http_fetch", args={"url": url}),
                host=host,
            )
        )
    if sources_dir.is_dir():
        for entry in sorted(sources_dir.iterdir(), key=lambda path: path.name):
            if not entry.is_file():
                continue
            targets.append(
                ResearchTarget(
                    kind="file",
                    citation=entry.name,
                    call=ToolCallRequest(name="read_file", args={"path": entry.name}),
                )
            )
    return tuple(targets)


def _fetch_target(settings_ceiling: str | None, host: str) -> EgressTarget:
    """Describe one fetch host as an egress target.

    Args:
        settings_ceiling: `[research] max_data_classification`, or ``None`` when unset.
        host: The URL's host, lowercased.

    Returns:
        The target. A loopback host is never remote — nothing leaves the machine — and carries no
        ceiling, which the shipped policy approves. Any other host is remote and carries the
        declared ceiling or, unset, ``None``, which the shipped policy **denies**
        (`no_ceiling_declared`, fail closed). This is ADR-0103 decision 2 moved from the backend
        target to the fetch target, unchanged in shape and in reason.
    """
    remote = host not in LOOPBACK_HOSTS
    ceiling = (
        DataClassification(settings_ceiling) if remote and settings_ceiling is not None else None
    )
    return EgressTarget(
        name=f"research:{host}",
        remote=remote,
        max_data_classification=ceiling,
        provider_kind="http_fetch",
    )


def project_notes(runtime: Runtime, project_id: str) -> list[tuple[str, str]]:
    """The project's research notes, as `assemble_context` wants them.

    Args:
        runtime: The process's handles.
        project_id: Which project.

    Returns:
        ``(title, text)`` pairs in write order — the same rows `fact_check` checks claims against
        (`services/review_loop.py::_project_sources`), read here for workflows §7's "relevant
        research notes … ranked by explicit reference". A row with no stored text is omitted: a
        note that is empty ranks and budgets like a real one and tells a model nothing.
    """
    with runtime.storage.read() as session:
        rows = session.execute(
            select(SourceRow.title, SourceRow.content_text)
            .where(SourceRow.project_id == project_id)
            .order_by(SourceRow.created_at, SourceRow.id)
        ).all()
    return [(str(title), str(body)) for title, body in rows if body and str(body).strip()]


def _write_note(
    runtime: Runtime,
    *,
    project_id: str,
    target: ResearchTarget,
    result: ToolResult,
    invocation_id: str,
    at: datetime,
) -> None:
    """Store one successful call's text as a `sources` row carrying its citation."""
    with runtime.storage.write() as session:
        session.add(
            SourceRow(
                project_id=project_id,
                kind=target.kind,
                title=note_title_for(target.citation),
                path=target.citation,
                sha256=f"sha256:{sha256_of(result.content)}",
                content_text=result.content,
                metadata_json={
                    "invocation_id": invocation_id,
                    "retrieved_at": at.isoformat(),
                    "tool": target.call.name,
                },
            )
        )


def research_body(
    runtime: Runtime,
    *,
    project_id: str,
    resolver: Resolver | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Callable[[StageTask], None]:
    """Build the `research` stage body for one project.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        resolver: `http_fetch`'s hostname resolution, or ``None`` for the real one.
        transport: `http_fetch`'s httpx transport, or ``None`` for the real one. Both are here
            rather than on a settings object so a test drives the whole stage — egress decision,
            executor, records, notes — without opening a socket.

    Returns:
        The callable the runner executes. It **replaces** this project's previously fetched notes
        rather than appending to them: running the stage twice on the same brief must not double
        every note's weight in the context budget, and a note whose source has changed is the new
        text or nothing. Notes are the only rows it deletes; nothing else about the project moves.
    """

    def body(task: StageTask) -> None:
        database = runtime.storage
        sink = runtime.events
        settings = runtime.settings.research

        def emit(event_type: str, message: str, data: dict[str, Any]) -> None:
            sink.emit(database, task.run_id, event_type=event_type, message=message, data=data)

        project = runtime.projects.get(project_id)
        sources_dir = runtime.projects.directory(project) / SOURCES_DIRECTORY_NAME
        plant = ResearchPlant.build(
            settings, sources_dir=sources_dir, resolver=resolver, transport=transport
        )
        targets = discover_targets(project.brief_text, sources_dir)
        if not targets:
            emit(
                "research.skipped",
                "Nothing to research: the brief names no URL and the project's sources/ "
                "directory is empty.",
                {"sources_dir": str(sources_dir)},
            )
            return

        with database.write() as session:
            session.execute(delete(SourceRow).where(SourceRow.project_id == project_id))

        egress = database.egress
        run_id = pseudo_run_id(project_id)
        written = 0
        refused = 0

        for index, target in enumerate(targets, start=1):
            runtime.runner.checkpoint(task)
            invocation_id = new_id()
            at = datetime.now(UTC)

            approved = True
            if target.host is not None and egress is not None:
                decision = egress.evaluate(
                    run_id=run_id,
                    source_ref=invocation_id,
                    classification=DataClassification(
                        runtime.settings.inference.data_classification
                    ),
                    target=_fetch_target(settings.max_data_classification, target.host),
                )
                approved = decision.verdict is Verdict.APPROVED
                if not approved:
                    emit(
                        "research.egress_denied",
                        f"{target.citation}: egress denied — {decision.reason}",
                        {"citation": target.citation, "reason": decision.reason},
                    )

            store = CollectingToolCallStore()
            result = plant.executor(store).execute(
                target.call, plant.context(invocation_id, egress_approved=approved)
            )

            record_attempt(
                database,
                stage_run_id=task.run_id,
                stage="research",
                result=None,
                attempt=index,
                outcome=_OUTCOME_BY_STATUS[result.status],
                error_code=result.reason,
                error_text=result.reason_detail,
                project_id=project_id,
                tool_calls=store.records,
            )

            if result.status is ToolStatus.OK:
                _write_note(
                    runtime,
                    project_id=project_id,
                    target=target,
                    result=result,
                    invocation_id=invocation_id,
                    at=at,
                )
                written += 1
                emit(
                    "research.note_written",
                    f"{target.citation}: {len(result.content)} characters",
                    {"citation": target.citation, "kind": target.kind},
                )
            else:
                refused += 1
                emit(
                    "research.call_refused",
                    f"{target.citation}: {result.status.value} — {result.reason}",
                    {
                        "citation": target.citation,
                        "status": result.status.value,
                        "reason": result.reason,
                        "detail": result.reason_detail,
                    },
                )

        emit(
            "research.completed",
            f"{written} note(s) written, {refused} call(s) refused or failed",
            {"written": written, "refused": refused, "targets": len(targets)},
        )

    return body


def research_factory(
    runtime: Runtime, *, project_id: str, unit_keys: Sequence[str], resume: bool
) -> Callable[[StageTask], None]:
    """The `STAGE_BODIES` adapter: `research` is per project, so units and resume mean nothing.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        unit_keys: Ignored. The stage runs before a plan exists; there are no units to select.
        resume: Ignored. One pass replaces the project's notes, so resuming is re-running.

    Returns:
        The stage body.
    """
    return research_body(runtime, project_id=project_id)
