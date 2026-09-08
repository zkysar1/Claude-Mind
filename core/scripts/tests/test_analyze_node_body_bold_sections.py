"""_analyze_node_body counts BOLD-delimited append-grown sections, not only headings.

g-115-4883. The classifier scanned markdown HEADINGS only, so a node that
delimits every refresh block with a line-start bold run scored ~0 and was
routed to the `distill is WRONG` arm of crit3 -- while tree.py's own comment
cited one such node (product-world-model) as evidence that PRECISION HOLDS.
A false negative offered as proof of accuracy is what stops the next reader
re-measuring, so the comment is pinned here too.

The two controls at the bottom are the ones that matter: the first probe of
this defect used `^\\*\\*([^*]{0,200})\\*\\*` and over-reported, because a
negated character class MATCHES NEWLINES (it spanned lines and harvested
stamps from following ones) while the 200-char bound simultaneously DROPPED
the motivating node, whose header is longer. Both directions are pinned.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tree as T


def _sections(text):
    return T._analyze_node_body(text)[2]


def test_bold_delimited_stamped_sections_are_counted():
    body = ("**Refresh 2026-08-04 session-zeta cc-02 (g-115-23 sweep)**\n"
            "prose\n"
            "**Refresh 2026-08-05 session-alpha cc-07 (g-115-99 sweep)**\n"
            "prose\n"
            "**g-306-284 — carrier disposition**\n")
    assert _sections(body) == 3


def test_heading_arm_is_unchanged():
    body = "## Refresh 2026-06-21\n## g-315-330 — thing\n## unrelated\n"
    assert _sections(body) == 2


def test_the_two_arms_apply_the_SAME_test_not_two_drifting_ones():
    # Same marker text, two delimiters -> same verdict, in both directions.
    for marker in ("Refresh 2026-08-04 x", "g-115-4883 y", "Verified Values"):
        assert _sections("## %s\n" % marker) == _sections("**%s**\n" % marker) == 1
    for marker in ("just a bolded phrase", "no stamp here"):
        assert _sections("## %s\n" % marker) == _sections("**%s**\n" % marker) == 0


def test_a_bold_run_must_not_span_lines():
    # The negated-character-class form matched across the newline and counted
    # this as a section. It is not one.
    assert _sections("**a\nb 2026-01-01**\n") == 0


def test_a_long_header_is_not_dropped():
    # The 200-char bound dropped the very node that motivated the goal.
    long_marker = "Refresh 2026-08-04 session-zeta cc-02 " + ("x" * 300)
    assert _sections("**%s**\n" % long_marker) == 1


def test_bold_must_start_the_line():
    assert _sections("   **Refresh 2026-01-01**\n") == 0
    assert _sections("see **Refresh 2026-01-01** inline\n") == 0


def test_empty_and_unstamped_bodies_score_zero():
    # Positive control for the predicate's ability to return 0 at all.
    assert _sections("") == 0
    assert _sections("plain prose with no sections\n") == 0


def test_comment_no_longer_cites_product_world_model_as_a_correct_negative():
    src = Path(T.__file__).read_text(encoding="utf-8")
    assert "product-world-model 1, test-coverage-illusions 0" not in src, (
        "the stale precision-example citation is back: product-world-model is a "
        "measured FALSE negative, not a correct one (g-115-4883)")
    assert "product-world-model USED TO BE CITED HERE" in src


def test_bold_pattern_is_line_bounded_and_non_greedy():
    # Guards the regex itself, so a future 'simplification' to [^*]* fails here.
    assert T._APPEND_BOLD_SECTION.match("**a\nb**") is None
    m = T._APPEND_BOLD_SECTION.match("**one** and **two**")
    assert m.group(1) == "one"
