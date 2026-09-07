"""ideapress.services.pricing — reading ``[pricing] file`` into ``baseaicore.ModelPricing``.

ADR-0030 rule 1 is that **cost is derived and never stored**, which only works if the price the
derivation used can be found again. This module is where IdeaPress finds it: an optional
``[pricing] file`` names a JSON catalogue in the format ADR-0072 defines, and every debit costs an
attempt's usage against the record that catalogue holds for the model that answered.

This is a **transcription of ``promptcadence.services.pricing``** (row J1 D3), the suite's first
reader of this format, trimmed to what IdeaPress needs: PromptCadence indexes records by *tier*
because a trajectory may run under any of several configured tiers; IdeaPress has exactly one
configured backend at a time; so this module holds one flat catalogue rather than a tier map, and
carries none of the tier-facing estimation surface (``claiming()``, pre-flight worst-case pricing) —
a stage attempt is recorded *after* the model has answered, never estimated ahead of one (workflows
§6). Everything about the file format, and the three rules that are load-bearing rather than
stylistic, is unchanged from that module's own docstring:

* **Rates are decimal strings, never floats.** ``"2.50"`` goes through
  :meth:`baseaicore.Money.from_decimal` to whole nanos.
* **An omitted rate is UNSUPPORTED, not zero.** A price list that states no cache-read rate cannot
  price a call that read from cache.
* **A record is an observation, not a fact about a model.** Several records may name the same
  weights; :meth:`PricingCatalog.for_model` resolves them by the window they claim and, among those
  still claiming the instant, by which was observed most recently.

**No network, ever.** A pricing file is read from disk at startup and never fetched.

If a second application ever needs this reader as well as this format, it graduates into a shared
package under its own row (ADR-0072 §"Revisit when") — this row's handoff names that as a finding,
not work done here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from baseaicore import (
    UNSUPPORTED,
    ModelIdentity,
    ModelPricing,
    Money,
    PricingSource,
    ProviderKind,
    TokenRates,
    ValidationError,
    from_rfc3339,
    normalize_digest,
)

from ideapress.config import ConfigurationError

if TYPE_CHECKING:
    from datetime import datetime

    from ideapress.config import Settings

__all__ = ["PricingCatalog", "load_pricing_records"]

_RATE_FIELDS: Final = (
    "input_per_million_tokens",
    "output_per_million_tokens",
    "cache_write_per_million_tokens",
    "cache_read_per_million_tokens",
)


def _refuse(message: str, **details: Any) -> ConfigurationError:
    """Build the one refusal shape this module raises, naming the file and the field."""
    return ConfigurationError(message, details=details)


def _rates_of(document: Mapping[str, Any], *, path: Path, index: int) -> TokenRates:
    """Read one record's ``rates`` block, keeping "not stated" distinct from "free"."""
    block = document.get("rates")
    if not isinstance(block, Mapping):
        message = f"{path}: record {index} has no 'rates' object"
        raise _refuse(message, file=str(path), record=index, field="rates")
    currency = block.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        message = f"{path}: record {index} states no 'rates.currency'"
        raise _refuse(message, file=str(path), record=index, field="rates.currency")
    stated: dict[str, Money] = {}
    for name in _RATE_FIELDS:
        raw = block.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            message = (
                f"{path}: record {index} states rates.{name}={raw!r}; a rate is a decimal "
                '*string* such as "2.50". A JSON number is a float, and a price that arrived as '
                "a float has already lost the value this suite's integer arithmetic protects."
            )
            raise _refuse(message, file=str(path), record=index, field=f"rates.{name}")
        try:
            stated[name] = Money.from_decimal(currency, raw)
        except ValidationError as exc:
            message = f"{path}: record {index} states an unreadable rates.{name}: {exc.message}"
            raise _refuse(message, file=str(path), record=index, field=f"rates.{name}") from exc
    try:
        return TokenRates(
            currency=currency,
            input_per_million_tokens=stated.get("input_per_million_tokens", UNSUPPORTED),
            output_per_million_tokens=stated.get("output_per_million_tokens", UNSUPPORTED),
            cache_write_per_million_tokens=stated.get(
                "cache_write_per_million_tokens", UNSUPPORTED
            ),
            cache_read_per_million_tokens=stated.get("cache_read_per_million_tokens", UNSUPPORTED),
        )
    except ValidationError as exc:
        message = f"{path}: record {index} has unusable rates: {exc.message}"
        raise _refuse(message, file=str(path), record=index, field="rates") from exc


def _instant(document: Mapping[str, Any], name: str, *, path: Path, index: int) -> datetime | None:
    """Read one RFC 3339 field, refusing a value that is present but unreadable."""
    raw = document.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        message = f"{path}: record {index} states {name}={raw!r}; expected an RFC 3339 string"
        raise _refuse(message, file=str(path), record=index, field=name)
    try:
        return from_rfc3339(raw)
    except ValidationError as exc:
        message = f"{path}: record {index} states an unreadable {name}: {exc.message}"
        raise _refuse(message, file=str(path), record=index, field=name) from exc


def _record_of(document: Mapping[str, Any], *, path: Path, index: int) -> ModelPricing:
    """Build one :class:`~baseaicore.ModelPricing` from one record object."""
    kind_raw = str(document.get("provider_kind"))
    try:
        kind = ProviderKind(kind_raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in ProviderKind))
        message = (
            f"{path}: record {index} names provider_kind={kind_raw!r}, which is not a provider "
            f"this suite knows ({known})"
        )
        raise _refuse(message, file=str(path), record=index, field="provider_kind") from exc
    name = document.get("provider_model_name")
    if not isinstance(name, str) or not name.strip():
        message = f"{path}: record {index} names no provider_model_name"
        raise _refuse(message, file=str(path), record=index, field="provider_model_name")
    source_raw = str(document.get("source"))
    try:
        source = PricingSource(source_raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in PricingSource))
        message = (
            f"{path}: record {index} names source={source_raw!r}; a price without stated "
            f"provenance cannot be weighed (ADR-0030). Expected one of: {known}"
        )
        raise _refuse(message, file=str(path), record=index, field="source") from exc
    observed_at = _instant(document, "observed_at", path=path, index=index)
    if observed_at is None:
        message = (
            f"{path}: record {index} states no observed_at; a price with no date is a price "
            "nobody can tell has gone stale"
        )
        raise _refuse(message, file=str(path), record=index, field="observed_at")
    digest_raw = document.get("artifact_digest")
    try:
        digest = normalize_digest(digest_raw) if isinstance(digest_raw, str) else None
        identity = ModelIdentity(
            provider_kind=kind, provider_model_name=name, artifact_digest=digest
        )
        return ModelPricing(
            identity=identity,
            rates=_rates_of(document, path=path, index=index),
            source=source,
            observed_at=observed_at,
            effective_from=_instant(document, "effective_from", path=path, index=index),
            effective_until=_instant(document, "effective_until", path=path, index=index),
            price_tier=document.get("price_tier") or None,
            region=document.get("region") or None,
        )
    except ValidationError as exc:
        message = f"{path}: record {index} is not a usable price observation: {exc.message}"
        raise _refuse(message, file=str(path), record=index) from exc


def load_pricing_records(path: Path) -> tuple[ModelPricing, ...]:
    """Read one pricing file into price observations.

    Args:
        path: The ``[pricing] file`` to read.

    Returns:
        Every record in the file, in file order. An empty ``records`` array is legitimate and
        loads to an empty tuple.

    Raises:
        ConfigurationError: If the file is missing, is not readable, is not JSON, is not an object
            with a ``records`` array, or holds a record this module cannot turn into a
            :class:`~baseaicore.ModelPricing`. Every one of these is a startup refusal by design
            (ADR-0072 §7): a price list discovered to be unreadable mid-run would leave real
            attempts that cannot be costed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"pricing file {path} cannot be read: {exc.strerror or exc}"
        raise _refuse(message, file=str(path)) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        message = f"pricing file {path} is not valid JSON: {exc}"
        raise _refuse(message, file=str(path)) from exc
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        message = (
            f"pricing file {path} must be a JSON object with a 'records' array of "
            "ModelPricing observations"
        )
        raise _refuse(message, file=str(path), field="records")
    records = []
    for index, entry in enumerate(document["records"]):
        if not isinstance(entry, dict):
            message = f"{path}: record {index} is not an object"
            raise _refuse(message, file=str(path), record=index)
        records.append(_record_of(entry, path=path, index=index))
    return tuple(records)


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    """Every price observation this installation knows, loaded once at startup.

    Built by :meth:`from_settings` before anything runs, so an unreadable price list is a refusal
    to start rather than a run that spends attempts nobody can account for. An installation naming
    no ``[pricing] file`` holds no records at all, and :meth:`for_model` returning ``None`` is the
    correct answer for it, not a gap: a local model's cost is ``UNSUPPORTED``, never ``$0.00``
    (ADR-0016).

    Attributes:
        records: Every observation, in file order.
    """

    records: tuple[ModelPricing, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> PricingCatalog:
        """Load the configured ``[pricing] file``, if one is named.

        Args:
            settings: The validated configuration.

        Returns:
            The catalogue. Empty when ``settings.pricing.file`` is blank.

        Raises:
            ConfigurationError: The named file cannot be read or holds an unusable record.
        """
        path = settings.pricing.file.strip()
        if not path:
            return cls()
        return cls(records=load_pricing_records(Path(path).expanduser()))

    def for_model(self, *, canonical_id: str, at: datetime) -> ModelPricing | None:
        """Return the price observation to cost a call by ``canonical_id`` at ``at``.

        Matching is on the identity's two stable halves — the provider kind and the provider's own
        model name — with the artifact digest as an *optional* narrowing: a record that states a
        digest matches only that digest, and a record that states none matches the weights under
        any digest. A price list is usually written against a provider's product name, which
        survives a retag; pinning every record to a digest would make a routine retag silently
        unpriceable, while ignoring a digest a record *did* state would price one set of weights at
        another's rates.

        Args:
            canonical_id: ``provider/name`` or ``provider/name@sha256:…`` (ADR-0008), from
                :attr:`ideapress.domain.inference.ModelIdentity.canonical_id`.
            at: The instant to price at — when the attempt happened, so re-costing history later
                finds the same record.

        Returns:
            The most recently *observed* record that claims ``at``, or ``None`` when the catalogue
            holds no record for these weights. ``None`` is not free: the caller records the usage
            unpriced and says why.
        """
        candidates = [
            record
            for record in self.records
            if _matches(record, canonical_id) and _claims(record, at)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.observed_at)


def _split(canonical_id: str) -> tuple[str, str, str | None]:
    """Split ``provider/name`` or ``provider/name@sha256:…`` without parsing the name itself."""
    prefix, _, remainder = canonical_id.partition("/")
    name, at, digest = remainder.rpartition("@")
    if not at or not digest.startswith("sha256:"):
        return prefix, remainder, None
    return prefix, name, digest


def _matches(record: ModelPricing, canonical_id: str) -> bool:
    """Whether one record's identity names the weights ``canonical_id`` names."""
    kind, name, digest = _split(canonical_id)
    if record.identity.provider_kind.value != kind or record.identity.provider_model_name != name:
        return False
    return record.identity.artifact_digest in (None, digest)


def _claims(record: ModelPricing, at: datetime) -> bool:
    """Whether one record says it applies at ``at``. An unstated bound is not a bound."""
    if record.effective_from is not None and at < record.effective_from:
        return False
    return not (record.effective_until is not None and at > record.effective_until)
