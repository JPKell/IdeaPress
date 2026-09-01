"""The configuration reference is generated, so it cannot drift — P9's named failure mode.

"Documentation drift from the generated configuration reference" is one of P9's likely failure
modes, and the only durable answer is that the committed file is not written by hand and a test
compares it against the generator. A setting added without documentation then fails the gate in the
same commit that added it.
"""

from __future__ import annotations

from pathlib import Path

from ideapress.config_reference import render_reference, sections

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


def test_the_committed_reference_equals_what_the_generator_produces() -> None:
    """The drift check itself. Regenerate with:

    ```bash
    python -m ideapress.config_reference > docs/configuration.md
    ```
    """
    assert REFERENCE.exists(), "docs/configuration.md is missing"
    assert REFERENCE.read_text(encoding="utf-8") == render_reference(), (
        "docs/configuration.md is out of date; regenerate it with "
        "`python -m ideapress.config_reference > docs/configuration.md`"
    )


def test_every_settings_section_with_keys_appears_in_the_reference() -> None:
    """A section the walker misses would be a silently undocumented part of the configuration.

    A section holding only sub-sections — `[models]`, whose sole member is `[models.stages]` — has
    no keys to tabulate and is deliberately not given an empty table. Its children are documented
    on their own, which is where a person looks for them.
    """
    from pydantic import BaseModel

    text = REFERENCE.read_text(encoding="utf-8")
    for path, model in sections():
        has_keys = any(
            not (isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel))
            for field in model.model_fields.values()
        )
        if not has_keys:
            continue
        assert f"## `[{path}]`" in text, path


def test_every_leaf_setting_appears_with_its_environment_variable() -> None:
    """Configuration Standards §3: every setting is reachable from the environment, and the
    reference is where a person finds the spelling."""
    text = REFERENCE.read_text(encoding="utf-8")
    missing = []
    for path, model in sections():
        for name, field in model.model_fields.items():
            annotation = field.annotation
            from pydantic import BaseModel

            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                continue
            env = f"IDEAPRESS_{path.replace('.', '__').upper()}__{name.upper()}"
            if f"`{name}`" not in text or env not in text:
                missing.append(f"{path}.{name}")
    assert missing == [], f"settings absent from the reference: {missing}"


def test_the_reference_is_deterministic() -> None:
    """Two renders differ in nothing, so the drift test says something real rather than flapping."""
    assert render_reference() == render_reference()


def test_the_reference_says_it_is_generated() -> None:
    """A reader who edits it by hand should be told before they start, not after their change is
    overwritten by the next regeneration."""
    text = REFERENCE.read_text(encoding="utf-8")
    assert "Do not edit by hand" in text
    assert "python -m ideapress.config_reference" in text


def test_the_refusals_are_documented() -> None:
    """A refusal a person cannot look up is a refusal they will file as a bug."""
    text = REFERENCE.read_text(encoding="utf-8")
    for phrase in ("INSECURE_BINDING", "max_concurrent_stages", "fallback_mode", "allowed_hosts"):
        assert phrase in text, phrase
