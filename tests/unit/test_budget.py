"""``ideapress.services.budget`` — the LoadLedger mount (row J1 D1-D5, ADR-0069)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from baseaicore import ModelIdentity, ModelPricing, PricingSource, ProviderKind, TokenRates
from loadledger import CurrencyMismatch

from ideapress.config import load_settings
from ideapress.domain.inference import TokenUsage as DomainTokenUsage
from ideapress.services.budget import (
    NOT_PRICED,
    BudgetService,
    CurrencyMismatchError,
    project_tag,
    pseudo_run_id,
)
from ideapress.services.database import Database, upgrade
from ideapress.services.pricing import PricingCatalog

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 9, 7, tzinfo=UTC)


def _clock() -> datetime:
    return AT


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database.from_url(f"sqlite:///{tmp_path / 't.sqlite3'}")
    upgrade(db)
    return db


def _service(
    database: Database, *, budget_overrides: dict[str, object] | None = None
) -> BudgetService:
    settings = load_settings(cli_overrides={"budget": budget_overrides or {}}).settings
    return BudgetService(database, settings, PricingCatalog(), clock=_clock)


def _priced_service(database: Database, records: tuple[ModelPricing, ...]) -> BudgetService:
    settings = load_settings().settings
    return BudgetService(database, settings, PricingCatalog(records=records), clock=_clock)


_PRICED_MODEL = ModelIdentity(provider_kind=ProviderKind.OLLAMA, provider_model_name="gemma4:12b")


def test_project_tag_and_pseudo_run_id() -> None:
    assert project_tag("p1") == "project:p1"
    assert pseudo_run_id("p1") == project_tag("p1")


def test_ceilings_for_is_empty_when_nothing_is_configured(database: Database) -> None:
    service = _service(database)
    assert service.ceilings_for(project_id="p1") == ()


def test_ceilings_for_builds_only_the_configured_halves(database: Database) -> None:
    service = _service(
        database,
        budget_overrides={
            "per_output_token_ceiling": 1000,
            "per_project_money_ceiling": {"currency": "USD", "nanos": 2_000_000_000},
        },
    )
    ceilings = service.ceilings_for(project_id="p1")
    scopes = {c.scope.value for c in ceilings}
    assert scopes == {"per_run", "per_tag"}
    per_run = next(c for c in ceilings if c.scope.value == "per_run")
    per_tag = next(c for c in ceilings if c.scope.value == "per_tag")
    assert per_run.tokens == 1000
    assert per_run.money is None
    assert per_tag.money is not None and per_tag.money.nanos == 2_000_000_000
    assert per_tag.tag == "project:p1"


def test_price_of_a_local_attempt_with_no_model_identity_is_unpriced(database: Database) -> None:
    service = _service(database)
    priced = service.price(
        canonical_id=None, usage=DomainTokenUsage(input_tokens=10, output_tokens=5), at=AT
    )
    assert priced.cost is None
    assert "did not disclose" in priced.unpriced_reason


def test_price_with_no_catalogue_record_is_unpriced_never_zero(database: Database) -> None:
    service = _service(database)
    priced = service.price(
        canonical_id="ollama/gemma4:12b",
        usage=DomainTokenUsage(input_tokens=10, output_tokens=5),
        at=AT,
    )
    assert priced.cost is None
    assert "UNSUPPORTED" in priced.unpriced_reason


def test_a_debit_of_unpriced_usage_accumulates_tokens_and_touches_no_money(
    database: Database,
) -> None:
    service = _service(database)
    with database.write() as session:
        entry = service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=service.price(
                canonical_id=None,
                usage=DomainTokenUsage(input_tokens=100, output_tokens=50),
                at=AT,
            ),
            at=AT,
        )
    assert entry.unpriced is True
    assert entry.pricing_hash is None
    cost = service.project_cost(project_id="p1")
    assert cost["money_spent_display"] == NOT_PRICED
    # Always a token floor too (`unmetered_debit_count`): IdeaPress's own domain `TokenUsage`
    # carries no cache-token fields at all, so every debit leaves two token classes unreported —
    # the same reality ADR-0069 documents for every real ModelRack adapter.
    assert cost["tokens_spent_display"] == "at least 150"


def test_a_debit_of_priced_usage_derives_money_and_stores_a_pricing_hash(
    database: Database,
) -> None:
    """A backend that reported no cache class leaves a floor, and money still accrues.

    Row K4 gave the domain type the two cache classes; leaving them unset is still what a backend
    that reported nothing produces, and it is still a floor. The next test is the other half.
    """
    from baseaicore import Money

    pricing = ModelPricing(
        identity=_PRICED_MODEL,
        rates=TokenRates(
            currency="USD",
            input_per_million_tokens=Money.from_decimal("USD", "1.00"),
            output_per_million_tokens=Money.from_decimal("USD", "2.00"),
        ),
        source=PricingSource.USER_OVERRIDE,
        observed_at=AT,
    )
    service = _priced_service(database, (pricing,))
    priced = service.price(
        canonical_id="ollama/gemma4:12b",
        usage=DomainTokenUsage(input_tokens=1_000_000, output_tokens=1_000_000),
        at=AT,
    )
    assert priced.cost is not None
    assert not priced.cost.is_complete  # neither cache class was reported (ADR-0069)
    with database.write() as session:
        entry = service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=priced,
            at=AT,
        )
    assert entry.unpriced is True
    assert entry.pricing_hash is not None
    cost = service.project_cost(project_id="p1")
    assert cost["money_spent_display"] == "at least 3 USD"
    assert cost["money_is_floor"] is True
    assert cost["unpriced_debit_count"] == 1
    assert cost["untotalled_debit_count"] == 1


def test_a_debit_with_every_class_reported_and_priced_totals_rather_than_flooring(
    database: Database,
) -> None:
    """Row K4's point: a bare figure, not "at least", once nothing is missing.

    ADR-0070 rule 1 says a protocol that bills no cache tier reports ``0`` — a real zero, not the
    fabricated one ADR-0016 forbids — so a fully priced attempt against such a backend has nothing
    left unreported and nothing left unpriced. Before this row IdeaPress's domain type carried no
    cache fields at all, so this figure said "at least" forever, for a reason that had nothing to
    do with the call (J1 handoff §8).
    """
    from baseaicore import Money

    pricing = ModelPricing(
        identity=_PRICED_MODEL,
        rates=TokenRates(
            currency="USD",
            input_per_million_tokens=Money.from_decimal("USD", "1.00"),
            output_per_million_tokens=Money.from_decimal("USD", "2.00"),
            cache_write_per_million_tokens=Money.from_decimal("USD", "1.25"),
            cache_read_per_million_tokens=Money.from_decimal("USD", "0.10"),
        ),
        source=PricingSource.USER_OVERRIDE,
        observed_at=AT,
    )
    service = _priced_service(database, (pricing,))
    priced = service.price(
        canonical_id="ollama/gemma4:12b",
        usage=DomainTokenUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_write_tokens=0,
            cache_read_tokens=0,
        ),
        at=AT,
    )
    assert priced.cost is not None
    assert priced.cost.is_complete
    assert priced.unpriced_reason == ""
    with database.write() as session:
        entry = service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=priced,
            at=AT,
        )
    assert entry.unpriced is False
    cost = service.project_cost(project_id="p1")
    assert cost["money_spent_display"] == "3 USD"
    assert cost["tokens_spent_display"] == "2000000"
    assert cost["money_is_floor"] is False
    assert cost["unpriced_debit_count"] == 0
    assert cost["untotalled_debit_count"] == 0
    assert cost["unmetered_debit_count"] == 0


def test_an_unreported_cache_class_still_floors_a_fully_rated_price(database: Database) -> None:
    """The rule is per class, not per price list: one `None` is enough to keep the floor."""
    from baseaicore import Money

    pricing = ModelPricing(
        identity=_PRICED_MODEL,
        rates=TokenRates(
            currency="USD",
            input_per_million_tokens=Money.from_decimal("USD", "1.00"),
            output_per_million_tokens=Money.from_decimal("USD", "2.00"),
            cache_write_per_million_tokens=Money.from_decimal("USD", "1.25"),
            cache_read_per_million_tokens=Money.from_decimal("USD", "0.10"),
        ),
        source=PricingSource.USER_OVERRIDE,
        observed_at=AT,
    )
    service = _priced_service(database, (pricing,))
    priced = service.price(
        canonical_id="ollama/gemma4:12b",
        usage=DomainTokenUsage(
            input_tokens=1_000_000, output_tokens=1_000_000, cache_write_tokens=0
        ),
        at=AT,
    )
    assert priced.cost is not None
    assert not priced.cost.is_complete
    with database.write() as session:
        service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=priced,
            at=AT,
        )
    assert service.project_cost(project_id="p1")["money_spent_display"] == "at least 3 USD"


def test_an_unreported_input_count_is_never_priced_as_zero(database: Database) -> None:
    """Row K4: LoadCoach sends `"unsupported"` for a count it does not have (ADR-0016 rule 4).

    That arrives here as `None`, becomes `UNSUPPORTED`, and is excluded — so the estimate does not
    total and the figure is a floor. The coercion this replaced read it as `0`, which priced a
    real call's whole input side at nothing and reported the result as a measured total.
    """
    from baseaicore import Money

    pricing = ModelPricing(
        identity=_PRICED_MODEL,
        rates=TokenRates(
            currency="USD",
            input_per_million_tokens=Money.from_decimal("USD", "1.00"),
            output_per_million_tokens=Money.from_decimal("USD", "2.00"),
            cache_write_per_million_tokens=Money.from_decimal("USD", "1.25"),
            cache_read_per_million_tokens=Money.from_decimal("USD", "0.10"),
        ),
        source=PricingSource.USER_OVERRIDE,
        observed_at=AT,
    )
    service = _priced_service(database, (pricing,))
    priced = service.price(
        canonical_id="ollama/gemma4:12b",
        usage=DomainTokenUsage(
            input_tokens=None,
            output_tokens=1_000_000,
            cache_write_tokens=0,
            cache_read_tokens=0,
        ),
        at=AT,
    )
    assert priced.cost is not None
    assert not priced.cost.is_complete
    assert priced.unpriced_reason
    with database.write() as session:
        service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=priced,
            at=AT,
        )
    cost = service.project_cost(project_id="p1")
    # The output side alone, announced as a floor — not "3 USD", and never "0 USD".
    assert cost["money_spent_display"] == "at least 2 USD"
    assert cost["tokens_spent_display"] == "at least 1000000"
    assert cost["unmetered_debit_count"] == 1


def test_a_partial_estimate_omits_an_unpriced_component_and_still_floors(
    database: Database,
) -> None:
    """ADR-0069: an estimate missing a *rate* accumulates what it could, same as missing usage."""
    from baseaicore import Money

    pricing = ModelPricing(
        identity=_PRICED_MODEL,
        rates=TokenRates(
            currency="USD", input_per_million_tokens=Money.from_decimal("USD", "1.00")
        ),
        source=PricingSource.USER_OVERRIDE,
        observed_at=AT,
    )
    service = _priced_service(database, (pricing,))
    priced = service.price(
        canonical_id="ollama/gemma4:12b",
        usage=DomainTokenUsage(input_tokens=1_000_000, output_tokens=1_000_000),
        at=AT,
    )
    assert priced.cost is not None
    assert not priced.cost.is_complete
    with database.write() as session:
        entry = service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=priced,
            at=AT,
        )
    assert entry.unpriced is True
    cost = service.project_cost(project_id="p1")
    assert cost["money_is_floor"] is True
    assert cost["unpriced_debit_count"] == 1
    assert cost["untotalled_debit_count"] == 1
    assert cost["money_spent_display"].startswith("at least ")
    # A missing output rate leaves only the input side priced, less than the two-sided case above.
    assert cost["money_spent_display"] == "at least 1 USD"


def test_unit_cost_has_no_ceiling_when_none_configured_but_still_renders(
    database: Database,
) -> None:
    service = _service(database)
    with database.write() as session:
        service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=service.price(
                canonical_id=None,
                usage=DomainTokenUsage(input_tokens=10, output_tokens=5),
                at=AT,
            ),
            at=AT,
        )
    cost = service.unit_cost(project_id="p1", unit_id="unit1")
    assert cost["has_ceiling"] is False
    assert cost["tokens_spent_display"] == "at least 15"


def test_unit_cost_reports_a_configured_ceiling_and_exceeds_it(database: Database) -> None:
    service = _service(database, budget_overrides={"per_output_token_ceiling": 10})
    with database.write() as session:
        service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=service.price(
                canonical_id=None,
                usage=DomainTokenUsage(input_tokens=100, output_tokens=50),
                at=AT,
            ),
            at=AT,
        )
    cost = service.unit_cost(project_id="p1", unit_id="unit1")
    assert cost["has_ceiling"] is True
    assert cost["exceeded"] is True
    assert cost["tokens_remaining"] is not None and cost["tokens_remaining"] < 0


def test_project_cost_accumulates_across_units_lifetime_and_never_resets(
    database: Database,
) -> None:
    service = _service(database)
    with database.write() as session:
        for run_id in ("unit1", "unit2"):
            service.debit(
                session,
                project_id="p1",
                run_id=run_id,
                source_ref=f"attempt-{run_id}",
                stage="draft",
                backend="ollama",
                priced=service.price(
                    canonical_id=None,
                    usage=DomainTokenUsage(input_tokens=10, output_tokens=5),
                    at=AT,
                ),
                at=AT,
            )
    cost = service.project_cost(project_id="p1")
    assert cost["tokens_spent_display"] == "at least 30"


def test_unit_cost_with_a_ceiling_but_no_debit_or_declaration_reads_the_full_cap(
    database: Database,
) -> None:
    """The `UnknownRun` fallback: defensive, and correct for data written before this row."""
    service = _service(database, budget_overrides={"per_output_token_ceiling": 500})
    cost = service.unit_cost(project_id="p1", unit_id="never-touched")
    assert cost["has_ceiling"] is True
    assert cost["exceeded"] is False
    assert cost["tokens_remaining"] == 500


def test_project_cost_with_a_configured_ceiling_reports_headroom(database: Database) -> None:
    service = _service(database, budget_overrides={"per_project_token_ceiling": 1000})
    with database.write() as session:
        service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=service.price(
                canonical_id=None, usage=DomainTokenUsage(input_tokens=10, output_tokens=5), at=AT
            ),
            at=AT,
        )
    cost = service.project_cost(project_id="p1")
    assert cost["has_ceiling"] is True
    assert cost["exceeded"] is False
    assert cost["tokens_remaining"] == 985


def test_render_money_and_render_tokens_of_none() -> None:
    from ideapress.services.budget import render_money, render_tokens

    assert render_money(None, is_floor=False) == NOT_PRICED
    assert render_tokens(None, is_floor=False) == NOT_PRICED


def test_declare_run_lets_a_fresh_unit_read_an_empty_window(database: Database) -> None:
    service = _service(database, budget_overrides={"per_output_token_ceiling": 100})
    with database.write() as session:
        service.declare_run(session, "unit1")
    cost = service.unit_cost(project_id="p1", unit_id="unit1")
    assert cost["has_ceiling"] is True
    assert cost["exceeded"] is False
    assert cost["tokens_remaining"] == 100


def test_a_currency_mismatch_is_raised_as_a_domain_error(database: Database) -> None:
    from baseaicore import Money

    settings = load_settings(
        cli_overrides={
            "budget": {"per_output_money_ceiling": {"currency": "USD", "nanos": 1_000_000_000}}
        }
    ).settings
    pricing = ModelPricing(
        identity=_PRICED_MODEL,
        rates=TokenRates(
            currency="EUR", input_per_million_tokens=Money.from_decimal("EUR", "1.00")
        ),
        source=PricingSource.USER_OVERRIDE,
        observed_at=AT,
    )
    priced_service = BudgetService(
        database, settings, PricingCatalog(records=(pricing,)), clock=_clock
    )
    priced = priced_service.price(
        canonical_id="ollama/gemma4:12b",
        usage=DomainTokenUsage(input_tokens=1_000_000, output_tokens=0),
        at=AT,
    )
    assert priced.cost is not None
    with pytest.raises(CurrencyMismatchError), database.write() as session:
        priced_service.debit(
            session,
            project_id="p1",
            run_id="unit1",
            source_ref="attempt1",
            stage="draft",
            backend="ollama",
            priced=priced,
            at=AT,
        )


def test_loadledger_currency_mismatch_is_never_raised_directly() -> None:
    """The public surface raises this application's own error, never the package's."""
    assert issubclass(CurrencyMismatchError, ValueError)
    assert not issubclass(CurrencyMismatchError, CurrencyMismatch)
