#!/usr/bin/env python3
""": an auto-filed Apply goal's title must name an OBJECT, not just a verb.

WHY THIS EXISTS. `insight-trigger-sweep.py` built its title from the bare
`action_type:<verb>` tag, so a generic verb produced `Apply: implement (from
<author> insight_trigger <msg-id>)` — a title that names no work at all.
guard-6141 is the governing rule: every consumer validates the RECORD, none
validates the TASK, so the selector scores a non-action as high as real work.

MEASURED 2026-09-20 (zeta, hostname cc-02, uname -r 6.8.0-139-generic) over the
317 goals this sweep has filed: 34 carried that shape, ALL 34 still `pending`,
oldest g-115-913 from 2026-05-18. Unlike guard-6141's own population (which
agents worked to close ten times), these are never worked — they only consume
selection bandwidth. One of them ranked 3rd of 2,294 candidates on the
iteration that produced this fix.

WHAT THIS SEAM EXCLUDES (guard-1462). Every test drives the pure `_title_subject`
helper or `_build_goal_payload` with a hand-built trigger. Board discovery, the
filing loop and the daemon write are upstream and unfalsifiable here; the
existing sweep suites cover those.

Anti-vacuity guard: `test_the_two_title_shapes_do_not_collapse`. Mutate against
THAT ALONE (guard-1793).
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "insight-trigger-sweep.py")
_spec = importlib.util.spec_from_file_location("its_title_mod", _SRC)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_title_mod"] = its
_spec.loader.exec_module(its)

# The real headline of msg-20260916-182132-echo-5305, the post that filed
#  — kept verbatim so the regression is pinned to a real input.
REAL_HEADLINE = (
    "THE vin_ KEY IS CONSUMED BY A DEPLOYED ENDPOINT — MEASURED WITH FABRICATED "
    "KEYS ONLY, NO SECRET DECRYPTED, NOTHING WRITTEN, NO SPEND."
)


def _trigger(text, action="implement"):
    return {
        "msg_id": "msg-test-1", "author": "echo", "channel": "findings",
        "timestamp": "2026-09-16T18:21:32", "text": text, "tags": ["x"],
        "action": action, "target": "either", "severity": "enables",
        "affects_goal": "g-373-27",
    }


# ── the pure helper ──────────────────────────────────────────────────────────

def test_first_meaningful_line_is_the_subject():
    assert its._title_subject("A real enough headline here\nbody\nmore") == \
        "A real enough headline here"


def test_blank_and_short_lines_are_skipped():
    """A banner or a stray token is not a headline."""
    assert its._title_subject("\n\nok\n---\nThe actual finding headline") == \
        "The actual finding headline"


def test_decoration_is_stripped():
    """Fleet posts open with === / ## / ** banners; the subject is the words."""
    assert its._title_subject("=== THE MEASURED FINDING, STATED ===") == \
        "THE MEASURED FINDING, STATED"


def test_empty_text_yields_no_subject():
    for empty in ("", None, "   \n\n  ", "body"):
        assert its._title_subject(empty) == ""


def test_overlong_headline_is_cut_on_a_word_boundary():
    line = " ".join(["word"] * 60)          # 299 chars, all whole words
    out = its._title_subject(line)
    assert len(out) <= its.TITLE_SUBJECT_MAX
    assert out.endswith("word")             # never mid-token
    assert line.startswith(out)


def test_a_trim_never_appends_an_ellipsis():
    """guard-6141 counts a trailing ellipsis as a FRAGMENT tell, so the fix must
    not manufacture the very signature it exists to remove."""
    out = its._title_subject(" ".join(["word"] * 60))
    assert not out.endswith("...")
    assert not out.endswith("…")


def test_a_single_unbroken_token_is_hard_cut_rather_than_dropped():
    out = its._title_subject("x" * 400)
    assert out == "x" * its.TITLE_SUBJECT_MAX


# ── the payload ──────────────────────────────────────────────────────────────

def test_payload_title_names_an_object():
    """The  shape: the verb alone named no work."""
    p = its._build_goal_payload(_trigger(REAL_HEADLINE + "\n\nbody"))
    assert p["title"].startswith("Apply: implement — THE vin_ KEY IS CONSUMED")
    assert "insight_trigger msg-test-1" in p["title"]


def test_payload_falls_back_to_the_bare_verb_when_there_is_no_headline():
    """Behaviour must never be worse than before the fix."""
    p = its._build_goal_payload(_trigger("body"))
    assert p["title"] == \
        "Apply: implement (from echo insight_trigger msg-test-1)"


def test_the_two_title_shapes_do_not_collapse():
    """Anti-vacuity: the spliced title and the fallback are really different,
    so a helper stubbed to return "" would fail this, not pass it."""
    spliced = its._build_goal_payload(_trigger(REAL_HEADLINE))["title"]
    fallback = its._build_goal_payload(_trigger("body"))["title"]
    assert spliced != fallback
    assert len(spliced) > len(fallback)
    assert "vin_" in spliced and "vin_" not in fallback


def test_dedup_key_is_unchanged_by_the_title_change():
    """guard-3751: a check that dedups on its own generated title goes blind the
    moment the template changes. This sweep keys on origin_signal instead — pin
    that, because the title fix is only safe while it stays true."""
    with_subject = its._build_goal_payload(_trigger(REAL_HEADLINE))
    without = its._build_goal_payload(_trigger("body"))
    assert with_subject["origin_signal"] == "insight_trigger:msg-test-1"
    assert with_subject["origin_signal"] == without["origin_signal"]


# ── the leading-minus regression (fresh-eyes probe, 2026-09-20) ──────────────

def test_a_leading_minus_is_never_stripped_from_a_measurement():
    """The first strip set included `-` on BOTH ends, so "-40% latency" became
    "40% latency" — a regression rendered as an improvement in the ONE field the
    selector shows. Found by the guard-343 fresh-eyes probe on this helper the
    same day it shipped; board msg-20260920-061103-zeta-2924."""
    assert its._title_subject("-40% latency after the fix was measured on cc-02") == \
        "-40% latency after the fix was measured on cc-02"


def test_a_leading_cli_flag_survives_the_decoration_strip():
    assert its._title_subject("--dry-run does not imply --json on this wrapper") == \
        "--dry-run does not imply --json on this wrapper"


def test_a_symmetric_dash_banner_still_reduces_to_its_words():
    """The narrowing must not cost the banner case it was protecting: a dash run
    on BOTH ends is decoration, and is still stripped."""
    assert its._title_subject("--- THE SYMMETRIC BANNER FINDING ---") == \
        "THE SYMMETRIC BANNER FINDING"
