"""`domain.research`: which targets a project has, decided before anything is fetched (row M1).

Pure and dull on purpose (ADR-0116 decision 4). Every case here is a thing the stage will
**not** do — because a research stage that inferred a target would be choosing its own egress.
"""

from __future__ import annotations

from ideapress.domain.research import MAX_URL_LENGTH, brief_urls, note_title_for


def test_absolute_http_urls_are_found_in_order() -> None:
    brief = "See http://a.example/one and then https://b.example/two for the numbers."
    assert brief_urls(brief) == ("http://a.example/one", "https://b.example/two")


def test_a_repeated_url_is_fetched_once() -> None:
    """A brief citing one source three times must not spend three fetches on it."""
    brief = "http://a.example/x is key. Again: http://a.example/x. And http://a.example/x."
    assert brief_urls(brief) == ("http://a.example/x",)


def test_a_sentence_final_full_stop_is_not_part_of_the_address() -> None:
    assert brief_urls("Read http://a.example/page.") == ("http://a.example/page",)
    assert brief_urls("Read http://a.example/page, then stop") == ("http://a.example/page",)


def test_markdown_link_syntax_does_not_swallow_the_bracket() -> None:
    assert brief_urls("[docs](https://a.example/d) here") == ("https://a.example/d",)


def test_a_bare_hostname_is_not_a_url() -> None:
    """Nothing completes a scheme: a completed address is one nobody wrote."""
    assert brief_urls("see docs.example.com for details") == ()


def test_a_non_http_scheme_is_not_a_url() -> None:
    assert brief_urls("file:///etc/passwd and ftp://a.example/x and gopher://b/c") == ()


def test_a_scheme_with_no_host_is_refused() -> None:
    assert brief_urls("http:///nothing") == ()


def test_an_over_long_url_is_dropped_rather_than_sent_to_be_refused() -> None:
    """ToolYard would refuse it; a record saying so tells the operator nothing actionable."""
    long_url = "http://a.example/" + "x" * MAX_URL_LENGTH
    assert brief_urls(long_url) == ()


def test_an_empty_brief_has_no_targets() -> None:
    assert brief_urls("") == ()


def test_scheme_case_is_accepted_and_preserved() -> None:
    assert brief_urls("HTTPS://A.example/X") == ("HTTPS://A.example/X",)


def test_a_note_is_titled_by_its_citation_not_its_content() -> None:
    """A fetched page must not be able to name itself after the unit and win the budget."""
    assert note_title_for("  https://a.example/x  ") == "https://a.example/x"
    assert len(note_title_for("h" * 500)) == 300
