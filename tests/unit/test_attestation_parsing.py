"""ADR-0039: an audit's silence must not settle anything — only its explicit attestation can.

The parsing half of option (b): per-requirement verdicts come back in
``requirements_assessment``, only a literal ``met`` ever enters :attr:`AuditReport.attested_met`,
and every malformed or absent answer degrades toward ``cannot_judge`` — the direction that never
satisfies a gate.
"""

from __future__ import annotations

import json

from ideapress.domain.requirements import (
    CompiledBy,
    Requirement,
    RequirementCheck,
    SourceReference,
)
from ideapress.services.review import parse_findings, render_assessed_requirements

_SOURCE = SourceReference(document="brief", quote="a quotation long enough to ground something")
_COMPILED = CompiledBy(prompt_id="stages.requirements.compile", version="1.1.0")


def _requirement(key: str, *, checked: bool, blocking: bool = True) -> Requirement:
    return Requirement(
        key=key,
        text=f"Requirement {key} text",
        blocking=blocking,
        source=_SOURCE,
        compiled_by=_COMPILED,
        checks=((RequirementCheck(kind="must_contain_any", values=("word",)),) if checked else ()),
    )


def test_only_an_explicit_met_is_attested() -> None:
    report = parse_findings(
        json.dumps(
            {
                "findings": [],
                "requirements_assessment": [
                    {"key": "R-001", "verdict": "met"},
                    {"key": "R-002", "verdict": "not_met"},
                    {"key": "R-003", "verdict": "cannot_judge"},
                ],
            }
        ),
        stage="audit_fast",
    )
    assert report.attested_met == frozenset({"R-001"})
    assert dict(report.requirement_verdicts) == {
        "R-001": "met",
        "R-002": "not_met",
        "R-003": "cannot_judge",
    }


def test_an_invented_verdict_reads_as_cannot_judge_never_met() -> None:
    """Coercing an unknown verdict toward satisfaction would be the silence hole respelled."""
    report = parse_findings(
        json.dumps(
            {
                "findings": [],
                "requirements_assessment": [
                    {"key": "R-001", "verdict": "definitely fine"},
                    {"key": "R-002", "verdict": "MET"},
                ],
            }
        ),
        stage="audit_fast",
    )
    assert dict(report.requirement_verdicts)["R-001"] == "cannot_judge"
    assert dict(report.requirement_verdicts)["R-002"] == "met", "case folding is not invention"
    assert report.attested_met == frozenset({"R-002"})


def test_an_absent_assessment_attests_nothing() -> None:
    """The old mechanism's exact failure: silence. It now produces the empty set."""
    report = parse_findings(json.dumps({"findings": []}), stage="audit_fast")
    assert report.requirement_verdicts == ()
    assert report.attested_met == frozenset()


def test_malformed_assessment_entries_are_dropped_not_guessed() -> None:
    report = parse_findings(
        json.dumps(
            {
                "findings": [],
                "requirements_assessment": [
                    "not an object",
                    {"verdict": "met"},
                    {"key": "  ", "verdict": "met"},
                    {"key": "R-009", "verdict": "met"},
                ],
            }
        ),
        stage="audit_fast",
    )
    assert report.attested_met == frozenset({"R-009"})


def test_the_assessed_list_is_exactly_the_checkless_requirements() -> None:
    rendered = render_assessed_requirements(
        [
            _requirement("R-001", checked=True),
            _requirement("R-002", checked=False),
            _requirement("R-003", checked=False, blocking=False),
        ]
    )
    assert "R-001" not in rendered, "a checked requirement is the check's business, not a model's"
    assert "R-002 [BLOCKING]" in rendered
    assert "R-003 [advisory]" in rendered


def test_the_assessed_list_says_none_when_everything_is_checked() -> None:
    rendered = render_assessed_requirements([_requirement("R-001", checked=True)])
    assert rendered == "(none — return an empty array)"
