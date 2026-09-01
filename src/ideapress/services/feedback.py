"""ideapress.services.feedback — telling LoadCoach how its work turned out.

After a unit commits, IdeaPress knows something LoadCoach cannot: whether the text it routed was
accepted, and whether IdeaPress's deterministic validation passed. That is the signal LoadCoach's
`reliability_factor` and its regression detection are built on, and sending it is the fourth clause
of P7's acceptance.

**Once per committed unit, and once only.** The plan's named failure mode is feedback posted more
than once, which would let one unit's opinion count several times toward a model's production
evidence. Two mechanisms stand behind that, deliberately belt-and-braces:

* LoadCoach is idempotent per ``(job_id, source)`` and `source` is taken from the `X-Client-Name`
  header rather than the body, so a repeat updates rather than duplicating; and
* this module keeps its own record of what it has sent, so the repeat is not even attempted after
  a resume — a network that swallowed the response is a different thing from work not done.

Feedback is **never** allowed to fail a commit. The unit is already written; a LoadCoach that
stopped answering between the generation and the commit is a degradation to record, not a reason to
undo finished work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from ideapress.domain.inference import InferenceBackend
    from ideapress.services.database import Database

__all__ = ["FeedbackOutcome", "job_ids_for_unit", "send_unit_feedback"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """What one round of feedback did.

    Attributes:
        sent: Job ids feedback was posted for, in order.
        skipped: Job ids already recorded as sent, so not posted again.
        failed: Job ids LoadCoach would not accept, with the reason.
    """

    sent: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def posted_count(self) -> int:
        """How many jobs were told about on this call."""
        return len(self.sent)


def job_ids_for_unit(
    database: Database, *, project_id: str, unit_key: str
) -> tuple[tuple[str, str | None], ...]:
    """The LoadCoach jobs that contributed to one unit, newest last.

    Args:
        database: The project database.
        project_id: Which project the unit belongs to — checked, not assumed.
        unit_key: The unit's stable key (``U-01``), resolved to its row here so callers speak the
            identifier a person uses rather than a ULID.

    Returns:
        ``(job_id, attempt_id)`` pairs for every attempt that recorded one, de-duplicated and in
        attempt order. Empty when the unit was produced by a backend that has no jobs, which is the
        normal standalone case and not a failure.

    ``project_id`` is a filter, not decoration. M7-29's general shape — *any query that decides
    something about other processes must know who owns what* — applies to a query that decides what
    to tell another application about: feedback attributed to the wrong project would corrupt the
    evidence it feeds.
    """
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Attempt, StageRun, Unit

    with database.read() as session:
        rows = session.execute(
            select(Attempt.id, Attempt.routing_json)
            .join(StageRun, StageRun.id == Attempt.stage_run_id)
            .join(Unit, Unit.id == Attempt.unit_id)
            .where(
                StageRun.project_id == project_id,
                Unit.project_id == project_id,
                Unit.unit_key == unit_key,
                Attempt.routing_json.is_not(None),
            )
            .order_by(Attempt.created_at, Attempt.id)
        ).all()

    seen: set[str] = set()
    found: list[tuple[str, str | None]] = []
    for attempt_id, routing in rows:
        if not isinstance(routing, dict):
            continue
        job_id = str(routing.get("job_id") or "")
        if job_id and job_id not in seen:
            seen.add(job_id)
            found.append((job_id, attempt_id))
    return tuple(found)


def send_unit_feedback(
    database: Database,
    backend: InferenceBackend,
    *,
    project_id: str,
    unit_key: str,
    accepted: bool,
    validation_passed: bool | None = None,
    edited: bool = False,
    notes: str | None = None,
    already_sent: Iterable[str] = (),
) -> FeedbackOutcome:
    """Post feedback for every LoadCoach job behind one committed unit.

    Args:
        database: The project database.
        backend: The adapter in use. A backend with no ``post_feedback`` is skipped entirely —
            feedback is LoadCoach's mechanism and standalone modes have nowhere to send it.
        project_id: The unit's project.
        unit_key: The committed unit's key.
        accepted: Whether the unit committed. ``False`` says the work was not used, which is as
            useful to routing as a success.
        validation_passed: Whether IdeaPress's deterministic validation passed.
        edited: Whether the text was revised before commit.
        notes: A short note; truncated by the adapter to LoadCoach's 4 000-character limit.
        already_sent: Job ids feedback has already been posted for, so a resumed project does not
            post twice.

    Returns:
        A :class:`FeedbackOutcome` naming what was sent, skipped and refused.

    Never raises. A LoadCoach that stopped answering between the generation and the commit is
    recorded in ``failed`` and logged at WARNING; the unit is already committed and nothing about
    finished work is undone because a report about it could not be delivered.
    """
    poster = getattr(backend, "post_feedback", None)
    if not callable(poster):
        return FeedbackOutcome()

    seen = set(already_sent)
    sent: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for job_id, _attempt_id in job_ids_for_unit(database, project_id=project_id, unit_key=unit_key):
        if job_id in seen:
            skipped.append(job_id)
            continue
        try:
            poster(
                job_id,
                accepted=accepted,
                validation_passed=validation_passed,
                edited=edited,
                notes=notes,
            )
        except Exception as exc:  # noqa: BLE001 — a delivery failure never fails a commit
            logger.warning(
                "feedback.not_delivered",
                extra={"project_id": project_id, "unit_key": unit_key, "job_id": job_id},
            )
            failed.append((job_id, str(exc)))
            continue
        seen.add(job_id)
        sent.append(job_id)
        logger.info(
            "feedback.sent",
            extra={"project_id": project_id, "unit_key": unit_key, "job_id": job_id},
        )

    return FeedbackOutcome(sent=tuple(sent), skipped=tuple(skipped), failed=tuple(failed))


def feedback_summary(outcomes: Sequence[FeedbackOutcome]) -> Mapping[str, int]:
    """Totals across several units, for a stage report.

    Args:
        outcomes: What each committed unit's feedback did.

    Returns:
        ``{"sent": n, "skipped": n, "failed": n}``.
    """
    return {
        "sent": sum(len(outcome.sent) for outcome in outcomes),
        "skipped": sum(len(outcome.skipped) for outcome in outcomes),
        "failed": sum(len(outcome.failed) for outcome in outcomes),
    }
