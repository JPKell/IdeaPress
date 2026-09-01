"""ADR-0043 §1: a grounding requirement with no source is refused at plan time.

M8's demonstration is the whole argument. A brief asked that claims be grounded in *"usage figures,
named programme types, and the specific services that have no other local provider"* and attached
**no sources**. The model supplied a daily footfall count, a workshop attendance figure and a named
2023 service-mapping audit, none of which exist, and every gate passed: 23 checks, an audit scoring
1.00, a critique of `leave_it_alone`, full coverage, committed.

Nothing in that run observed that the requirement asked for evidence and the project had none. This
is the observation, made once, before any unit is written.
"""

from __future__ import annotations

import pytest

from ideapress.domain.requirements import CompiledBy, Requirement, SourceReference
from ideapress.errors import GroundingUnavailable
from ideapress.services.plan import refuse_ungroundable
from ideapress.services.requirements import demands_grounding

_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.1.0")

R006_TEXT = (
    "Every claim must be grounded in usage figures, named programme types, and the specific "
    "services that have no other local provider."
)


def _requirement(
    key: str, text: str, *, blocking: bool = True, grounding: bool = True
) -> Requirement:
    return Requirement(
        key=key,
        text=text,
        blocking=blocking,
        source=SourceReference(document="brief", quote="a quote long enough to ground it"),
        compiled_by=_BY,
        demands_grounding=grounding,
    )


# ------------------------------------------------------------------ marking


@pytest.mark.parametrize(
    "text",
    [
        R006_TEXT,
        "Every figure must be supported by data from the attached report.",
        "Claims should cite their source.",
        "The section must be verifiable against the evidence provided.",
        "Statistics must carry an attribution.",
    ],
)
def test_a_requirement_about_evidence_is_marked(text: str) -> None:
    assert demands_grounding(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "The unit must be explicit about where inference happens.",
        "The article must not exceed 800 words.",
        "The section must be written for a non-technical reader.",
    ],
)
def test_a_requirement_about_content_is_not_marked(text: str) -> None:
    """A conservative vocabulary: over-marking costs a refusal a person can answer, but marking
    every requirement would make the refusal meaningless."""
    assert demands_grounding(text) is False


def test_the_models_own_flag_marks_a_wording_the_vocabulary_misses() -> None:
    """The compiler may recognise a phrasing nobody listed."""
    assert demands_grounding("Assertions must rest on the attached tables.", declared=True) is True


def test_the_vocabulary_marks_it_when_the_model_says_nothing() -> None:
    """The half that matters most: a prompt revision or a smaller model that stops emitting the
    field must not silently disable the refusal. A safety check that quietly stops firing is worse
    than none, because the plan still reports that everything is fine."""
    assert demands_grounding(R006_TEXT, declared=None) is True
    assert demands_grounding(R006_TEXT, declared=False) is True


# ------------------------------------------------------------------ the refusal


def test_a_grounding_requirement_with_no_sources_refuses() -> None:
    with pytest.raises(GroundingUnavailable) as caught:
        refuse_ungroundable([_requirement("R-006", R006_TEXT)], sources=None)
    assert "R-006" in caught.value.message


def test_the_refusal_names_the_requirement_and_the_remedy() -> None:
    """A refusal a person cannot act on is an outage with better manners."""
    with pytest.raises(GroundingUnavailable) as caught:
        refuse_ungroundable([_requirement("R-006", R006_TEXT)], sources={})
    assert "R-006" in caught.value.message
    assert "no sources attached" in caught.value.message
    assert "Attach a source" in caught.value.message
    assert caught.value.details["requirement_keys"] == ["R-006"]


def test_attaching_a_source_lifts_the_refusal() -> None:
    """The other half — the refusal is about the *missing evidence*, not about the requirement."""
    refuse_ungroundable([_requirement("R-006", R006_TEXT)], sources={"report.md": "…"})


def test_a_non_blocking_grounding_requirement_does_not_refuse() -> None:
    """A preference the author can take or leave is not worth stopping a project over."""
    refuse_ungroundable([_requirement("R-009", R006_TEXT, blocking=False)], sources=None)


def test_a_project_with_no_grounding_requirements_is_untouched() -> None:
    ordinary = _requirement("R-001", "The unit must be explicit about scope.", grounding=False)
    refuse_ungroundable([ordinary], sources=None)


def test_every_offending_requirement_is_named_not_just_the_first() -> None:
    """A person fixing one and re-running only to hit the next is a refusal wasting their time."""
    with pytest.raises(GroundingUnavailable) as caught:
        refuse_ungroundable(
            [_requirement("R-006", R006_TEXT), _requirement("R-007", "Claims must cite a source.")],
            sources=None,
        )
    assert caught.value.details["requirement_keys"] == ["R-006", "R-007"]


# ------------------------------------------------ §3: satisfied is not the same as unchecked


def test_a_grounding_requirement_with_no_source_is_labelled_as_unchecked() -> None:
    """ADR-0043 §3. `satisfied` and `satisfied against no source` are different states.

    Reporting them identically is what let M8 commit invented figures under a green report: the
    coverage table said `yes` for a requirement demanding evidence in a project that had none.
    """
    from ideapress.domain.exporters.model import RequirementCoverage

    row = RequirementCoverage(
        key="R-009",
        text=R006_TEXT,
        blocking=False,
        satisfied=True,
        satisfied_by="audit",
        detail="",
        checks="",
        demands_grounding=True,
        checked_against_source=False,
    )
    assert row.satisfied_label == "yes — not checked against any source"


def test_a_grounding_requirement_checked_against_a_source_reads_plainly() -> None:
    """The other half, so the qualifier means something when it appears."""
    from ideapress.domain.exporters.model import RequirementCoverage

    row = RequirementCoverage(
        key="R-006",
        text=R006_TEXT,
        blocking=True,
        satisfied=True,
        satisfied_by="audit",
        detail="",
        checks="",
        demands_grounding=True,
        checked_against_source=True,
    )
    assert row.satisfied_label == "yes"


def test_an_ordinary_requirement_is_never_qualified() -> None:
    """A requirement that never asked for evidence must not acquire a caveat about sources."""
    from ideapress.domain.exporters.model import RequirementCoverage

    row = RequirementCoverage(
        key="R-001",
        text="The unit must be explicit about scope.",
        blocking=True,
        satisfied=True,
        satisfied_by="deterministic_check",
        detail="",
        checks="contains any of: 'scope'",
    )
    assert row.satisfied_label == "yes"


def test_an_unsatisfied_requirement_says_no_whatever_else_is_true() -> None:
    from ideapress.domain.exporters.model import RequirementCoverage

    row = RequirementCoverage(
        key="R-006",
        text=R006_TEXT,
        blocking=True,
        satisfied=False,
        satisfied_by="audit",
        detail="",
        checks="",
        demands_grounding=True,
        checked_against_source=False,
    )
    assert row.satisfied_label == "no"
