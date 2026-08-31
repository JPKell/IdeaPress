"""Slug derivation and safety — the rule that keeps user and model text away from the filesystem."""

from __future__ import annotations

import pytest

from ideapress.domain.project import MAX_SLUG_LENGTH, is_safe_slug, slugify


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My Great Article", "my-great-article"),
        ("  Leading and trailing  ", "leading-and-trailing"),
        ("Ünïcödé Tïtlé", "unicode-title"),
        ("Multiple   spaces", "multiple-spaces"),
        ("Hyphen--collapse", "hyphen-collapse"),
        ("2026 Report", "2026-report"),
    ],
)
def test_ordinary_titles_slugify_readably(title: str, expected: str) -> None:
    assert slugify(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "../../etc/passwd",
        "..",
        ".",
        "./.",
        "a/b/c",
        "a\\b\\c",
        "\x00null",
        "CON",
        "nul",
        "LPT1",
        "",
        "   ",
        "🙂🙂🙂",
        "-",
        "---",
        "~/.ssh/id_rsa",
        "%2e%2e%2f",
    ],
)
def test_hostile_titles_still_produce_a_safe_slug(title: str) -> None:
    """Risk S2: a path is never built from text a user or a model supplied."""
    slug = slugify(title)
    assert is_safe_slug(slug), f"{title!r} produced unsafe slug {slug!r}"
    assert "/" not in slug
    assert "\\" not in slug
    assert slug not in {".", ".."}


def test_a_very_long_title_is_truncated_and_still_safe() -> None:
    slug = slugify("word " * 200)
    assert len(slug) <= MAX_SLUG_LENGTH
    assert is_safe_slug(slug)
    assert not slug.endswith("-")


@pytest.mark.parametrize(
    "slug",
    ["", ".", "..", "a/b", "a\\b", "A", "Ab", "-a", "a-", "con", "nul", "com1", "a" * 65, "a b"],
)
def test_is_safe_slug_refuses_what_it_must(slug: str) -> None:
    assert not is_safe_slug(slug)


@pytest.mark.parametrize("slug", ["a", "ab", "a-b", "my-great-article", "2026-report", "a" * 64])
def test_is_safe_slug_accepts_what_slugify_produces(slug: str) -> None:
    assert is_safe_slug(slug)


def test_a_title_with_nothing_usable_falls_back() -> None:
    """Uniqueness is the service's job; slugify only guarantees a valid, safe name."""
    assert slugify("..", fallback="project") == "project"
    assert slugify("🙂", fallback="draft") == "draft"
    assert slugify("project") == "project"
