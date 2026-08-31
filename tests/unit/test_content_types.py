"""The content-type registry: open, and the engine knows nothing about what is in it."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from baseaicore import ValidationError

from ideapress.content_types.registry import (
    ARTICLE,
    CONTENT_TYPES,
    ENTRY_POINT_GROUP,
    REPORT,
    ContentType,
    discover,
    get_content_type,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "ideapress"


def test_two_content_types_ship_at_1_0() -> None:
    assert set(CONTENT_TYPES) == {"article", "report"}
    assert ARTICLE.version == "1.0"
    assert REPORT.version == "1.0"


def test_fact_check_is_on_for_the_research_backed_type_only() -> None:
    """Workflows §2 stage 10: "on for research-backed content types"."""
    assert REPORT.fact_check_by_default is True
    assert ARTICLE.fact_check_by_default is False


def test_the_registry_is_open() -> None:
    assert ENTRY_POINT_GROUP == "ideapress.content_types"
    assert set(discover()) >= {"article", "report"}


def test_an_unknown_content_type_is_refused_and_names_the_alternatives() -> None:
    with pytest.raises(ValidationError) as caught:
        get_content_type("novel")
    assert "novel" in caught.value.message
    assert "article" in caught.value.message


def test_a_content_type_is_data_not_behaviour() -> None:
    """Risk G2: a content type the engine reads, never a branch the engine takes."""
    fields = set(ContentType.__dataclass_fields__)
    assert fields == {
        "name",
        "version",
        "description",
        "default_workflow",
        "min_units",
        "max_units",
        "target_words_per_unit",
        "structural_expectations",
        "fact_check_by_default",
    }
    for value in vars(ARTICLE).values() if hasattr(ARTICLE, "__dict__") else ():
        assert not callable(value)


def test_no_engine_module_mentions_a_content_types_vocabulary() -> None:
    """Risk G2's own mitigation: a term scan for content-type vocabulary in engine modules.

    The engine is `domain/` and `services/` **excluding** the registry itself. It may say "unit"
    and "requirement"; it may not say "chapter", "scene", "abstract" or "executive summary", because
    the moment it does, a second content type needs the engine changed.
    """
    # Whole words, not substrings: "quest" is inside "request" and "abstract" is inside
    # "AbstractContextManager", and a scan that flagged those would be noise nobody reads.
    # "abstract" is deliberately absent: `AbstractContextManager` is a standard-library type, and
    # a scan that flagged it would produce noise nobody reads, which is how a term scan stops being
    # run at all. These seven are unambiguous — none of them is a word this engine could need.
    forbidden = re.compile(
        r"\b(chapters?|scenes?|quests?|stanzas?|bylines?|sidebars?|executive summary)\b"
    )
    offenders: list[str] = []
    for path in sorted((SRC / "domain").rglob("*.py")) + sorted((SRC / "services").rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        # Docstrings are prose about the design and may quote the forbidden words when explaining
        # why they are forbidden; identifiers and string constants may not.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                readable = re.sub(r"(?<!^)(?=[A-Z])", " ", name).replace("_", " ").lower()
                if forbidden.search(readable):
                    offenders.append(f"{path.name}:{node.lineno} {name}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and forbidden.search(node.value.lower())
            ):
                offenders.append(f"{path.name}:{node.lineno} {node.value[:40]!r}")
        assert text is not None
    assert offenders == [], offenders


def test_the_engine_speaks_of_units_and_requirements() -> None:
    """The positive half: the vocabulary it *does* use is the one workflows §10 permits."""
    engine = (SRC / "services" / "unit_loop.py").read_text(encoding="utf-8").lower()
    assert "unit" in engine
    assert "requirement" in engine
