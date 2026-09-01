"""ideapress.services.stages — the stage runner: one stage, over a project's units, in a thread.

The runner owns the state machine, the attempt records and the event stream. It runs in a plain
``threading.Thread`` with explicit shutdown (architecture §5.2), holds its own database session,
and reaches a model only through :class:`~ideapress.services.inference.InferenceGateway`.

**Only one stage task runs per project at a time** (api.md §3): a second returns 409
``STAGE_ALREADY_RUNNING``. That is not the same rule as ADR-0038's — the gateway serialises
*generations* across the whole process, and this serialises *stages* within one project — and both
are needed: without this one, two stages of the same project would interleave writes to the same
units while politely taking turns at the model.

Cancellation is honoured at the next model-call boundary (workflows §9): partial output is preserved
on the attempt record and never committed.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import SuiteError
from sqlalchemy import select

from ideapress.domain.stage_state import TERMINAL_RUN_STATES
from ideapress.errors import ProjectNotFound, StageAlreadyRunning, StagePreconditionFailed
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import StageRun as StageRunRow
from ideapress.observability.logging import correlation

if TYPE_CHECKING:
    from ideapress.domain.inference import StageResult
    from ideapress.domain.stages import StageId
    from ideapress.services.database import Database
    from ideapress.services.events import StageEventSink
    from ideapress.services.inference import InferenceGateway

__all__ = ["StageRunner", "StageTask", "boot_id", "process_is_alive", "record_attempt"]


def boot_id() -> str:
    """An identifier for this boot of this machine.

    Read from ``/proc/sys/kernel/random/boot_id`` where it exists, and the machine's name
    otherwise. It is what makes a recorded PID meaningful: PIDs are reused, and a PID recorded
    before a reboot says nothing about a process running after one.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        # No /proc: fall back to the machine's name. That does not distinguish boots, so a run
        # recorded before a reboot will look like it belongs to this one — and the check then rests
        # on the PID alone, which is the conservative direction: an un-marked dead run stays
        # visible, while a marked live one loses work.
        import platform

        return f"host:{platform.node()}"


def process_is_alive(pid: int) -> bool:
    """Whether a process with this identifier exists.

    Args:
        pid: The recorded owner.

    Returns:
        Whether the process exists **now**. ``signal 0`` checks existence without delivering
        anything; a process we do not own answers `PermissionError`, which still means it exists.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # pragma: no cover — a platform without signals
        return True
    return True


logger = logging.getLogger(__name__)


class StageCancelled(SuiteError):
    """Raised inside a stage body when the user cancelled it. Not an error the API surfaces."""

    code = "STAGE_CANCELLED"


@dataclass
class StageTask:
    """One running stage: its identity, its thread, and the flag that stops it."""

    run_id: str
    project_id: str
    stage: str
    thread: threading.Thread | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def request_cancel(self) -> None:
        """Ask the stage to stop at its next model-call boundary."""
        self.cancel.set()


def record_attempt(
    database: Database,
    *,
    stage_run_id: str,
    stage: str,
    result: StageResult | None,
    unit_id: str | None = None,
    attempt: int = 1,
    round_: int = 0,
    prompt_id: str | None = None,
    prompt_version: str | None = None,
    prompt_sha256: str | None = None,
    outcome: str = "completed",
    error_code: str | None = None,
    error_text: str | None = None,
    store_content: bool = False,
) -> str:
    """Write one attempt record — the unit of provenance (workflows §8).

    Args:
        database: Where to write.
        stage_run_id: Which run.
        stage: Which stage.
        result: What the model produced, or ``None`` for a deterministic step or a failure.
        unit_id: Which unit, when the attempt is about one.
        attempt: Which attempt within the stage.
        round_: The revision round; 0 for the first pass.
        prompt_id, prompt_version, prompt_sha256: Which prompt record produced it.
        outcome: ``completed``, ``validation_failed``, ``provider_error``, ``timeout``,
            ``cancelled`` or ``content_rejected``.
        error_code, error_text: For a failure.
        store_content: Whether the prompt and response text may be stored. Off by default: this is
            the user's private work and hashes are enough for provenance (data model §4).

    Returns:
        The attempt's identifier.
    """
    from baseaicore import sha256_of

    with database.write() as session:
        row = AttemptRow(
            stage_run_id=stage_run_id,
            unit_id=unit_id,
            stage=stage,
            attempt=attempt,
            round=round_,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            outcome=outcome,
            error_code=error_code,
            error_text=error_text,
        )
        if result is not None:
            row.backend = result.backend
            row.model_provider_kind = result.model.provider_kind if result.model else None
            row.model_provider_name = result.model.provider_model_name if result.model else None
            row.model_digest = result.model.artifact_digest if result.model else None
            row.model_canonical_id = result.model.canonical_id if result.model else None
            row.input_tokens = result.usage.input_tokens
            row.output_tokens = result.usage.output_tokens
            row.thinking_tokens = result.usage.thinking_tokens
            row.provider_ms = result.timing.duration_ms
            row.ttft_ms = result.timing.ttft_ms
            row.response_hash = f"sha256:{sha256_of(result.text)}"
            row.degradations_json = list(result.degradations)
            row.rejection_reason = result.refusal_reason
            row.routing_json = dict(result.routing) if result.routing else None
            # LoadCoach's adapter carries the key it submitted under in the routing map;
            # recording it is what lets a resumed project prove a retry replayed rather
            # than duplicating (P7's idempotency claim).
            key = (result.routing or {}).get("idempotency_key")
            row.idempotency_key = str(key) if key else None
            if store_content:
                row.response_text = result.text
        session.add(row)
        session.flush()
        return row.id


class StageRunner:
    """Starts, tracks and cancels stage runs for this process.

    One active run per project, enforced here and by a repository check rather than by hope: a
    second start returns 409 rather than quietly interleaving two writers over the same units.
    """

    def __init__(
        self,
        database: Database,
        *,
        gateway: InferenceGateway,
        sink: StageEventSink,
        store_content: bool = False,
    ) -> None:
        """Bind the runner to a database, the inference choke point and the event sink."""
        self._database = database
        self._gateway = gateway
        self._sink = sink
        self._store_content = store_content
        self._tasks: dict[str, StageTask] = {}
        self._lock = threading.Lock()

    @property
    def gateway(self) -> InferenceGateway:
        """The single door to a model. Exposed so a stage body reaches it and nothing else does."""
        return self._gateway

    @property
    def sink(self) -> StageEventSink:
        """The event sink, for a stage body to emit through."""
        return self._sink

    @property
    def store_content(self) -> bool:
        """Whether attempts may store prompt and response text."""
        return self._store_content

    def active_task(self, project_id: str) -> StageTask | None:
        """The running task for this project, if any."""
        with self._lock:
            return self._tasks.get(project_id)

    def task(self, run_id: str) -> StageTask | None:
        """The task with this run identifier, if it is still tracked."""
        with self._lock:
            for task in self._tasks.values():
                if task.run_id == run_id:
                    return task
        return None

    def start(
        self,
        *,
        project_id: str,
        stage: StageId,
        body: Callable[[StageTask], None],
        options: dict[str, Any] | None = None,
        units_total: int = 0,
    ) -> StageTask:
        """Start one stage in a background thread.

        Args:
            project_id: Which project.
            stage: Which stage.
            body: What to run. Receives the task, so it can check ``task.cancel``.
            options: Recorded on the run for the UI and for a resume.
            units_total: How many units the stage expects to touch.

        Returns:
            The task, already running.

        Raises:
            StageAlreadyRunning: A stage task is already running for this project.
            ProjectNotFound: No such project.
        """
        from ideapress.infrastructure.db.repositories import projects as project_repository

        with self._database.read() as session:
            if project_repository.get_by_id(session, project_id) is None:
                message = f"No project with id {project_id!r}."
                raise ProjectNotFound(message, details={"project_id": project_id})

        with self._lock:
            existing = self._tasks.get(project_id)
            if existing is not None and existing.thread is not None and existing.thread.is_alive():
                message = (
                    f"A {existing.stage!r} stage is already running for this project. Only one "
                    "runs at a time; cancel it or wait for it to finish."
                )
                raise StageAlreadyRunning(
                    message, details={"project_id": project_id, "task_id": existing.run_id}
                )

            with self._database.write() as session:
                run = StageRunRow(
                    project_id=project_id,
                    stage=stage,
                    state="running",
                    units_total=units_total,
                    options_json=dict(options or {}),
                    backend=self._gateway.backend.name,
                    backend_mode=self._gateway.backend.name,
                    owner_pid=os.getpid(),
                    owner_boot_id=boot_id(),
                )
                session.add(run)
                session.flush()
                run_id = run.id

            task = StageTask(run_id=run_id, project_id=project_id, stage=stage)
            self._tasks[project_id] = task

        self._sink.emit(
            self._database, run_id, event_type="stage.started", message=f"{stage} started"
        )
        thread = threading.Thread(
            target=self._run, args=(task, body), name=f"ideapress-{stage}", daemon=True
        )
        task.thread = thread
        thread.start()
        return task

    def _run(self, task: StageTask, body: Callable[[StageTask], None]) -> None:
        """Run a stage body, and record how it ended whatever happens."""
        with correlation(project_id=task.project_id, stage=task.stage):
            try:
                body(task)
            except StageCancelled:
                self._finish(task, state="cancelled", event="stage.failed", message="cancelled")
                return
            except SuiteError as exc:
                logger.warning("stage.failed", extra={"code": exc.code})
                self._finish(
                    task,
                    state="failed",
                    event="stage.failed",
                    message=exc.message,
                    error_code=exc.code,
                )
                return
            except Exception as exc:  # noqa: BLE001 — a stage thread must never die silently
                logger.error("stage.unhandled_error", exc_info=exc)
                self._finish(
                    task,
                    state="failed",
                    event="stage.failed",
                    message=str(exc),
                    error_code="INTERNAL_ERROR",
                )
                return
            self._finish(task, state="completed", event="stage.completed", message="completed")

    def _finish(
        self,
        task: StageTask,
        *,
        state: str,
        event: str,
        message: str,
        error_code: str | None = None,
    ) -> None:
        """Close a run: write its terminal state, then emit its terminal event, in that order."""
        now = datetime.now(UTC)
        with self._database.write() as session:
            run = session.get(StageRunRow, task.run_id)
            if run is not None:
                run.state = state
                run.completed_at = now
                if state == "cancelled":
                    run.cancelled_at = now
                run.error_code = error_code
                run.error_text = message if error_code else None
        self._sink.emit(
            self._database,
            task.run_id,
            event_type=event,
            message=message,
            data={"state": state, "error_code": error_code},
        )
        with self._lock:
            if self._tasks.get(task.project_id) is task:
                del self._tasks[task.project_id]

    def cancel(self, run_id: str) -> bool:
        """Ask a run to stop at its next model-call boundary.

        Returns:
            Whether a running task was found to cancel. Cancelling an already-finished run is not
            an error — it is a race a user cannot avoid.
        """
        task = self.task(run_id)
        if task is None:
            return False
        task.request_cancel()
        return True

    def checkpoint(self, task: StageTask) -> None:
        """Raise if this stage has been cancelled. Called at every model-call boundary.

        Raises:
            StageCancelled: The user cancelled. Partial output already written to an attempt record
                is preserved; nothing is committed.
        """
        if task.cancel.is_set():
            message = "The stage was cancelled."
            raise StageCancelled(message, details={"task_id": task.run_id})

    def mark_interrupted(self) -> int:
        """Mark runs left ``running`` **by a dead process** as ``interrupted``.

        Returns:
            How many runs were marked.

        Called whenever a runtime is built (workflows §9). A run whose process died is not
        ``failed`` — nothing went wrong with it — and it is not ``running`` either, because nothing
        is running it. The distinction is what lets ``--resume`` pick it up rather than treat it as
        a defeat.

        **Only a run whose owner is gone.** The first version marked every ``running`` row, and the
        M7 demonstration found what that costs: a read-only inspection from a second process marked
        a live draft — three units in — as interrupted, after which the runner refused the next
        stage because the thread was still going. A row is marked only when its ``owner_boot_id``
        matches this boot *and* its ``owner_pid`` names no living process; a row from an earlier
        boot is marked too, because nothing from that boot can still be running. A row with no
        recorded owner is left alone: it predates this column, and refusing to guess is the safe
        direction — an un-marked dead run is visible and resumable by hand, while a marked live one
        corrupts work in progress.

        PID reuse within one boot could in principle make a dead run look alive. The consequence is
        that it stays ``running`` until someone looks, which is the harmless failure.
        """
        current_boot = boot_id()
        with self._database.write() as session:
            rows = session.scalars(
                select(StageRunRow).where(StageRunRow.state.in_(("running", "queued")))
            ).all()
            marked = 0
            for row in rows:
                if row.owner_pid is None:
                    continue
                same_boot = row.owner_boot_id == current_boot
                if same_boot and process_is_alive(row.owner_pid):
                    continue
                row.state = "interrupted"
                marked += 1
            return marked

    def run_state(self, run_id: str) -> str | None:
        """The stored state of a run, or ``None`` when there is no such run."""
        with self._database.read() as session:
            run = session.get(StageRunRow, run_id)
            return run.state if run is not None else None

    def is_finished(self, run_id: str) -> bool:
        """Whether a run has reached a terminal state — what the SSE stream closes on."""
        return (self.run_state(run_id) or "completed") in TERMINAL_RUN_STATES

    def require_not_running(self, project_id: str) -> None:
        """Refuse an action that a running stage would race.

        Raises:
            StagePreconditionFailed: A stage is running for this project.
        """
        task = self.active_task(project_id)
        if task is not None and task.thread is not None and task.thread.is_alive():
            message = f"A {task.stage!r} stage is running for this project; wait or cancel it."
            raise StagePreconditionFailed(
                message, details={"project_id": project_id, "task_id": task.run_id}
            )
