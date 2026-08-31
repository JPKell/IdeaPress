"""ideapress.services.events — persisted stage events, gap-free sequences and live fan-out.

Risk T7 is a long-running stage lost to a crash or a refresh, and the mitigation is that events are
**persisted before they are published**: the table is the source of truth and the in-memory broker
is only a latency optimisation. A browser that reconnects with ``Last-Event-ID`` replays the missed
events out of the database and then follows the live stream, which is what makes a refresh
survivable and a restart resumable.

Sequence numbers are dense from 1 with no gaps, because replay-from-N is only correct if N+1
exists. The number is assigned inside the same transaction as the insert; a lock serialises the
read-then-insert within this process and the unique constraint on ``(stage_run_id, sequence)``
catches anything the lock cannot see.

**Token frames are persisted too**, unlike LoadCoach's, and deliberately: a drafting stage produces
one document rather than a firehose, the tokens *are* the unit's content as it arrives, and a
reader who refreshes mid-draft should see what was written rather than an empty pane until the
stage ends. The volume is bounded by the unit's own length band.
"""

from __future__ import annotations

import logging
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mirrorwall import Event, EventBroker
from sqlalchemy import func, select

from ideapress.infrastructure.db.models import StageEvent as StageEventRow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mirrorwall import Subscription
    from sqlalchemy.orm import Session

    from ideapress.services.database import Database

__all__ = ["TERMINAL_STAGE_EVENTS", "StageEventRecord", "StageEventSink", "StageEventSource"]

logger = logging.getLogger(__name__)

TERMINAL_STAGE_EVENTS: frozenset[str] = frozenset({"stage.completed", "stage.failed"})
"""Seeing one of these closes the stream. `stage.cancelled` is reported as `stage.failed` with a
cancellation code, so a client has one rule rather than three."""


@dataclass(frozen=True, slots=True)
class StageEventRecord:
    """One persisted stage event, as everything above the repository sees it."""

    sequence: int
    event_type: str
    message: str
    unit_key: str | None
    data: dict[str, Any]
    timestamp_iso: str

    def as_event(self) -> Event:
        """Render as MirrorWall's SSE event.

        Every frame carries the SetSpec event envelope **except** ``token``, which is bare
        (ADR-0025 §3): a token frame is one fragment of an answer arriving thousands of times, and
        wrapping each in an envelope would cost more than it carries.
        """
        if self.event_type == "token":
            return Event(sequence=self.sequence, type="token", payload=self.data.get("text", ""))
        return Event(
            sequence=self.sequence,
            type=self.event_type,
            payload={
                "event": self.event_type,
                "occurred_at": self.timestamp_iso,
                "message": self.message,
                "unit_key": self.unit_key,
                "data": self.data,
            },
        )


class StageEventSink:
    """Writes an event, then publishes it. In that order, always.

    A publish-before-persist would let a client see an event the database never got, which is the
    one failure a replay cannot repair.
    """

    def __init__(self) -> None:
        """Build the broker. One per process, held by the runtime."""
        self._broker = EventBroker()
        self._lock = threading.Lock()

    @property
    def broker(self) -> EventBroker:
        """The live fan-out."""
        return self._broker

    def emit(
        self,
        database: Database,
        stage_run_id: str,
        *,
        event_type: str,
        message: str = "",
        unit_id: str | None = None,
        unit_key: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        """Persist one event and publish it. Returns its sequence number."""
        payload = dict(data or {})
        with self._lock:
            with database.write() as session:
                sequence = self._next_sequence(session, stage_run_id)
                session.add(
                    StageEventRow(
                        stage_run_id=stage_run_id,
                        sequence=sequence,
                        event_type=event_type,
                        message=message,
                        unit_id=unit_id,
                        data_json=payload,
                    )
                )
                session.flush()
            record = StageEventRecord(
                sequence=sequence,
                event_type=event_type,
                message=message,
                unit_key=unit_key,
                data=payload,
                timestamp_iso="",
            )
            self._broker.publish(stage_run_id, record.as_event())
        return sequence

    @staticmethod
    def _next_sequence(session: Session, stage_run_id: str) -> int:
        """The next dense sequence number for this run."""
        highest = session.scalar(
            select(func.max(StageEventRow.sequence)).where(
                StageEventRow.stage_run_id == stage_run_id
            )
        )
        return int(highest or 0) + 1

    def source(self, database: Database, stage_run_id: str) -> StageEventSource:
        """Build the :class:`~mirrorwall.EventSource` this run's SSE endpoint reads from."""
        return StageEventSource(self, database, stage_run_id)


class StageEventSource:
    """Replays persisted events, then follows the broker — MirrorWall's `EventSource` protocol."""

    def __init__(self, sink: StageEventSink, database: Database, stage_run_id: str) -> None:
        """Bind to one run."""
        self._sink = sink
        self._database = database
        self._run_id = stage_run_id

    def replay(self, *, stream_id: str, after_sequence: int, limit: int) -> Sequence[Event]:
        """Return up to ``limit`` persisted events after ``after_sequence``, in order."""
        return [record.as_event() for record in self.records(after=after_sequence, limit=limit)]

    def subscribe(self, *, stream_id: str) -> AbstractContextManager[Subscription]:
        """Open a live subscription to this run."""
        return self._sink.broker.subscribe(stream_id=stream_id)

    def records(self, *, after: int = 0, limit: int = 500) -> list[StageEventRecord]:
        """The persisted events after ``after``, oldest first, as value objects."""
        with self._database.read() as session:
            rows = session.scalars(
                select(StageEventRow)
                .where(StageEventRow.stage_run_id == self._run_id, StageEventRow.sequence > after)
                .order_by(StageEventRow.sequence)
                .limit(limit)
            ).all()
            return [_to_record(session, row) for row in rows]


def _to_record(session: Session, row: StageEventRow) -> StageEventRecord:
    """Convert a row into a value object, resolving the unit's key for display."""
    unit_key: str | None = None
    if row.unit_id is not None:
        from ideapress.infrastructure.db.models import Unit as UnitRow

        unit = session.get(UnitRow, row.unit_id)
        unit_key = unit.unit_key if unit is not None else None
    return StageEventRecord(
        sequence=row.sequence,
        event_type=row.event_type,
        message=row.message,
        unit_key=unit_key,
        data=dict(row.data_json),
        timestamp_iso=row.timestamp.isoformat(),
    )
