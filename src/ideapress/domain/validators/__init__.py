"""ideapress.domain.validators — the seven families of workflows §4.

Structural, length, format, content constraints, reference integrity, consistency, safety. Each is
a pure function of the text and the context Python assembled; none consults a model.

:data:`DEFAULT_VALIDATORS` is the order they run in, fixed so a report is stable and a diff between
two rounds means something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ideapress.domain.validators.consistency import ConsistencyValidator
from ideapress.domain.validators.content import ContentConstraintValidator
from ideapress.domain.validators.reference import ReferenceIntegrityValidator
from ideapress.domain.validators.safety import SafetyValidator

from ideapress.domain.validators.formatting import FormatValidator
from ideapress.domain.validators.length import LengthValidator
from ideapress.domain.validators.structural import StructuralValidator

if TYPE_CHECKING:
    from ideapress.domain.validation import Validator

__all__ = [
    "DEFAULT_VALIDATORS",
    "ConsistencyValidator",
    "ContentConstraintValidator",
    "FormatValidator",
    "LengthValidator",
    "ReferenceIntegrityValidator",
    "SafetyValidator",
    "StructuralValidator",
]

DEFAULT_VALIDATORS: Final[tuple[Validator, ...]] = (
    StructuralValidator(),
    LengthValidator(),
    FormatValidator(),
    ContentConstraintValidator(),
    ReferenceIntegrityValidator(),
    ConsistencyValidator(),
    SafetyValidator(),
)
"""All seven, in workflows §4's own order."""
