"""ideapress.infrastructure.tool_calls — where ToolYard's records land in this application.

ToolYard owns no data (its spec §10): it defines :class:`toolyard.ToolCallRecord` and one method to
append one, and the application owns the table, the retention and the migration (ADR-0116). This
module is that ownership, and it is deliberately the *only* place a ToolYard record becomes a row.

**Why the store collects rather than writes.** The executor appends its record from inside
:meth:`toolyard.ToolExecutor.execute`, and that call may be an ``http_fetch`` spending its whole
read timeout on an origin that is answering slowly. A store that wrote through to a session would
hold a database transaction open across that wait — on SQLite, a write lock. So
:class:`CollectingToolCallStore` collects during execution and the research stage hands the
collected records to :func:`~ideapress.services.stages.record_attempt`, which maps each through
:func:`tool_call_row` onto the same transaction the attempt row commits in. The ADR-0044
property survives: the record and the attempt that owns it are one write, and a crash between them
is impossible.

**Why a failed append must raise.** ToolYard turns a raising store into
:class:`toolyard.StoreFailure`, carrying the result and the record on the exception, precisely
because a side effect may already have happened and losing its audit trail is the worse failure.
Collection cannot fail, so the raise that matters here is the insert's, and it happens inside the
attempt's transaction where a rollback loses the attempt too — which is the correct outcome: an
attempt whose tool call could not be recorded must not be reported as having run.

Transcribed from ``promptcadence.infrastructure.tool_calls``, which took this shape first (row E4).
What differs is what the links carry: PromptCadence points a record at the ``TOOL`` turn that
returned it to a model, and IdeaPress has no such turn — the research stage decides its calls from
the project, and a result reaches no model until ``research_synthesis`` reads the note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ideapress.infrastructure.db import models

if TYPE_CHECKING:
    from collections.abc import Sequence

    from toolyard import ToolCallRecord

__all__ = ["CollectingToolCallStore", "tool_call_row"]


def tool_call_row(
    record: ToolCallRecord, *, project_id: str, attempt_id: str
) -> models.ToolCallRecord:
    """Map one ToolYard record onto the ``tool_call_records`` row.

    Every field ToolYard produces is carried through unchanged — including ``args_json`` being
    ``None``, and ``tool_name`` holding a name no tool has, because a refusal that does not say
    what was asked for cannot be diagnosed.

    Args:
        record: What the executor produced for one call, whatever its outcome.
        project_id: The project the call belongs to.
        attempt_id: The attempt the call belongs to.

    Returns:
        The row, ready to be added to a session the caller owns. The identifier is left unset so
        the model's own ULID default mints it — the same source every other row here uses.
    """
    return models.ToolCallRecord(
        project_id=project_id,
        attempt_id=attempt_id,
        invocation_id=record.invocation_id,
        tool_name=record.tool_name,
        args_json=record.args_json,
        args_sha256=record.args_sha256,
        status=record.status.value,
        reason=record.reason,
        reason_detail=record.reason_detail,
        result_summary=record.result_summary,
        result_sha256=record.result_sha256,
        duration_ms=record.duration_ms,
        risk_class=record.risk_class.value,
        egress=record.egress.value,
        started_at=record.started_at,
    )


class CollectingToolCallStore:
    """ToolYard's :class:`toolyard.ToolCallStore`, collecting for a later flush.

    One instance per call, because the research stage writes each call's record and its attempt in
    one transaction and then moves on to the next target. Collecting more than one would only
    delay the write.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        """Create an empty store."""
        self._records: list[ToolCallRecord] = []

    def append(self, record: ToolCallRecord) -> None:
        """Collect one record.

        Args:
            record: The record for one call, whatever its outcome. Refusals and failures included:
                a fetch an operator is trying to work out why they never got is exactly the row
                this table exists for.
        """
        self._records.append(record)

    @property
    def records(self) -> Sequence[ToolCallRecord]:
        """Return the collected records, in append order.

        Returns:
            A tuple snapshot, so a caller iterating it cannot be surprised by a later append.
        """
        return tuple(self._records)
