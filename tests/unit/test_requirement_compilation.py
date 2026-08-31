"""Risk T6: the compiler may not invent requirements the material does not support.

The prompt for this run is explicit that a test asserting "the list is short" proves nothing, so
none of these do that. What they assert instead:

* **Provenance resolves.** Every accepted requirement quotes text that is a verbatim span of a
  document the project actually holds. A fabricated requirement must fabricate its evidence, and
  fabricated evidence is not a substring.
* **Benign material yields nothing.** A brief that deliberately states no constraints is fed to a
  compiler that returns three plausible-sounding requirements anyway, and every one is refused —
  because none of them can be quoted.
* **The residual case is named, not hidden.** A model that quotes real text and attaches an
  unrelated requirement passes the check. There is a test asserting exactly that, so the limit of
  the mechanism is recorded in the suite rather than only in a docstring.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import ValidationError

from ideapress.config import load_settings
from ideapress.domain.requirements import (
    MIN_QUOTE_CHARS,
    CompiledBy,
    RequirementCheck,
    SourceReference,
    ground_requirement,
)
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.inference import InferenceGateway
from ideapress.services.requirements import compile_requirements, parse_candidates

if TYPE_CHECKING:
    from collections.abc import Sequence

# A brief with real, quotable constraints.
CONSTRAINED_BRIEF = """
# Local inference for writers

Audience: working writers with no machine-learning background.

The article must state that inference runs entirely on the reader's own machine and that no
document content is uploaded anywhere. It must never promise that a local model is more accurate
than a hosted one. Keep it under 1200 words.
""".strip()

# A brief that is a real piece of writing and states no constraint on a finished work at all.
# This is the fixture the run's prompt asks for: material that deliberately implies nothing.
BENIGN_BRIEF = """
# Saturday

I walked to the market before it got warm. The stalls were mostly the usual ones. I bought
tomatoes, a loaf, and a bunch of flowers that turned out to be for someone else's order, so I
gave them back. On the way home a dog followed me for two streets and then lost interest.
""".strip()

COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.0.0")


def _documents(brief: str = CONSTRAINED_BRIEF) -> dict[str, str]:
    return {"brief": brief}


def _scripted_gateway(answer: str) -> InferenceGateway:
    """A gateway whose backend returns exactly ``answer``: the model, entirely under our control."""
    from modelrack.testing import FakeGeneration, FakeScript

    settings = load_settings().settings
    script = FakeScript(
        models=default_fake_script().models,
        capabilities=default_fake_script().capabilities,
        generations=(FakeGeneration(text=answer),),
        repeat_final_generation=True,
    )
    return InferenceGateway(
        backend=FakeBackend(script=script, seed=1),
        bindings=settings.models.stages,
        execution=settings.execution,
    )


def _answer(requirements: Sequence[dict[str, Any]]) -> str:
    return json.dumps({"requirements": list(requirements)})


def test_a_grounded_requirement_is_accepted() -> None:
    requirement = ground_requirement(
        key="R-001",
        text="The article must state that inference runs on the reader's own machine.",
        blocking=True,
        source=SourceReference(
            document="brief",
            quote="inference runs entirely on the reader's own machine",
            anchor="privacy",
        ),
        compiled_by=COMPILED_BY,
        checks=[RequirementCheck(kind="must_contain_any", values=("own machine", "locally"))],
        documents=_documents(),
    )
    assert requirement.key == "R-001"
    assert requirement.is_mechanically_checkable
    assert requirement.source.label == "brief#privacy"


def test_a_quote_that_is_not_in_the_material_is_refused() -> None:
    """The fabrication marker. This is the whole mechanism."""
    with pytest.raises(ValidationError) as caught:
        ground_requirement(
            key="R-001",
            text="The article must include three customer testimonials.",
            blocking=True,
            source=SourceReference(
                document="brief", quote="the article must include three customer testimonials"
            ),
            compiled_by=COMPILED_BY,
            documents=_documents(),
        )
    assert "does not appear" in caught.value.message
    assert "R-001" in caught.value.message


def test_a_citation_of_a_document_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ground_requirement(
            key="R-002",
            text="The article must follow the corporate style guide precisely.",
            blocking=True,
            source=SourceReference(document="style-guide.md", quote="x" * 40),
            compiled_by=COMPILED_BY,
            documents=_documents(),
        )
    assert "style-guide.md" in caught.value.message
    assert "brief" in caught.value.message


def test_a_quote_too_short_to_be_evidence_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ground_requirement(
            key="R-003",
            text="The article must be good and useful to its readers.",
            blocking=True,
            source=SourceReference(document="brief", quote="Audience"),
            compiled_by=COMPILED_BY,
            documents=_documents(),
        )
    assert str(MIN_QUOTE_CHARS) in caught.value.message


def test_a_statement_nobody_could_evaluate_is_refused() -> None:
    with pytest.raises(ValidationError):
        ground_requirement(
            key="R-004",
            text="Good.",
            blocking=True,
            source=SourceReference(document="brief", quote="working writers with no machine"),
            compiled_by=COMPILED_BY,
            documents=_documents(),
        )


def test_a_quote_matches_across_a_line_wrap_the_model_did_not_reproduce() -> None:
    """Normalisation must not be so strict that honest quoting fails; the brief is hard-wrapped."""
    requirement = ground_requirement(
        key="R-005",
        text="The article must not claim a local model is more accurate than a hosted one.",
        blocking=True,
        source=SourceReference(
            document="brief",
            # In the brief this spans a newline.
            quote="never promise that a local model is more accurate than a hosted one",
        ),
        compiled_by=COMPILED_BY,
        documents=_documents(),
    )
    assert requirement.blocking


def test_benign_material_yields_no_requirement_however_confident_the_model_is() -> None:
    """The fixture whose brief deliberately implies nothing, against a model that invents anyway.

    Every candidate here is plausible, well-formed, and confidently asserted. Not one can be quoted
    from a brief about walking to the market, so not one survives.
    """
    gateway = _scripted_gateway(
        _answer(
            [
                {
                    "text": "The piece must include a clear call to action for the reader.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "the piece must include a clear call to action",
                },
                {
                    "text": "The piece must be written in an accessible, friendly register.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "written in an accessible, friendly register",
                },
                {
                    "text": "The piece must be between 800 and 1200 words.",
                    "blocking": False,
                    "source_document": "brief",
                    "source_quote": "between 800 and 1200 words",
                    "checks": [{"kind": "min_words", "threshold": 800}],
                },
            ]
        )
    )
    result = compile_requirements(gateway, project_id="01PROJECT", brief=BENIGN_BRIEF)
    assert result.requirements == (), "benign material produced invented requirements"
    assert len(result.rejected) == 3, "and the rejections must be visible, not silently dropped"
    for rejected in result.rejected:
        assert "does not appear" in rejected.reason


def test_every_accepted_requirement_resolves_into_the_author_material() -> None:
    """Provenance, asserted as a property of the result rather than of one fixture."""
    gateway = _scripted_gateway(
        _answer(
            [
                {
                    "text": "The article must state that inference runs on the reader's machine.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "inference runs entirely on the reader's own machine",
                    "checks": [{"kind": "must_contain_any", "values": ["own machine", "locally"]}],
                },
                {
                    "text": "The article must not claim local models are more accurate.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "never promise that a local model is more accurate",
                },
                {
                    "text": "The article must include a section on GPU pricing tiers.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "must include a section on GPU pricing tiers",
                },
            ]
        )
    )
    result = compile_requirements(gateway, project_id="01PROJECT", brief=CONSTRAINED_BRIEF)

    from ideapress.domain.requirements import normalise_for_matching

    material = normalise_for_matching(CONSTRAINED_BRIEF)
    assert result.requirements, "grounded requirements were dropped"
    for requirement in result.requirements:
        assert normalise_for_matching(requirement.source.quote) in material
        assert requirement.compiled_by.prompt_id == "stages.requirements.compile"
        assert requirement.compiled_by.prompt_sha256
    assert len(result.rejected) == 1, "the invented one is refused"
    assert "GPU pricing" in result.rejected[0].text


def test_keys_are_generated_by_ideapress_not_by_the_model() -> None:
    """Risk S2's rule applied to identifiers: nothing the model produced becomes one."""
    gateway = _scripted_gateway(
        _answer(
            [
                {
                    "text": "The article must state that no document content is uploaded.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "no document content is uploaded anywhere",
                    "requirement_key": "R-999-INJECTED",
                }
            ]
        )
    )
    result = compile_requirements(gateway, project_id="01PROJECT", brief=CONSTRAINED_BRIEF)
    assert [r.key for r in result.requirements] == ["R-001"]


def test_a_model_supplied_regex_check_cannot_exist() -> None:
    """Workflows §11: a model may not cause code execution, and a pattern is a small program."""
    with pytest.raises(ValidationError) as caught:
        RequirementCheck(kind="must_match", values=("(a+)+$",))  # type: ignore[arg-type]  # the point
    assert "not a check kind" in caught.value.message


def test_a_check_that_checks_nothing_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        RequirementCheck(kind="must_contain_any", values=())
    assert "guarantees nothing" in caught.value.message


def test_a_malformed_check_is_dropped_and_the_requirement_survives_flagged() -> None:
    """A bad check must not lose a grounded requirement — but it must not look mechanical either."""
    gateway = _scripted_gateway(
        _answer(
            [
                {
                    "text": "The article must state that no document content is uploaded.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "no document content is uploaded anywhere",
                    "checks": [{"kind": "must_match", "values": ["^.*$"]}],
                }
            ]
        )
    )
    result = compile_requirements(gateway, project_id="01PROJECT", brief=CONSTRAINED_BRIEF)
    assert len(result.requirements) == 1
    requirement = result.requirements[0]
    assert not requirement.is_mechanically_checkable
    assert "no deterministic check" in requirement.describe_checks()


def test_malformed_json_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ValidationError):
        parse_candidates("I have compiled the requirements for you. They are: be good.")
    with pytest.raises(ValidationError):
        parse_candidates('{"reqs": []}')
    with pytest.raises(ValidationError):
        parse_candidates('{"requirements": "none"}')


def test_a_fenced_json_block_is_tolerated() -> None:
    """Tolerance for formatting is not tolerance for content."""
    assert parse_candidates('```json\n{"requirements": []}\n```') == []


def test_an_empty_requirement_list_parses_and_is_not_an_error_here() -> None:
    """Emptiness is refused by the *plan gate*, not by the parser — the parser reports honestly."""
    gateway = _scripted_gateway(_answer([]))
    result = compile_requirements(gateway, project_id="01PROJECT", brief=BENIGN_BRIEF)
    assert result.requirements == ()
    assert result.rejected == ()


def test_the_limit_of_the_mechanism_a_real_quote_with_an_unrelated_requirement() -> None:
    """The residual case, recorded in the suite rather than only in a docstring.

    A model that quotes real text and attaches an unrelated requirement to it **passes** the
    grounding check. No deterministic check settles that — it is a semantic judgement about
    support. The mitigation is that the quote travels with the requirement into every view and
    every export, so the reviewer reads the claim and the evidence side by side; this test exists
    so nobody reads T6 as closed.
    """
    gateway = _scripted_gateway(
        _answer(
            [
                {
                    "text": "The article must be published on a Tuesday.",
                    "blocking": True,
                    "source_document": "brief",
                    "source_quote": "working writers with no machine-learning background",
                }
            ]
        )
    )
    result = compile_requirements(gateway, project_id="01PROJECT", brief=CONSTRAINED_BRIEF)
    assert len(result.requirements) == 1, "documented limit: this is accepted"
    accepted = result.requirements[0]
    assert accepted.source.quote in CONSTRAINED_BRIEF
    assert "Tuesday" in accepted.text, "the reviewer sees the claim and the quote together"
