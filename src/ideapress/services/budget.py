"""ideapress.services.budget — the LoadLedger mount: ceilings, pricing and debits (row J1, IP-A1).

LoadLedger keeps the balances and decides every ``exceeded``; this module decides **what the
ceilings are** and **what IdeaPress does about a verdict** (ADR-0050 split). Nothing here adds up
tokens or compares a balance to a cap — that would be ledger logic living in an application, which
is the mistake the mount exists to avoid. Transcribed from ``promptcadence.services.budget``, and
much smaller: IdeaPress has no tiers, no approvals and one configured backend at a time, so there is
one flat pricing catalogue rather than a per-tier map and no pre-flight estimator — a stage attempt
is recorded *after* the model has answered (``services.stages.record_attempt``), never sized ahead
of one.

**Two ceilings, both optional and independent** (D4). ``per_output`` binds one *run* — a unit's own
attempts, or, for a stage with no unit (``plan``, ``project_review``), the project's pseudo-run
``project:<id>`` (D1). ``per_project`` binds every attempt tagged with the project, for the
project's whole lifetime, and never resets — LoadLedger's ``PER_TAG`` scope with no window that
ever closes. Naming neither half of a ceiling means it does not exist, and the ledger still
accumulates and still renders (row J1 exit 1) — an unset ceiling is not a ceiling of zero.

**Debits store usage and a pricing hash, never money** (ADR-0030 rule 1). :meth:`BudgetService.
price` rebuilds :class:`baseaicore.TokenUsage` from all four classes
:class:`ideapress.domain.inference.TokenUsage` now carries (row K4). A cache class the backend left
unreported arrives as ``None`` and becomes ``UNSUPPORTED``, excluded from any total rather than
counted as zero (ADR-0016) — never a fabricated ``$0.00``.

**Honesty is the whole feature** (ADR-0030, ADR-0069). A local model's cost is ``UNSUPPORTED``:
:meth:`BudgetService.price` returns ``cost=None`` and every surface renders :data:`NOT_PRICED`, an
em dash, with the reason beside it — never ``0``. A price list that could not total an estimate
(a reported class the catalogue states no rate for) produces a **floor**: the components that were
priced accumulate, the components that were not are excluded, and every rendered figure says "at
least" (ADR-0069's ``PartialPricing.FLOOR``, the default; ``STRICT`` is configurable).

**A debit is a floor only when something really was unreported** (row K4). Until this row,
``ideapress.domain.inference.TokenUsage`` carried no cache fields at all, so every debit left both
cache classes ``UNSUPPORTED``, ADR-0069's ``unmetered_debit_count`` was never zero, and every
figure said "at least" forever — priced or not, and for a reason that had nothing to do with the
call. The domain type now carries all four classes and the adapters fill them: a protocol that
bills no cache tier reports ``0``, which is honest and totals (ADR-0070 rule 1), and a class that
genuinely went unreported stays ``None`` and keeps its floor. ``partial_pricing = "strict"`` is
therefore usable against a backend that reports all four; the ``floor`` default stays the right
choice where one does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import Money, ValidationError, estimate_cost
from loadledger import (
    BudgetCeiling,
    CeilingScope,
    CurrencyMismatch,
    Debit,
    PartialPricing,
    UnknownRun,
)
from loadledger.sql import SqlLedger
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from baseaicore import CostEstimate
    from baseaicore import TokenUsage as BaseTokenUsage
    from loadledger import LedgerEntry, WindowBalance

    from ideapress.config import MoneyAmount, Settings
    from ideapress.domain.inference import TokenUsage as DomainTokenUsage
    from ideapress.services.database import Database
    from ideapress.services.pricing import PricingCatalog

__all__ = [
    "NOT_PRICED",
    "PROJECT_TAG_PREFIX",
    "BudgetService",
    "CurrencyMismatchError",
    "PricedUsage",
    "project_tag",
    "pseudo_run_id",
    "render_money",
    "render_tokens",
]

NOT_PRICED = "—"
"""What an unpriced amount renders as, everywhere: an em dash. Never ``$0.00`` (ADR-0016)."""

_AT_LEAST = "at least "
PROJECT_TAG_PREFIX = "project:"


def project_tag(project_id: str) -> str:
    """Return the tag every debit for this project carries, and the ``PER_TAG`` ceiling binds on."""
    return f"{PROJECT_TAG_PREFIX}{project_id}"


def pseudo_run_id(project_id: str) -> str:
    """The run identity a stage attempt with no unit debits against (D1): ``project:<id>``.

    ``plan`` and ``project_review`` produce attempts with no ``unit_id``; debiting nothing would
    silently under-count the project's real cost, so these attempts spend against a project-level
    pseudo-run instead of a unit's.
    """
    return project_tag(project_id)


class CurrencyMismatchError(ValueError):
    """A debit priced in a currency an active money ceiling caps in another. Never converted."""


@dataclass(frozen=True, slots=True)
class PricedUsage:
    """One attempt's usage and what it was estimated to cost, ready to become a debit.

    Attributes:
        usage: What the backend reported, as ``baseaicore.TokenUsage``. A cache class the
            backend did not report stays ``UNSUPPORTED`` and is excluded from any total; one it
            reported as ``0`` (its protocol bills no such class — ADR-0070 rule 1) is a real
            zero, and a debit whose four classes are all reported totals rather than floors.
        cost: The estimate, or ``None`` when no pricing was applied at all — the local case, and
            the case of a catalogue that does not cover the model that answered.
        unpriced_reason: Why ``cost`` is ``None``, or why the estimate did not total. Empty when
            the estimate is complete.
    """

    usage: BaseTokenUsage
    cost: CostEstimate | None
    unpriced_reason: str = ""


class BudgetService:
    """The application's half of the budget: ceilings, pricing, debits and reads.

    Stateless and cheap, like the ledger it wraps. Built once at process start
    (:mod:`ideapress.services.runtime`) and reached from every attempt through
    :attr:`~ideapress.services.database.Database.budget` — the one handle every stage-recording
    call site already threads through, which is what lets :func:`ideapress.services.stages.
    record_attempt` debit without a signature change reaching the callers that use it.
    """

    __slots__ = ("_clock", "_database", "_pricing", "_settings")

    def __init__(
        self,
        database: Database,
        settings: Settings,
        pricing: PricingCatalog,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind the service to the process's handles.

        Args:
            database: The application's database handle; the ledger's tables live in it.
            settings: The validated configuration — every ceiling's source.
            pricing: The price observations loaded at startup.
            clock: The instant source, injected and required — every window and day edge in this
                module depends on it, and a ledger reading the system clock could not be tested
                across a UTC midnight.
        """
        self._database = database
        self._settings = settings
        self._pricing = pricing
        self._clock = clock

    @property
    def pricing(self) -> PricingCatalog:
        """The price observations loaded at startup."""
        return self._pricing

    # ---- ceilings -----------------------------------------------------------------------------

    def ceilings_for(self, *, project_id: str) -> tuple[BudgetCeiling, ...]:
        """Build the ceilings active on one project's work, in the order verdicts come back in.

        Args:
            project_id: The project.

        Returns:
            Zero, one or two ceilings: the ``per_output`` ``PER_RUN`` ceiling when
            ``[budget] per_output_money_ceiling``/``per_output_token_ceiling`` names either half,
            then the ``per_project`` ``PER_TAG`` ceiling when its two keys name either half. An
            unset ceiling is *omitted*, never constructed with neither bound — LoadLedger refuses
            that combination (``InvalidCeiling``) and "no ceiling" is a different fact from "a
            ceiling of zero".
        """
        budget = self._settings.budget
        rule = PartialPricing(budget.partial_pricing)
        ceilings: list[BudgetCeiling] = []
        output_money = _money(budget.per_output_money_ceiling)
        if output_money is not None or budget.per_output_token_ceiling is not None:
            ceilings.append(
                BudgetCeiling(
                    scope=CeilingScope.PER_RUN,
                    money=output_money,
                    tokens=budget.per_output_token_ceiling,
                    partial_pricing=rule if output_money is not None else PartialPricing.FLOOR,
                )
            )
        project_money = _money(budget.per_project_money_ceiling)
        if project_money is not None or budget.per_project_token_ceiling is not None:
            ceilings.append(
                BudgetCeiling(
                    scope=CeilingScope.PER_TAG,
                    tag=project_tag(project_id),
                    money=project_money,
                    tokens=budget.per_project_token_ceiling,
                    partial_pricing=rule if project_money is not None else PartialPricing.FLOOR,
                )
            )
        return tuple(ceilings)

    def tags_for(self, *, project_id: str, stage: str, backend: str) -> tuple[str, ...]:
        """Return the tags every debit carries (D1): the project, the stage and the backend."""
        return (project_tag(project_id), f"stage:{stage}", f"backend:{backend}")

    # ---- the ledger -----------------------------------------------------------------------------

    def ledger(
        self, *, ceilings: Sequence[BudgetCeiling] = (), session: Session | None = None
    ) -> SqlLedger:
        """Build a ledger over the mounted tables.

        Args:
            ceilings: The ceilings to evaluate. Empty for a read that needs no verdict.
            session: A session to join, or ``None`` for the ledger's own unit of work. When given,
                the ledger's writes land inside the caller's transaction as a savepoint, so a debit
                and the attempt row it belongs to commit together or not at all (ADR-0044 applied
                to money).

        Returns:
            A ledger bound to this application's tables and injected clock.
        """
        if session is None:
            factory: Callable[[], Session] = self._database.sessions
        else:
            connection = session.connection()

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

        return SqlLedger(factory, tuple(ceilings), clock=self._clock)

    def declare_run(self, session: Session, unit_id: str) -> None:
        """Register a unit with the ledger, at plan creation (``services.plan.store_plan``).

        Called before any attempt exists, so a per-unit cost view on a freshly planned unit reads
        a real empty window rather than raising :class:`~loadledger.UnknownRun` (LoadLedger spec
        §13: "a run exists once debited *or* declared").
        """
        self.ledger(session=session).declare_run(unit_id)

    # ---- pricing and debiting -------------------------------------------------------------------

    def price(
        self, *, canonical_id: str | None, usage: DomainTokenUsage, at: datetime
    ) -> PricedUsage:
        """Cost one attempt's usage against the catalogue's own price record at the instant it ran.

        Args:
            canonical_id: The model that answered, or ``None`` when the backend did not disclose
                one.
            usage: The token classes IdeaPress's domain reports. A cache class left ``None``
                by the backend becomes ``UNSUPPORTED``, so it is excluded from the estimate rather
                than counted as zero (ADR-0016).
            at: When the attempt happened — not when this runs, so re-costing history later
                reproduces the same figure (ADR-0030).

        Returns:
            The usage with its estimate, or with ``cost=None`` and a reason: no model identity, no
            catalogue entry for these weights (the local case, and the "not covered" case alike).
        """
        from baseaicore import UNSUPPORTED
        from baseaicore import TokenUsage as BaseTokenUsage

        base_usage = BaseTokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            # `None` is "the backend reported no such class", which is `UNSUPPORTED` — never 0.
            # A backend whose protocol cannot bill a class reports it as 0 itself (ADR-0070
            # rule 1), and that is what lets the estimate total.
            cache_write_tokens=(
                UNSUPPORTED if usage.cache_write_tokens is None else usage.cache_write_tokens
            ),
            cache_read_tokens=(
                UNSUPPORTED if usage.cache_read_tokens is None else usage.cache_read_tokens
            ),
        )
        if canonical_id is None:
            return PricedUsage(
                usage=base_usage,
                cost=None,
                unpriced_reason="the backend did not disclose which model answered",
            )
        pricing = self._pricing.for_model(canonical_id=canonical_id, at=at)
        if pricing is None:
            return PricedUsage(
                usage=base_usage,
                cost=None,
                unpriced_reason=(
                    f"no price record for {canonical_id!r} at {at.isoformat()}; a local model's "
                    "cost is UNSUPPORTED, never $0.00 (ADR-0016)"
                ),
            )
        cost = estimate_cost(base_usage, pricing, at=at)
        reason = "; ".join(cost.unpriced_reasons) if not cost.is_complete else ""
        return PricedUsage(usage=base_usage, cost=cost, unpriced_reason=reason)

    def debit(
        self,
        session: Session,
        *,
        project_id: str,
        run_id: str,
        source_ref: str,
        stage: str,
        backend: str,
        priced: PricedUsage,
        at: datetime,
    ) -> LedgerEntry:
        """Record one attempt's spend, inside the caller's transaction.

        Args:
            session: The caller's session; the debit rides its transaction as a savepoint.
            project_id: Which project — the ``per_project`` ceiling and the project tag.
            run_id: The unit id, or the project's pseudo-run (:func:`pseudo_run_id`) for a stage
                attempt with no unit (D1).
            source_ref: The attempt's own id. Idempotent by construction: each attempt is recorded
                exactly once, at the one funnel every stage routes through.
            stage: The stage name, for the ``stage:<name>`` tag.
            backend: The backend that answered, for the ``backend:<name>`` tag.
            priced: The usage and its estimate.
            at: When the attempt happened.

        Returns:
            The recorded entry, whole — every active ceiling's verdict after this debit.

        Raises:
            CurrencyMismatchError: The estimate is priced in a currency an active money ceiling
                caps in another. Refused before anything is written.
        """
        ceilings = self.ceilings_for(project_id=project_id)
        debit = Debit(
            run_id=run_id,
            source_ref=source_ref,
            usage=priced.usage,
            cost=priced.cost,
            tags=self.tags_for(project_id=project_id, stage=stage, backend=backend),
            occurred_at=at,
        )
        try:
            return self.ledger(ceilings=ceilings, session=session).debit(debit)
        except CurrencyMismatch as exc:
            raise CurrencyMismatchError(exc.message) from exc

    # ---- reads for the page ---------------------------------------------------------------------

    def unit_cost(self, *, project_id: str, unit_id: str) -> dict[str, Any]:
        """What one unit's own run has spent, and what its ``per_output`` ceiling says (D5).

        Reads spend through :meth:`loadledger.Ledger.balances` (``PER_RUN``, ``window_key=unit_id``)
        rather than :meth:`~loadledger.Ledger.remaining`, deliberately: ``balances`` reads through
        no ceiling at all, so a unit's own cost renders honestly whether or not
        ``[budget] per_output_*`` is configured — "the ledger still accumulates and still renders"
        holds even with zero ceilings (row J1 exit 1), which ``remaining`` alone cannot do, since a
        verdict requires a ceiling to exist. The ceiling's own ``exceeded``/remaining figures are
        added on top of that spend when a ``per_output`` ceiling *is* configured.
        """
        view = balance_view(self.ledger().balances(scope=CeilingScope.PER_RUN, window_key=unit_id))
        per_run = next(
            (
                c
                for c in self.ceilings_for(project_id=project_id)
                if c.scope is CeilingScope.PER_RUN
            ),
            None,
        )
        if per_run is None:
            return {**view, "has_ceiling": False, "exceeded": False}
        try:
            (verdict,) = self.ledger(ceilings=(per_run,)).remaining(unit_id)
        except UnknownRun:
            # Declared at plan creation (`services.plan.store_plan`) whenever governance is
            # attached; unreachable in production, and defensive for data written before this row.
            return {
                **view,
                "has_ceiling": True,
                "exceeded": False,
                "money_remaining": per_run.money.as_canonical() if per_run.money else None,
                "tokens_remaining": per_run.tokens,
            }
        return {
            **view,
            "has_ceiling": True,
            "exceeded": verdict.exceeded,
            "money_remaining": (
                verdict.money_remaining.as_canonical()
                if verdict.money_remaining is not None
                else None
            ),
            "tokens_remaining": verdict.tokens_remaining,
        }

    def project_cost(self, *, project_id: str) -> dict[str, Any]:
        """The project's lifetime spend under its ``PER_TAG`` window, and its ceiling if any (D5).

        See :meth:`unit_cost`: the same ceiling-agnostic ``balances`` read, so a project's cost
        badge renders whether or not ``[budget] per_project_*`` is configured. The ceiling's own
        verdict, when configured, comes from :meth:`loadledger.Ledger.position` rather than
        ``remaining`` — a ``PER_TAG`` window is ledger-wide (its key is the tag, not a run id), and
        ``position`` is LoadLedger's reader for exactly that shape; it never raises
        :class:`~loadledger.UnknownRun`, unlike ``remaining`` over a run identity nothing declared.
        """
        tag = project_tag(project_id)
        view = balance_view(self.ledger().balances(scope=CeilingScope.PER_TAG, window_key=tag))
        per_project = next(
            (
                c
                for c in self.ceilings_for(project_id=project_id)
                if c.scope is CeilingScope.PER_TAG
            ),
            None,
        )
        if per_project is None:
            return {**view, "has_ceiling": False, "exceeded": False}
        (verdict,) = self.ledger(ceilings=(per_project,)).position()
        return {
            **view,
            "has_ceiling": True,
            "exceeded": verdict.exceeded,
            "money_remaining": (
                verdict.money_remaining.as_canonical()
                if verdict.money_remaining is not None
                else None
            ),
            "tokens_remaining": verdict.tokens_remaining,
        }


def render_money(amount: Money | None, *, is_floor: bool) -> str:
    """Render one spent money figure, the one way this application allows.

    Returns:
        :data:`NOT_PRICED` for ``None`` (nothing priced, or the ceiling binds no money);
        ``"at least 0.004 USD"`` for a floor; the bare figure otherwise. A floor is never rendered
        bare, because "this is what it cost" over an incomplete sum is a claim nobody can make
        (ADR-0069).
    """
    if amount is None:
        return NOT_PRICED
    rendered = f"{amount.to_decimal()} {amount.currency}"
    return f"{_AT_LEAST}{rendered}" if is_floor else rendered


def render_tokens(count: int | None, *, is_floor: bool) -> str:
    """Render one spent token count, with :func:`render_money`'s floor rule.

    A token count is a floor too whenever a backend left a class unreported: excluded rather than
    counted as zero, so the count is a lower bound.
    """
    if count is None:
        return NOT_PRICED
    return f"{_AT_LEAST}{count}" if is_floor else str(count)


def balance_view(balance: WindowBalance) -> dict[str, Any]:
    """Render one window's accumulated spend — the project cost badge's shape.

    Money is a **list**, one entry per currency, even where today's configuration only ever
    produces one: a window's currency set is open and the figures are never summed across it
    (ADR-0030 rule 3).
    """
    money_is_floor = balance.unpriced_debit_count > 0
    displays = [render_money(one, is_floor=money_is_floor) for one in balance.money_spent]
    return {
        "tokens_spent_display": render_tokens(
            balance.tokens_spent, is_floor=balance.unmetered_debit_count > 0
        ),
        "money_spent_display": ", ".join(displays) if displays else NOT_PRICED,
        "money_is_floor": money_is_floor,
        "unpriced_debit_count": balance.unpriced_debit_count,
        "untotalled_debit_count": balance.untotalled_debit_count,
        "unmetered_debit_count": balance.unmetered_debit_count,
    }


def _money(amount: MoneyAmount | None) -> Money | None:
    """Convert a configured amount to `baseaicore.Money`, or ``None`` for unset or zero.

    A zero-nanos configured ceiling means "no money ceiling here", not "spend nothing": a zero cap
    would be exceeded before anything is spent, and an operator who left the default meant to leave
    the ceiling unset.
    """
    if amount is None or amount.nanos <= 0:
        return None
    try:
        return Money(currency=amount.currency, nanos=amount.nanos)
    except ValidationError:  # pragma: no cover — config already validated the currency
        return None
