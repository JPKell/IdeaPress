"""Structured logging: correlation fields attach, and project content never leaks."""

from __future__ import annotations

import json
import logging

import pytest

from ideapress.observability.logging import configure_logging, correlation, current_correlation


def test_correlation_fields_attach_to_records(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(level="DEBUG")
    with caplog.at_level(logging.INFO), correlation(project_id="P1", stage="draft", attempt=2):
        logging.getLogger("t").info("stage.started")
    record = caplog.records[-1]
    assert record.__dict__["project_id"] == "P1"
    assert record.__dict__["stage"] == "draft"
    assert record.__dict__["attempt"] == "2"


def test_none_valued_fields_are_omitted_not_stringified() -> None:
    with correlation(project_id="P1", unit_id=None):
        assert current_correlation() == {"project_id": "P1"}


def test_correlation_is_removed_on_exit_including_on_error() -> None:
    with pytest.raises(RuntimeError), correlation(project_id="P1"):
        raise RuntimeError("boom")
    assert current_correlation() == {}


def test_nested_correlation_merges_and_unwinds() -> None:
    with correlation(project_id="P1"):
        with correlation(unit_id="U1"):
            assert current_correlation() == {"project_id": "P1", "unit_id": "U1"}
        assert current_correlation() == {"project_id": "P1"}


def test_content_is_redacted_at_info_even_when_content_logging_is_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec §14, risk S5: an INFO log is not a deliberate act by the person whose drafts these
    are."""
    configure_logging(level="DEBUG", include_content=True)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("t").info("attempt", extra={"response_text": "the user's draft"})
    assert caplog.records[-1].__dict__["response_text"] == "<redacted>"


def test_content_is_redacted_by_default_at_every_level(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(level="DEBUG", include_content=False)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("t").debug("attempt", extra={"prompt_text": "the user's brief"})
    assert caplog.records[-1].__dict__["prompt_text"] == "<redacted>"


def test_content_survives_at_debug_when_deliberately_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging(level="DEBUG", include_content=True)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("t").debug("attempt", extra={"prompt_text": "the user's brief"})
    assert caplog.records[-1].__dict__["prompt_text"] == "the user's brief"


def test_json_format_emits_one_object_per_line() -> None:
    from ideapress.observability.logging import _JsonFormatter

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "stage.started", (), None)
    record.__dict__["project_id"] = "P1"
    payload = json.loads(_JsonFormatter().format(record))
    assert payload["message"] == "stage.started"
    assert payload["project_id"] == "P1"
    assert payload["level"] == "INFO"


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    configure_logging(level="INFO")
    first = len([h for h in root.handlers if getattr(h, "_ideapress", False)])
    configure_logging(level="INFO")
    second = len([h for h in root.handlers if getattr(h, "_ideapress", False)])
    assert first == second == 1
