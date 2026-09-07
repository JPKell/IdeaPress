"""``services.stages.record_attempt`` as the budget/egress debit site (row J1, D1-D8).

The funnel every stage attempt already routes through (kickoff ground truth 2) is where a debit
and an egress decision are recorded, with no change to the nine call sites — three of which row J2
owns. This proves the funnel does its job when governance is attached, leaves the attempt
recording path untouched when it is not, and demonstrates D4's bound-ceiling pause.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from baseaicore import ModelIdentity as BaseModelIdentity
from baseaicore import ModelPricing, Money, PricingSource, ProviderKind, TokenRates
from sqlalchemy import select

from ideapress.config import load_settings
from ideapress.domain.inference import ModelIdentity, StageResult, TokenUsage
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import Project as ProjectRow
from ideapress.infrastructure.db.models import StageRun as StageRunRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.budget import BudgetService, project_tag, pseudo_run_id
from ideapress.services.database import Database, upgrade
from ideapress.services.egress import EgressService
from ideapress.services.pricing import PricingCatalog
from ideapress.services.stages import record_attempt

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


def _project_and_run(database: Database, *, backend: str = "ollama") -> tuple[str, str]:
    with database.write() as session:
        project = ProjectRow(
            title="T",
            slug="t",
            content_type="article",
            content_type_version="1.0",
            workflow_id="default",
            workflow_version="1.0",
            status="active",
            brief_text="b",
            author_material_json=[],
            config_json={},
        )
        session.add(project)
        session.flush()
        run = StageRunRow(
            project_id=project.id,
            stage="draft",
            state="running",
            units_total=1,
            started_at=AT,
            options_json={},
            backend=backend,
            backend_mode=backend,
        )
        session.add(run)
        session.flush()
        return project.id, run.id


def _unit(database: Database, project_id: str, *, state: str = "drafting") -> str:
    with database.write() as session:
        unit = UnitRow(
            project_id=project_id,
            unit_key="U-01",
            ordinal=1,
            title="Unit",
            state=state,
        )
        session.add(unit)
        session.flush()
        return unit.id


def _result(
    *, backend: str = "ollama", input_tokens: int = 100, output_tokens: int = 50
) -> StageResult:
    return StageResult(
        text="hello",
        model=ModelIdentity(provider_kind="ollama", provider_model_name="gemma4:12b"),
        backend=backend,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _attach(
    database: Database,
    *,
    budget_overrides: dict[str, object] | None = None,
    mode: str = "ollama",
    remote: bool = False,
) -> None:
    inference: dict[str, object] = {"mode": mode}
    overrides: dict[str, object] = {"budget": budget_overrides or {}, "inference": inference}
    if remote:
        inference["openai_compatible"] = {"base_url": "https://api.example.com/v1"}
        overrides["providers"] = {"allow_remote": True}
    settings = load_settings(cli_overrides=overrides).settings
    budget = BudgetService(database, settings, PricingCatalog(), clock=_clock)
    egress = EgressService(database, clock=_clock)
    database.attach_governance(budget=budget, egress=egress, settings=settings)


def test_with_no_governance_attached_record_attempt_behaves_exactly_as_before(
    database: Database,
) -> None:
    """The bare `Database` every existing test already builds must see no change."""
    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id)
    attempt_id = record_attempt(
        database, stage_run_id=run_id, stage="draft", result=_result(), unit_id=unit_id
    )
    assert attempt_id
    assert database.budget is None
    assert database.egress is None


def test_an_attempt_debits_the_budget_ledger_under_the_unit_as_run_id(database: Database) -> None:
    _attach(database)
    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id)
    record_attempt(database, stage_run_id=run_id, stage="draft", result=_result(), unit_id=unit_id)
    assert database.budget is not None
    cost = database.budget.unit_cost(project_id=project_id, unit_id=unit_id)
    assert cost["tokens_spent_display"] == "at least 150"
    project_cost = database.budget.project_cost(project_id=project_id)
    assert project_cost["tokens_spent_display"] == "at least 150"


def test_a_stage_attempt_with_no_unit_debits_the_project_pseudo_run(database: Database) -> None:
    """D1: `plan`/`project_review` have no unit, so nothing is silently undebited."""
    _attach(database)
    project_id, run_id = _project_and_run(database)
    record_attempt(database, stage_run_id=run_id, stage="project_review", result=_result())
    assert database.budget is not None
    entries = database.budget.ledger().entries(run_id=pseudo_run_id(project_id))
    assert len(entries) == 1
    assert entries[0].debit.tags == (
        project_tag(project_id),
        "stage:project_review",
        "backend:ollama",
    )


def test_an_attempt_records_an_egress_decision_joined_by_attempt_id(database: Database) -> None:
    _attach(database)
    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id)
    attempt_id = record_attempt(
        database, stage_run_id=run_id, stage="draft", result=_result(), unit_id=unit_id
    )
    assert database.egress is not None
    (decision,) = database.egress.decisions(run_id=unit_id)
    assert decision.request.source_ref == attempt_id
    assert decision.verdict.value == "approved"
    assert decision.reason == "target_not_remote"


def test_a_remote_backend_with_no_declared_ceiling_is_denied_fail_closed(
    database: Database,
) -> None:
    """D6: unset `max_data_classification` denies, never assumes public."""
    _attach(database, mode="openai_compatible", remote=True)
    project_id, run_id = _project_and_run(database, backend="openai_compatible")
    unit_id = _unit(database, project_id)
    record_attempt(
        database,
        stage_run_id=run_id,
        stage="draft",
        result=_result(backend="openai_compatible"),
        unit_id=unit_id,
    )
    assert database.egress is not None
    (decision,) = database.egress.decisions(run_id=unit_id)
    assert decision.verdict.value == "denied"
    assert decision.reason == "no_ceiling_declared"
    # A denial pauses nothing on its own — only a bound *budget* ceiling does (D4). Commissioner
    # does not enforce (ADR-0054); the unit is untouched.
    with database.read() as session:
        unit = session.get(UnitRow, unit_id)
        assert unit is not None
        assert unit.state == "drafting"


def test_a_bound_output_ceiling_pauses_the_in_flight_unit(database: Database) -> None:
    """D4's demonstration: exceeding `per_output_token_ceiling` pauses the unit with a reason."""
    _attach(database, budget_overrides={"per_output_token_ceiling": 10})
    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id, state="drafting")
    record_attempt(
        database,
        stage_run_id=run_id,
        stage="draft",
        result=_result(input_tokens=100, output_tokens=50),
        unit_id=unit_id,
    )
    with database.read() as session:
        unit = session.get(UnitRow, unit_id)
        assert unit is not None
        assert unit.state == "paused"
        assert unit.paused_reason is not None and "ceiling was exceeded" in unit.paused_reason


def test_a_bound_ceiling_does_not_pause_a_unit_already_committed(database: Database) -> None:
    """A committed unit is immutable; the pause path must not raise trying to move it."""
    _attach(database, budget_overrides={"per_output_token_ceiling": 10})
    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id, state="committed")
    record_attempt(
        database,
        stage_run_id=run_id,
        stage="revise",
        result=_result(input_tokens=100, output_tokens=50),
        unit_id=unit_id,
    )
    with database.read() as session:
        unit = session.get(UnitRow, unit_id)
        assert unit is not None
        assert unit.state == "committed"


def test_a_debit_currency_mismatch_never_blocks_recording_the_attempt(database: Database) -> None:
    """Governance is best-effort: a bug in budget/egress config must not lose provenance."""
    settings = load_settings(
        cli_overrides={
            "budget": {"per_output_money_ceiling": {"currency": "USD", "nanos": 1_000_000_000}}
        }
    ).settings
    mismatched = ModelPricing(
        identity=BaseModelIdentity(
            provider_kind=ProviderKind.OLLAMA, provider_model_name="gemma4:12b"
        ),
        rates=TokenRates(
            currency="EUR", input_per_million_tokens=Money.from_decimal("EUR", "1.00")
        ),
        source=PricingSource.USER_OVERRIDE,
        observed_at=AT,
    )
    budget = BudgetService(database, settings, PricingCatalog(records=(mismatched,)), clock=_clock)
    egress = EgressService(database, clock=_clock)
    database.attach_governance(budget=budget, egress=egress, settings=settings)

    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id)
    attempt_id = record_attempt(
        database, stage_run_id=run_id, stage="draft", result=_result(), unit_id=unit_id
    )
    assert attempt_id
    with database.read() as session:
        assert session.get(AttemptRow, attempt_id) is not None


def test_governance_is_skipped_when_result_is_none(database: Database) -> None:
    """A deterministic step or a failure with no usage debits nothing and evaluates no egress."""
    _attach(database)
    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id)
    record_attempt(
        database,
        stage_run_id=run_id,
        stage="validate",
        result=None,
        unit_id=unit_id,
        outcome="validation_failed",
    )
    assert database.egress is not None
    assert database.egress.decisions(run_id=unit_id) == ()


def test_every_attempt_has_exactly_one_debit_and_one_decision(database: Database) -> None:
    """Two attempts, two of each — never accidentally shared or skipped."""
    _attach(database)
    project_id, run_id = _project_and_run(database)
    unit_id = _unit(database, project_id)
    record_attempt(database, stage_run_id=run_id, stage="draft", result=_result(), unit_id=unit_id)
    record_attempt(
        database, stage_run_id=run_id, stage="revise", result=_result(), unit_id=unit_id, round_=1
    )
    assert database.budget is not None
    assert database.egress is not None
    with database.read() as session:
        attempts = session.scalars(select(AttemptRow).where(AttemptRow.unit_id == unit_id)).all()
    assert len(attempts) == 2
    assert len(database.budget.ledger().entries(run_id=unit_id)) == 2
    assert len(database.egress.decisions(run_id=unit_id)) == 2
