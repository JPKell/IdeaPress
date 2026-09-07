"""``ideapress.services.pricing`` — the ``[pricing] file`` reader (row J1 D3, ADR-0072)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import (
    ModelIdentity,
    ModelPricing,
    PricingSource,
    ProviderKind,
    TokenRates,
    is_supported,
)

from ideapress.config import ConfigurationError, load_settings
from ideapress.services.pricing import PricingCatalog, load_pricing_records

if TYPE_CHECKING:
    from pathlib import Path

RECORD: dict[str, Any] = {
    "provider_kind": "ollama",
    "provider_model_name": "gemma4:12b",
    "artifact_digest": None,
    "source": "user_override",
    "observed_at": "2026-09-01T00:00:00Z",
    "effective_from": None,
    "effective_until": None,
    "price_tier": None,
    "region": None,
    "rates": {
        "currency": "USD",
        "input_per_million_tokens": "2.50",
        "output_per_million_tokens": "10.00",
    },
}


def _write(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"records": records}), encoding="utf-8")
    return path


def test_an_empty_records_array_loads_to_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path, [])
    assert load_pricing_records(path) == ()


def test_a_complete_record_loads_with_decimal_rates_as_whole_nanos(tmp_path: Path) -> None:
    path = _write(tmp_path, [RECORD])
    (record,) = load_pricing_records(path)
    assert record.identity.provider_kind is ProviderKind.OLLAMA
    assert record.identity.provider_model_name == "gemma4:12b"
    input_rate = record.rates.input_per_million_tokens
    output_rate = record.rates.output_per_million_tokens
    assert is_supported(input_rate) and input_rate.nanos == 2_500_000_000
    assert is_supported(output_rate) and output_rate.nanos == 10_000_000_000
    assert record.source is PricingSource.USER_OVERRIDE


def test_an_omitted_rate_is_unsupported_never_zero(tmp_path: Path) -> None:
    """ADR-0016: a price list that states no cache-read rate cannot price cache reads as free."""
    path = _write(tmp_path, [RECORD])
    (record,) = load_pricing_records(path)
    assert not is_supported(record.rates.cache_read_per_million_tokens)
    assert not is_supported(record.rates.cache_write_per_million_tokens)


def test_a_json_number_rate_is_refused_not_coerced(tmp_path: Path) -> None:
    """A float has already lost the value the whole-nanos arithmetic protects."""
    bad = dict(RECORD, rates={**RECORD["rates"], "input_per_million_tokens": 2.5})
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="decimal"):
        load_pricing_records(path)


def test_a_record_with_no_observed_at_is_refused(tmp_path: Path) -> None:
    bad = {k: v for k, v in RECORD.items() if k != "observed_at"}
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="observed_at"):
        load_pricing_records(path)


def test_a_record_with_no_rates_object_is_refused(tmp_path: Path) -> None:
    bad = {k: v for k, v in RECORD.items() if k != "rates"}
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="rates"):
        load_pricing_records(path)


def test_a_rates_block_with_no_currency_is_refused(tmp_path: Path) -> None:
    bad = dict(RECORD, rates={k: v for k, v in RECORD["rates"].items() if k != "currency"})
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="currency"):
        load_pricing_records(path)


def test_an_unreadable_decimal_rate_is_refused(tmp_path: Path) -> None:
    bad = dict(RECORD, rates={**RECORD["rates"], "input_per_million_tokens": "not a number"})
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="unreadable"):
        load_pricing_records(path)


def test_an_unreadable_effective_from_is_refused(tmp_path: Path) -> None:
    bad = dict(RECORD, effective_from="not a date")
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="unreadable"):
        load_pricing_records(path)


def test_an_unknown_provider_kind_is_refused(tmp_path: Path) -> None:
    bad = dict(RECORD, provider_kind="not_a_provider")
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="provider_kind"):
        load_pricing_records(path)


def test_no_provider_model_name_is_refused(tmp_path: Path) -> None:
    bad = dict(RECORD, provider_model_name="")
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="provider_model_name"):
        load_pricing_records(path)


def test_an_unknown_source_is_refused(tmp_path: Path) -> None:
    bad = dict(RECORD, source="not_a_source")
    path = _write(tmp_path, [bad])
    with pytest.raises(ConfigurationError, match="source"):
        load_pricing_records(path)


def test_a_non_object_record_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, ["not an object"])  # type: ignore[list-item]
    with pytest.raises(ConfigurationError, match="not an object"):
        load_pricing_records(path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot be read"):
        load_pricing_records(tmp_path / "nope.json")


def test_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_pricing_records(path)


def test_no_records_array_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"nope": []}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="records"):
        load_pricing_records(path)


def test_from_settings_is_empty_when_no_file_is_named() -> None:
    settings = load_settings().settings
    catalog = PricingCatalog.from_settings(settings)
    assert catalog.records == ()


def test_from_settings_reads_the_named_file(tmp_path: Path) -> None:
    path = _write(tmp_path, [RECORD])
    settings = load_settings(cli_overrides={"pricing": {"file": str(path)}}).settings
    catalog = PricingCatalog.from_settings(settings)
    assert len(catalog.records) == 1


def _pricing(**overrides: object) -> ModelPricing:
    base = dict(
        identity=ModelIdentity(provider_kind=ProviderKind.OLLAMA, provider_model_name="gemma4:12b"),
        rates=TokenRates(currency="USD"),
        source=PricingSource.USER_OVERRIDE,
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return ModelPricing(**base)  # type: ignore[arg-type]


def test_for_model_matches_by_provider_and_name_when_no_digest_is_stated() -> None:
    catalog = PricingCatalog(records=(_pricing(),))
    at = datetime(2026, 9, 2, tzinfo=UTC)
    assert catalog.for_model(canonical_id="ollama/gemma4:12b", at=at) is not None
    assert catalog.for_model(canonical_id="ollama/gemma4:12b@sha256:whatever", at=at) is not None


_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def test_for_model_with_a_stated_digest_matches_only_that_digest() -> None:
    identity = ModelIdentity(
        provider_kind=ProviderKind.OLLAMA,
        provider_model_name="gemma4:12b",
        artifact_digest=_DIGEST_A,
    )
    catalog = PricingCatalog(records=(_pricing(identity=identity),))
    at = datetime(2026, 9, 2, tzinfo=UTC)
    assert catalog.for_model(canonical_id=f"ollama/gemma4:12b@{_DIGEST_A}", at=at) is not None
    assert catalog.for_model(canonical_id=f"ollama/gemma4:12b@{_DIGEST_B}", at=at) is None
    assert catalog.for_model(canonical_id="ollama/gemma4:12b", at=at) is None


def test_for_model_returns_none_when_nothing_covers_the_weights() -> None:
    catalog = PricingCatalog(records=(_pricing(),))
    at = datetime(2026, 9, 2, tzinfo=UTC)
    assert catalog.for_model(canonical_id="openai_compatible/other-model", at=at) is None


def test_for_model_excludes_a_record_outside_its_effective_window() -> None:
    record = _pricing(effective_until=datetime(2026, 9, 1, 12, tzinfo=UTC))
    catalog = PricingCatalog(records=(record,))
    assert (
        catalog.for_model(canonical_id="ollama/gemma4:12b", at=datetime(2026, 9, 2, tzinfo=UTC))
        is None
    )
    assert (
        catalog.for_model(canonical_id="ollama/gemma4:12b", at=datetime(2026, 9, 1, tzinfo=UTC))
        is not None
    )


def test_for_model_prefers_the_most_recently_observed_record() -> None:
    older = _pricing(observed_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _pricing(observed_at=datetime(2026, 9, 1, tzinfo=UTC))
    catalog = PricingCatalog(records=(older, newer))
    found = catalog.for_model(canonical_id="ollama/gemma4:12b", at=datetime(2026, 9, 2, tzinfo=UTC))
    assert found is newer
