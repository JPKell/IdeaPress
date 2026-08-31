"""Every validator family in workflows §4: pass, fail, boundary, malformed input, unicode.

No model appears anywhere in this file, which is the point of the whole family: five stages in
workflows §2 involve no model, and these are the checks behind the one that decides most often.

The blocking/advisory split is tested as carefully as the checks themselves, because risk T4 —
validators too strict, blocking legitimate content — is a real failure mode and "make everything
blocking, it's safer" is how a suite arrives at it.
"""

from __future__ import annotations

import pytest

from ideapress.domain.plan import PlanUnit
from ideapress.domain.requirements import (
    CompiledBy,
    Requirement,
    RequirementCheck,
    SourceReference,
)
from ideapress.domain.validation import ValidationContext, run_validators
from ideapress.domain.validators import (
    DEFAULT_VALIDATORS,
    ConsistencyValidator,
    ContentConstraintValidator,
    FormatValidator,
    LengthValidator,
    ReferenceIntegrityValidator,
    SafetyValidator,
    StructuralValidator,
)

COMPILED_BY = CompiledBy(prompt_id="stages.requirements.compile", version="1.0.0")

GOOD_UNIT = """# Where the work happens

Inference runs on your own machine. Nothing you write is uploaded anywhere, and the model never
sees a network. That is the whole point of running it locally: the work stays where you made it.

- No account is required.
- No document leaves the disk.

The trade-off is that you provide the hardware.
"""


def _context(text: str, **kwargs: object) -> ValidationContext:
    return ValidationContext(text=text, **kwargs)  # type: ignore[arg-type]  # a fixed keyword set


def _outcome(validator: object, text: str, key: str, **kwargs: object) -> object:
    outcomes = validator.check(_context(text, **kwargs))  # type: ignore[attr-defined]
    return next(outcome for outcome in outcomes if outcome.check_key == key)


def _requirement(
    *, blocking: bool = True, values: tuple[str, ...] = ("own machine",)
) -> Requirement:
    return Requirement(
        key="R-001",
        text="The unit must state that inference runs on the reader's own machine.",
        blocking=blocking,
        source=SourceReference(document="brief", quote="a quotation long enough to be evidence"),
        compiled_by=COMPILED_BY,
        checks=(RequirementCheck(kind="must_contain_any", values=values),),
    )


# ---------------------------------------------------------------- structural


@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        (GOOD_UNIT, "non_empty", True),
        ("", "non_empty", False),
        ("   \n\n  ", "non_empty", False),
        ("# A\n## B\n### C\n#### D\n\nBody text here.", "heading_depth", True),
        ("##### Too deep\n\nBody.", "heading_depth", False),
        ("```\ncode\n```\n\nDone.", "closed_code_fences", True),
        ("```\ncode\n\nDone.", "closed_code_fences", False),
        ("This is **bold** text.", "closed_emphasis", True),
        ("This is **bold text.", "closed_emphasis", False),
        ("- one\n- two\n", "lists_well_formed", True),
        ("- one\n-\n- three\n", "lists_well_formed", False),
        ("1. one\n2.\n", "lists_well_formed", False),
        ("A complete sentence.", "complete_ending", True),
        ("A sentence that just stops mid", "complete_ending", False),
        ("# A heading is a complete ending", "complete_ending", True),
        ("- a list item is too", "complete_ending", True),
        ("| a | table |", "complete_ending", True),
        ("> a blockquote", "complete_ending", True),
    ],
)
def test_structural_checks(text: str, key: str, expected: bool) -> None:
    assert _outcome(StructuralValidator(), text, key).passed is expected  # type: ignore[attr-defined]


def test_asterisks_inside_a_code_fence_are_not_emphasis() -> None:
    """Malformed-input case: a code block full of asterisks is code, not unbalanced markup."""
    text = "Text.\n\n```python\nx = a ** b ** c\n```\n\nMore text.\n"
    assert _outcome(StructuralValidator(), text, "closed_emphasis").passed  # type: ignore[attr-defined]


def test_structural_failures_are_all_blocking() -> None:
    """A truncated sentence renders wrong; no style preference makes that acceptable."""
    outcomes = StructuralValidator().check(_context("A sentence that stops mid"))
    assert all(outcome.blocking for outcome in outcomes)


# ---------------------------------------------------------------- length


@pytest.mark.parametrize(
    ("words", "target", "key", "expected"),
    [
        (200, 400, "minimum_words", True),
        (200, 400, "maximum_words", True),
        (199, 400, "minimum_words", False),
        (200, 400, "minimum_words", True),
        (720, 400, "maximum_words", True),
        (721, 400, "maximum_words", False),
        (40, None, "minimum_words", True),
        (39, None, "minimum_words", False),
    ],
)
def test_length_bands_including_both_boundaries(
    words: int, target: int | None, key: str, expected: bool
) -> None:
    unit = PlanUnit(key="U-01", ordinal=1, title="t", goal_text="g", target_words=target)
    text = " ".join(["word"] * words)
    assert _outcome(LengthValidator(), text, key, unit=unit).passed is expected  # type: ignore[attr-defined]


def test_falling_short_blocks_but_running_long_only_advises() -> None:
    """Risk T4: an editor can cut. Refusing generous writing is the trap."""
    unit = PlanUnit(key="U-01", ordinal=1, title="t", goal_text="g", target_words=100)
    outcomes = LengthValidator().check(_context("word " * 500, unit=unit))
    by_key = {outcome.check_key: outcome for outcome in outcomes}
    assert by_key["maximum_words"].passed is False
    assert by_key["maximum_words"].blocking is False
    assert by_key["minimum_words"].blocking is True


def test_hyphenated_and_apostrophised_words_count_once() -> None:
    unit = PlanUnit(key="U-01", ordinal=1, title="t", goal_text="g", target_words=6)
    outcome = _outcome(
        LengthValidator(),
        "don't split hyphen-words when counting three",
        "minimum_words",
        unit=unit,
    )
    assert outcome.evidence == ("6",)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- format


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("No front matter here at all.", True),
        ("---\ntitle: x\n---\n\nBody.", True),
        ("---\ntitle: x\n\nBody with no closing marker.", False),
    ],
)
def test_front_matter(text: str, expected: bool) -> None:
    assert _outcome(FormatValidator(), text, "front_matter").passed is expected  # type: ignore[attr-defined]


def test_json_validity_applies_only_to_a_structured_unit() -> None:
    assert _outcome(FormatValidator(), "not json", "json_body").passed  # type: ignore[attr-defined]
    assert _outcome(  # type: ignore[attr-defined]
        FormatValidator(), '{"a": 1}', "json_body", content_type="structured"
    ).passed
    bad = _outcome(FormatValidator(), "{not json", "json_body", content_type="structured")
    assert bad.passed is False  # type: ignore[attr-defined]
    assert "not valid JSON" in bad.detail  # type: ignore[attr-defined]


# ---------------------------------------------------------------- content


@pytest.mark.parametrize(
    "text",
    [
        "As an AI, I should mention that inference runs locally.",
        "As a language model I cannot judge that.",
        "Here's the draft you asked for.",
        "I hope this helps!",
        "Let me know if you want a different angle.",
        "Certainly! Local inference is a good topic.",
    ],
)
def test_meta_commentary_is_caught_and_blocks(text: str) -> None:
    outcome = _outcome(ContentConstraintValidator(), text, "no_meta_commentary")
    assert outcome.passed is False  # type: ignore[attr-defined]
    assert outcome.blocking is True  # type: ignore[attr-defined]


def test_ordinary_prose_is_not_mistaken_for_meta_commentary() -> None:
    """The narrow-heuristic case: these are all legitimate sentences about AI."""
    for text in (
        "An AI model runs on your machine.",
        "The AI industry has settled on this convention.",
        "Certainly the fastest option is a local model.",
        "This helps when you have no network.",
    ):
        assert _outcome(ContentConstraintValidator(), text, "no_meta_commentary").passed, text  # type: ignore[attr-defined]


def test_forbidden_phrases_are_case_insensitive() -> None:
    outcome = _outcome(
        ContentConstraintValidator(),
        "We UPLOAD your documents nightly.",
        "no_forbidden_phrases",
        forbidden_phrases=("upload your documents",),
    )
    assert outcome.passed is False  # type: ignore[attr-defined]


def test_a_requirements_own_check_runs_at_validation_with_its_blocking_class() -> None:
    blocking = ContentConstraintValidator().check(
        _context("Nothing about where it runs.", requirements=(_requirement(),))
    )
    failure = next(o for o in blocking if o.check_key.startswith("requirement:R-001"))
    assert failure.passed is False
    assert failure.blocking is True
    assert "R-001" in failure.detail

    advisory = ContentConstraintValidator().check(
        _context("Nothing about where it runs.", requirements=(_requirement(blocking=False),))
    )
    soft = next(o for o in advisory if o.check_key.startswith("requirement:R-001"))
    assert soft.blocking is False


# ---------------------------------------------------------------- reference


def test_an_internal_link_must_name_a_heading_here() -> None:
    good = "# Privacy\n\nSee [privacy](#privacy) below.\n"
    assert _outcome(ReferenceIntegrityValidator(), good, "internal_anchors").passed  # type: ignore[attr-defined]
    bad = "# Privacy\n\nSee [pricing](#pricing) below.\n"
    assert not _outcome(ReferenceIntegrityValidator(), bad, "internal_anchors").passed  # type: ignore[attr-defined]


def test_a_unit_reference_must_name_a_unit_of_this_project() -> None:
    unit = PlanUnit(key="U-02", ordinal=2, title="t", goal_text="g")
    outcome = _outcome(
        ReferenceIntegrityValidator(),
        "As U-01 explained, and as U-99 will not.",
        "unit_references",
        unit=unit,
        committed_units={"U-01": "text"},
    )
    assert outcome.passed is False  # type: ignore[attr-defined]
    assert outcome.evidence == ("U-99",)  # type: ignore[attr-defined]


def test_citations_are_advisory_and_vacuous_with_no_sources() -> None:
    """Risk M2 is real; the fact-check stage is where it is answered, not a regex."""
    outcome = _outcome(ReferenceIntegrityValidator(), "See [the study](study.pdf).", "citations")
    assert outcome.passed is True  # type: ignore[attr-defined]
    assert outcome.blocking is False  # type: ignore[attr-defined]

    with_sources = _outcome(
        ReferenceIntegrityValidator(),
        "See [the study](missing.pdf).",
        "citations",
        source_titles=("present.pdf",),
    )
    assert with_sources.passed is False  # type: ignore[attr-defined]
    assert with_sources.blocking is False  # type: ignore[attr-defined]


# ---------------------------------------------------------------- consistency


def test_a_glossary_variant_is_flagged_as_advisory() -> None:
    outcome = _outcome(
        ConsistencyValidator(),
        "The AI suite handles this.",
        "glossary_terms",
        glossary={"AI suite": "the suite"},
    )
    assert outcome.passed is False  # type: ignore[attr-defined]
    assert outcome.blocking is False  # type: ignore[attr-defined]
    assert "'AI suite'" in outcome.evidence[0]  # type: ignore[attr-defined]


def test_glossary_matching_is_whole_word() -> None:
    """`rerank` must not fire on `reranking`."""
    assert _outcome(  # type: ignore[attr-defined]
        ConsistencyValidator(), "reranking works", "glossary_terms", glossary={"rerank": "re-rank"}
    ).passed


def test_no_glossary_means_nothing_to_check() -> None:
    assert _outcome(ConsistencyValidator(), "anything", "glossary_terms").passed  # type: ignore[attr-defined]


# ---------------------------------------------------------------- safety


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("<script>alert(1)</script>", "no_script_tags"),
        ('<img onerror="steal()">', "no_event_handlers"),
        ("[click](javascript:alert(1))", "no_javascript_urls"),
        ("{{ 7*7 }}", "no_template_syntax"),
        ("{% for x in y %}", "no_template_syntax"),
        ("<a href='data:text/html,x'>", "no_html_data_uri"),
    ],
)
def test_hostile_markup_is_flagged(text: str, key: str) -> None:
    outcome = _outcome(SafetyValidator(), text, key)
    assert outcome.passed is False  # type: ignore[attr-defined]
    assert outcome.evidence  # type: ignore[attr-defined]


def test_markup_flags_are_advisory_because_escaping_is_the_real_control() -> None:
    """An article about web security legitimately quotes a <script> tag."""
    outcomes = SafetyValidator().check(_context("<script>alert(1)</script>"))
    script = next(o for o in outcomes if o.check_key == "no_script_tags")
    assert script.blocking is False


@pytest.mark.parametrize(
    "text",
    ["See ../../etc/passwd", 'open("../secrets")', "cat /etc/passwd", "look in ~/.ssh/id_rsa"],
)
def test_path_traversal_blocks(text: str) -> None:
    """The one thing no piece of prose needs, and risk S2 says must never reach a filesystem."""
    outcome = _outcome(SafetyValidator(), text, "no_path_traversal")
    assert outcome.passed is False  # type: ignore[attr-defined]
    assert outcome.blocking is True  # type: ignore[attr-defined]


def test_ordinary_prose_trips_no_safety_flag() -> None:
    outcomes = SafetyValidator().check(_context(GOOD_UNIT))
    assert all(outcome.passed for outcome in outcomes), [
        o.check_key for o in outcomes if not o.passed
    ]


# ---------------------------------------------------------------- the whole suite


def test_a_good_unit_passes_every_family() -> None:
    unit = PlanUnit(key="U-01", ordinal=1, title="t", goal_text="g", target_words=60)
    report = run_validators(DEFAULT_VALIDATORS, _context(GOOD_UNIT, unit=unit))
    assert report.passed, [o.check_key for o in report.blocking_failures]
    assert report.failure_count == 0
    assert "passed" in report.summary()


def test_unicode_content_passes_and_is_counted_correctly() -> None:
    unit = PlanUnit(key="U-01", ordinal=1, title="t", goal_text="g", target_words=20)
    text = (
        "# Résumé\n\nLe modèle s'exécute sur votre propre machine. "
        + "Rien n'est téléversé où que ce soit, jamais, nulle part ailleurs. " * 3
    )
    report = run_validators(DEFAULT_VALIDATORS, _context(text, unit=unit))
    assert report.passed, [o.detail for o in report.blocking_failures]


def test_an_empty_unit_fails_blocking_and_says_so() -> None:
    report = run_validators(DEFAULT_VALIDATORS, _context(""))
    assert not report.passed
    keys = {outcome.check_key for outcome in report.blocking_failures}
    assert "non_empty" in keys
    assert "1 blocking" in report.summary() or "blocking" in report.summary()


def test_advisory_failures_alone_do_not_stop_a_commit() -> None:
    """The split is load-bearing: this is what "too strict" would break."""
    unit = PlanUnit(key="U-01", ordinal=1, title="t", goal_text="g", target_words=10)
    report = run_validators(
        DEFAULT_VALIDATORS,
        _context(GOOD_UNIT, unit=unit, glossary={"machine": "device"}),
    )
    assert report.advisory_failures
    assert report.passed, "advisory findings must never block"
    assert report.failure_count == len(report.advisory_failures)


def test_a_validator_that_raises_is_not_swallowed() -> None:
    """A caught exception would report "passed" for a check that never ran."""

    class Broken:
        kind = "broken"

        def check(self, context: ValidationContext) -> list[object]:
            message = "this validator is defective"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        run_validators([Broken()], _context("anything"))  # type: ignore[list-item]


def test_severity_reads_as_one_word() -> None:
    report = run_validators(DEFAULT_VALIDATORS, _context(""))
    severities = {outcome.severity for outcome in report.outcomes}
    assert severities <= {"ok", "blocking", "advisory"}
