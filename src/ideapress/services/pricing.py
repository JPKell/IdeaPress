"""ideapress.services.pricing — where the ``[pricing] file`` is, and what a broken one means.

ADR-0030 rule 1 is that **cost is derived and never stored**, which only works if the price the
derivation used can be found again. An optional ``[pricing] file`` names a JSON catalogue in the
format ADR-0072 defines, and every debit costs an attempt's usage against the record that
catalogue holds for the model that answered.

**The reader itself is no longer here.** Row J1 transcribed it from
``promptcadence.services.pricing``; row K4 moved it into ``loadledger.pricing``, the suite's one
ADR-0072 reader, because ``pricing_hash`` is the join between a stored usage and the price it was
costed under and two readers of one format would eventually file two prices under one hash
(ADR-0110). The format, its refusals and the matching rules all live there and are documented
there.

What stays here is the application's edge, and only that:

* **Which key names the file.** ``[pricing] file`` is IdeaPress's configuration, and a catalogue
  is loaded once at startup so an unreadable one is a refusal to start rather than a run that
  spends attempts nobody can account for (ADR-0072 §7).
* **How a broken file is reported.** :class:`~loadledger.PricingFileError` is re-raised as this
  application's :class:`~ideapress.config.ConfigurationError`, keeping the package's message and
  its ``details`` (the file, the record index, the field) — because to an operator this is a
  configuration mistake, and it should read like one.
* **The container.** IdeaPress has one configured backend at a time, so its catalogue is one flat
  tuple rather than PromptCadence's per-tier map, and it carries no pre-flight estimation surface:
  a stage attempt is recorded *after* the model has answered, never sized ahead of one
  (workflows §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loadledger import PricingFileError
from loadledger.pricing import load_pricing_records as _load_pricing_records
from loadledger.pricing import price_for_model

from ideapress.config import ConfigurationError

if TYPE_CHECKING:
    from datetime import datetime

    from baseaicore import ModelPricing

    from ideapress.config import Settings

__all__ = ["PricingCatalog", "load_pricing_records"]


def load_pricing_records(path: Path) -> tuple[ModelPricing, ...]:
    """Read one ADR-0072 pricing file, reporting a broken one as a configuration mistake.

    Args:
        path: The ``[pricing] file`` to read.

    Returns:
        Every record in the file, in file order. An empty ``records`` array is legitimate and
        loads to an empty tuple.

    Raises:
        ConfigurationError: If the file is missing, is not readable, is not JSON, is not an object
            with a ``records`` array, or holds a record ADR-0072's rules cannot turn into a
            :class:`~baseaicore.ModelPricing`. The package's message and ``details`` are kept
            whole; only the exception type changes, because a price list an operator wrote is part
            of this application's configuration and a refusal naming a different vocabulary would
            send them looking in the wrong place.
    """
    try:
        return _load_pricing_records(path)
    except PricingFileError as exc:
        raise ConfigurationError(exc.message, details=dict(exc.details)) from exc


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    """Every price observation this installation knows, loaded once at startup.

    Built by :meth:`from_settings` before anything runs. An installation naming no
    ``[pricing] file`` holds no records at all, and :meth:`for_model` returning ``None`` is the
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

        Delegates the matching rules to :func:`loadledger.pricing.price_for_model`, where they are
        documented: provider kind and model name must agree, a record stating a digest matches only
        that digest, one stating none matches those weights under any digest, and among the records
        claiming the instant the most recently observed wins.

        Args:
            canonical_id: ``provider/name`` or ``provider/name@sha256:…`` (ADR-0008), from
                :attr:`ideapress.domain.inference.ModelIdentity.canonical_id`.
            at: The instant to price at — when the attempt happened, so re-costing history later
                finds the same record.

        Returns:
            The record to cost against, or ``None`` when the catalogue holds none for these
            weights. ``None`` is not free: the caller records the usage unpriced and says why.
        """
        return price_for_model(self.records, canonical_id=canonical_id, at=at)
