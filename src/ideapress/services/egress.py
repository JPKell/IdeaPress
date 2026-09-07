"""ideapress.services.egress — the Commissioner mount: evaluate, record, read (row J1, IP-A2).

**The direction of this integration is easy to get backwards.** Commissioner *renders and records*
a verdict; enforcing it stays IdeaPress's own job (ADR-0054) — the refusal a remote, no-ceiling
configuration gets is still ``config.load_settings``'s ``_validate_egress``/``providers.
allow_remote`` gate, unchanged by this module. Every use here is the same two steps: build the
request, ask the policy — and then *record* the verdict, never enforce it.

**Evaluated on configuration, before availability** (ADR-0073): :func:`backend_target` reads the
configured backend's own settings — its base URL's host, its declared ``max_data_classification`` —
never whether it currently answers. A local backend is evaluated too, not skipped: it is what makes
"every attempt carries a decision" a checkable property rather than one with quiet exceptions.

**Fail closed** (D6, ADR-0054 rule 3): a remote backend with no declared
``max_data_classification`` is *denied*, never assumed public. This is deliberately a **behaviour
change** for an existing remote configuration that named no ceiling — before this row the badge
merely displayed "leaves this machine"; from this row a recorded decision says so and denies. See
``CHANGELOG.md`` and ``upgrading.md``.

**One run identity for egress, D1's for the ledger.** :func:`ideapress.services.budget.
pseudo_run_id`'s per-unit/per-project split governs *accumulation*, which egress decisions do not
do — each row is independent (spec §10). This module still uses D1's ``run_id`` per attempt (so a
decision joins to its attempt by reference, D7, D8), but the workspace badge — "the most recent
decision for this project" — reads by **target name** instead: IdeaPress has exactly one configured
backend for the whole process, so every project's most recent decision against that target *is* the
installation's current egress posture, and filtering by target rather than assembling every unit's
run id into one query is both simpler and exactly as correct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from commissioner import EgressDecision, EgressRequest, EgressTarget, OrderedClassificationPolicy
from commissioner.sql import SqlEgressLedger
from sqlalchemy.orm import Session

from ideapress.config import LOOPBACK_HOSTS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from baseaicore import DataClassification
    from commissioner import EgressPolicy, Verdict

    from ideapress.config import Settings
    from ideapress.services.database import Database

__all__ = ["EgressService", "backend_target", "decision_view"]


def backend_target(settings: Settings, backend_name: str) -> EgressTarget:
    """Describe the currently configured backend as an egress target (D6).

    Args:
        settings: The validated configuration.
        backend_name: ``attempt.backend`` / ``InferenceBackend.name`` — ``"ollama"``,
            ``"loadcoach"`` or ``"openai_compatible"``.

    Returns:
        The target. Ollama is never remote — it is a direct local process, matching the badge's
        pre-existing "stays on this machine" special case. The other two are remote exactly when
        their configured ``base_url`` names a host off this machine — the same test the deleted
        ``services.workspace._is_remote`` made, so the badge's copy keeps meaning what it always
        meant — and, when remote, carry the operator's declared ``max_data_classification`` or
        ``None`` when unset, which the shipped policy denies (fail closed).

        An unrecognized or empty ``backend_name`` — no backend could be built at all — is reported
        local with no ceiling, which the policy approves: there is no configured destination for
        anything to leave through.
    """
    inference = settings.inference
    if backend_name == "ollama":
        return EgressTarget(name="ollama", remote=False, max_data_classification=None)
    if backend_name == "loadcoach":
        remote = _host_is_remote(inference.loadcoach.base_url)
        ceiling = _ceiling(inference.loadcoach.max_data_classification) if remote else None
        return EgressTarget(
            name="loadcoach",
            remote=remote,
            max_data_classification=ceiling,
            provider_kind="loadcoach",
        )
    if backend_name == "openai_compatible":
        remote = _host_is_remote(inference.openai_compatible.base_url)
        ceiling = _ceiling(inference.openai_compatible.max_data_classification) if remote else None
        return EgressTarget(
            name="openai_compatible",
            remote=remote,
            max_data_classification=ceiling,
            provider_kind="openai_compatible",
        )
    return EgressTarget(name=backend_name or "none", remote=False, max_data_classification=None)


def _host_is_remote(base_url: str) -> bool:
    """Whether a configured endpoint's host is off this machine. Empty is never remote."""
    if not base_url:
        return False
    host = (urlsplit(base_url).hostname or "").lower()
    return host not in LOOPBACK_HOSTS


def _ceiling(value: str | None) -> DataClassification | None:
    """Read a configured ``max_data_classification`` string into the ordered enum."""
    from baseaicore import DataClassification

    return DataClassification(value) if value else None


class EgressService:
    """Evaluate, record and read every egress decision this application makes.

    Stateless between calls, like :class:`~ideapress.services.budget.BudgetService`: a ledger is
    built per operation over the mounted table, so a decision written inside a caller's transaction
    and one written in its own unit of work are the same code path with a different session.
    """

    __slots__ = ("_database", "_policy")

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime],
        policy: EgressPolicy | None = None,
    ) -> None:
        """Bind the service to the database and the policy that decides.

        Args:
            database: The application's database handle; Commissioner's table lives in it.
            clock: The instant source, injected and required; it stamps ``decided_at``.
            policy: The policy to evaluate with. Defaults to Commissioner's shipped
                :class:`~commissioner.OrderedClassificationPolicy`.
        """
        self._database = database
        self._policy: EgressPolicy = (
            policy if policy is not None else OrderedClassificationPolicy(clock=clock)
        )

    def ledger(self, *, session: Session | None = None) -> SqlEgressLedger:
        """Build a ledger over the mounted table.

        Args:
            session: A session to join, or ``None`` for the ledger's own unit of work. When given,
                the write lands inside the caller's transaction as a savepoint, so a decision and
                the attempt it belongs to commit together or not at all (ADR-0044).
        """
        if session is None:
            factory: Callable[[], Session] = self._database.sessions
        else:
            connection = session.connection()

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

        return SqlEgressLedger(factory)

    def evaluate(
        self,
        *,
        run_id: str,
        source_ref: str,
        classification: DataClassification,
        target: EgressTarget,
        session: Session | None = None,
    ) -> EgressDecision:
        """Decide one egress request and record the verdict, whatever it is.

        There is no variant of this method that decides without recording — an approval that was
        not written down is indistinguishable, afterwards, from a call nobody governed (spec §11
        contract 1).

        Args:
            run_id: D1's run identity for the attempt being evaluated.
            source_ref: The attempt's own id — how a recorded decision joins to it by reference,
                never by a SQL join to the mounted table (ADR-0050 decision 2).
            classification: The installation's declared ``inference.data_classification``. Never
                derived from model output.
            target: Where the data would go, from :func:`backend_target`.
            session: A session to join, so the decision commits with its caller's write.

        Returns:
            The recorded decision. This method never raises to signal a denial — a denial is an
            expected, recorded outcome, not an error.
        """
        decision = self._policy.evaluate(
            EgressRequest(
                run_id=run_id,
                source_ref=source_ref,
                data_classification=classification,
                target=target,
            )
        )
        self.ledger(session=session).record(decision)
        return decision

    def decisions(
        self,
        *,
        run_id: str | None = None,
        verdict: Verdict | None = None,
        target: str | None = None,
        since: datetime | None = None,
    ) -> Sequence[EgressDecision]:
        """Read recorded decisions, oldest-decided first, narrowed by whichever filters given."""
        return self.ledger().decisions(run_id=run_id, verdict=verdict, target=target, since=since)

    def latest_for_target(self, target: str) -> EgressDecision | None:
        """The most recent recorded decision against one target — what the workspace badge reads.

        Returns:
            The newest decision by ``decided_at``, or ``None`` when nothing has been evaluated
            against this target yet — a fresh installation, or one whose backend just changed. A
            caller renders that as "nothing has run yet", never as the pre-J1 ad-hoc flag.
        """
        found = self.decisions(target=target)
        if not found:
            return None
        return max(found, key=lambda decision: decision.decided_at)


def decision_view(decision: EgressDecision) -> dict[str, Any]:
    """Render one decision for the workspace badge and a unit's provenance table.

    Built from :meth:`~commissioner.EgressDecision.to_payload` rather than field by field, so the
    wire shape *is* SetSpec's ``governance.egress_decision`` 1.0 and cannot drift from it by an edit
    here.
    """
    payload: Any = decision.to_payload()
    return cast("dict[str, Any]", payload.model_dump(mode="json"))
