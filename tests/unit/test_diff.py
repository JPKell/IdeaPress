"""The diff view — P8's named failure mode is it breaking on long lines or unicode.

Both sides of a diff are model output (risk S1), so nothing here produces markup: the service
returns structured rows and the template escapes them exactly once. These tests are about the
*content* being carried through intact, which is the half a rendering test cannot see.
"""

from __future__ import annotations

from ideapress.services.diff import DiffLine, diff_lines


def test_an_added_line_is_marked_added() -> None:
    rows, _ = diff_lines("one\ntwo", "one\ntwo\nthree")
    added = [row for row in rows if row.kind == "added"]
    assert [row.text for row in added] == ["three"]
    assert added[0].new_number == 3
    assert added[0].old_number is None


def test_a_removed_line_is_marked_removed() -> None:
    rows, _ = diff_lines("one\ntwo\nthree", "one\nthree")
    removed = [row for row in rows if row.kind == "removed"]
    assert [row.text for row in removed] == ["two"]


def test_identical_texts_produce_no_changes() -> None:
    rows, _ = diff_lines("same\ntext", "same\ntext")
    assert all(row.kind == "equal" for row in rows)


def test_line_numbers_are_one_based_and_track_both_sides() -> None:
    rows, _ = diff_lines("a\nb\nc", "a\nB\nc")
    equal = [row for row in rows if row.kind == "equal"]
    assert equal[0].old_number == 1
    assert equal[0].new_number == 1


def test_unicode_survives_intact() -> None:
    """The named failure mode. A diff that re-encoded its input would show mojibake for the
    author's own words."""
    earlier = "naïve café — 日本語 🎉\nsecond"
    later = "naïve café — 日本語 🎉\nchanged"
    rows, _ = diff_lines(earlier, later)
    assert any(row.text == "naïve café — 日本語 🎉" for row in rows)
    assert any(row.text == "changed" and row.kind == "added" for row in rows)


def test_a_very_long_line_is_carried_whole() -> None:
    """900 characters on one line: wrapped by CSS, never truncated by the service."""
    long_line = "x" * 900
    rows, _ = diff_lines("short", long_line)
    added = [row for row in rows if row.kind == "added"]
    assert len(added[0].text) == 900


def test_html_in_the_text_is_not_escaped_by_the_service() -> None:
    """Escaping is the template's job and happens exactly once. Doing it here too would show a
    reader `&amp;lt;` where the model wrote `<`."""
    rows, _ = diff_lines("plain", "<script>alert(1)</script>")
    added = [row for row in rows if row.kind == "added"]
    assert added[0].text == "<script>alert(1)</script>"


def test_long_unchanged_runs_are_elided_and_say_so() -> None:
    earlier = "\n".join(str(number) for number in range(50))
    later = earlier + "\nnew"
    rows, truncated = diff_lines(earlier, later)
    assert truncated is True
    assert len(rows) < 50


def test_context_lines_are_configurable_and_zero_keeps_none() -> None:
    earlier = "\n".join(str(number) for number in range(50))
    later = earlier + "\nnew"
    rows, _ = diff_lines(earlier, later, context_lines=0)
    assert [row.text for row in rows if row.kind != "equal"] == ["new"]


def test_the_marker_carries_the_same_information_as_the_colour() -> None:
    """UI/UX Standards §13: colour is never the sole indicator of state."""
    assert DiffLine(kind="added", text="").marker == "+"
    assert DiffLine(kind="removed", text="").marker == "-"
    assert DiffLine(kind="equal", text="").marker == " "


def test_an_empty_text_diffs_against_a_full_one() -> None:
    rows, _ = diff_lines("", "first\nsecond")
    assert [row.text for row in rows if row.kind == "added"] == ["first", "second"]
