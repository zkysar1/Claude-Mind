"""test_seed_manifest_goalid_strip.py — regression guard for seed-manifest G13
goal-id comment-strip (g-115-2919).

Background (2026-07-22, g-115-2919):
  G13 strips production goal-IDs from comments during the downstream seed so a
  fresh repo doesn't carry Ayoai-Mind's specific goal-ids. Its pattern was
  `g-\\d{2,4}-\\d{1,3}` — the SECOND group capped the goal SEQUENCE at 3 digits.
  But goal sequences expanded to 4 digits on 2026-05-19 (g-NNN-NNNN, after
  asp-115 passed g-115-999). On a 4-digit seq the `\\d{1,3}` greedily matched
  only the first 3 digits: `g-115-2397` -> matched `g-115-239`, stripped it,
  and left a bare `7` FRAGMENT. Every downstream sync (Claude-Mind, ZDS-Mind)
  carried corrupted cross-references like `# (7, mirrors _merge_goal)`.
  Fix: pattern MUST match the canonical goal-id regex `g-\\d{3}-\\d{2,4}`
  (aspirations.py:1301).

These tests load the REAL G13 rule from core/config/seed-manifest.yaml and run
it through the REAL transform engine, so a revert to `\\d{1,3}` (or any
narrowing of the second group) fails the suite.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
SEED_MANIFEST = CORE_SCRIPTS.parent / "config" / "seed-manifest.yaml"

# _seed_transforms is pure (no MIND_WORLD/MIND_AGENT import-time deps) — load
# it via spec loader the same way test_seed_engine_anchoring.py loads the engine.
_spec = importlib.util.spec_from_file_location(
    "_seed_transforms", CORE_SCRIPTS / "_seed_transforms.py"
)
_st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_st)


def _g13_rule() -> dict:
    """The live G13 rule as authored in seed-manifest.yaml (not a fixture)."""
    manifest = yaml.safe_load(SEED_MANIFEST.read_text(encoding="utf-8"))
    for r in manifest.get("transformations", []):
        if r.get("id") == "G13":
            return r
    raise AssertionError("G13 rule not found in seed-manifest.yaml")


def _strip(line: str, rel_path: str = "core/scripts/foo.py") -> str:
    """Run the live G13 rule through the real global_regex engine."""
    return _st.apply_global_regex(line, _g13_rule(), rel_path)


# ---------------------------------------------------------------------------
# Headline: the exact  corruption case
# ---------------------------------------------------------------------------

def test_g13_strips_4digit_goalid_with_no_digit_fragment():
    """ (4-digit seq) MUST strip cleanly — the bare `7` fragment is
    the regression this test exists to catch."""
    line = "# (g-115-2397, mirrors _merge_goal): the per-store merge fn's LWW\n"
    out = _strip(line)
    assert "g-115-2397" not in out
    assert "2397" not in out
    assert "(7" not in out, f"digit fragment left behind: {out!r}"
    assert out == "# (, mirrors _merge_goal): the per-store merge fn's LWW\n"


def test_g13_strips_second_4digit_evidence_case():
    """ -> '(7' was the second evidence case in ."""
    out = _strip("# (g-115-2547, mirrors _merge_goal)\n")
    assert "(7" not in out, f"digit fragment left behind: {out!r}"
    assert out == "# (, mirrors _merge_goal)\n"


# ---------------------------------------------------------------------------
# All valid sequence widths strip cleanly, none leave a fragment
# ---------------------------------------------------------------------------

def test_g13_strips_all_seq_widths_cleanly():
    for gid in ("g-115-2397", "g-115-999", "g-001-58", "g-354-13", "g-001-01"):
        out = _strip(f"# ref {gid} here\n")
        assert gid not in out, f"{gid} not stripped: {out!r}"
        # no residual trailing digit fragment where the id was
        assert not re.search(r"ref\s+\d", out), f"fragment left for {gid}: {out!r}"


# ---------------------------------------------------------------------------
# Shape/scope invariants — the fix must not over- or under-reach
# ---------------------------------------------------------------------------

def test_g13_leaves_xw_form_untouched():
    """The g-xw-<timestamp>-NN form is a different shape and is NOT matched by
    the g-NNN-NNNN pattern — it must survive untouched (no fragment either)."""
    line = "# g-xw-20260717T220413-01 forced-flip guard\n"
    assert _strip(line) == line


def test_g13_only_strips_in_comment_context():
    """when_in_context: comment — a goal-id in a NON-comment code line stays."""
    line = "GOAL = 'g-115-2397'\n"  # no leading/inline # → not a comment
    assert _strip(line) == line


def test_g13_pattern_is_canonical_goalid_regex():
    """Pin the pattern string itself to the canonical form so a future edit that
    re-narrows the second group is caught even if no strip case exercises it."""
    assert _g13_rule()["pattern"] == r"g-\d{3}-\d{2,4}"


# ---------------------------------------------------------------------------
# CODE-SPAN GUARD () — code + TRAILING comment on one line
#
# `test_g13_only_strips_in_comment_context` above pinned the no-`#`-at-all
# shape, which already passed. The uncovered shape was code AND a trailing
# comment on the SAME line: `_is_comment_line` calls that whole line a comment,
# so a whole-line sub deleted ids out of the CODE half too. Measured at hop 1 of
# the promotion chain (Ayoai-Mind -> Claude-Mind): 53 goal-ids deleted from
# executable code, 53/53 on a line containing '#' and 0 on a line without one,
# while 108 goal-ids on '#'-free lines in the same files survived.
#
# It stays silent because the damage still compiles: an assertion promoted as
# `== ["", "", ""]` is valid Python that simply asserts the wrong thing.
# ---------------------------------------------------------------------------

def _g14_rule() -> dict:
    """The live G14 (asp-NNN) rule — same engine, same guard, empty replacement."""
    manifest = yaml.safe_load(SEED_MANIFEST.read_text(encoding="utf-8"))
    for r in manifest.get("transformations", []):
        if r.get("id") == "G14":
            return r
    raise AssertionError("G14 rule not found in seed-manifest.yaml")


def test_g13_spares_code_half_of_a_line_with_trailing_comment():
    """The headline case, verbatim from the observed hop-1 damage in
    core/scripts/tests/test_coordination_merge.py:543. The ids in the assertion
    are CODE; only the trailing comment may be rewritten."""
    line = 'assert sorted(_goal_ids(ab)) == ["g-115-01", "g-115-02"]  # g-115-2147 re-id\n'
    out = _strip(line)
    assert '"g-115-01", "g-115-02"' in out, f"code half was mangled: {out!r}"
    assert "g-115-2147" not in out, f"comment half was not stripped: {out!r}"
    assert out == 'assert sorted(_goal_ids(ab)) == ["g-115-01", "g-115-02"]  #  re-id\n'


def test_g13_spares_code_when_comment_holds_no_goalid():
    """A trailing comment with nothing to strip must leave the line byte-identical
    — the un-mangled form of the fixture lines that sat beside the damaged ones."""
    line = '    base = _goal("g-115-01", title="base")   # re-evict: no-op\n'
    assert _strip(line) == line


def test_g13_ignores_hash_inside_a_string_literal():
    """A `#` inside a string is not a comment marker, so the line is not comment
    context and nothing may be stripped. This is line 104 of THIS file's own
    source shape — one of the 53 lines the bug corrupted downstream."""
    line = "    line = \"GOAL = 'g-115-2397'\\n\"  # no leading/inline # -> not a comment\n"
    out = _strip(line)
    assert "g-115-2397" in out, f"id inside the string literal was deleted: {out!r}"


def test_g13_still_strips_a_full_line_comment():
    """No-regression: the ordinary full-line comment case keeps stripping."""
    assert _strip("# see g-115-2397 for the trace\n") == "# see  for the trace\n"


def test_g13_still_strips_inside_a_python_docstring():
    """No-regression for the `else` branch. A docstring line is genuine comment
    context that carries no bare `#`, so the span split cannot apply — it must
    fall back to a whole-line sub, not silently skip. Docstrings are where the
    known-positive hop-1 case (test_owncloud_baseline_stamp.py) took its hits."""
    line = '    """Baseline stamp behavior (g-115-1946)."""\n'
    out = _strip(line)
    assert "g-115-1946" not in out, f"docstring id was not stripped: {out!r}"


def test_g13_still_strips_inside_an_md_html_comment():
    """No-regression: .md has no bare-`#` comment, so `<!-- -->` lines also take
    the whole-line fallback. (A bare `#` in .md is a heading, not a comment.)"""
    line = "<!-- provenance: g-115-2397 -->\n"
    out = _strip(line, rel_path="core/config/conventions/foo.md")
    assert "g-115-2397" not in out, f"md html-comment id was not stripped: {out!r}"


def test_g13_spares_a_goalid_inside_a_string_with_an_in_string_hash():
    """Found by fresh-eyes review OF the code-span fix itself ().

    `_is_comment_line`'s relaxed scan is `re.search(r'\\s#', line)` — NOT
    quote-aware — so it calls this pure code line comment context. The
    quote-aware split then finds no REAL marker. The fallback must therefore
    leave the line alone: whole-line substituting here deleted the id from
    inside a string literal, which is the very class the fix exists to stop."""
    for line in (
        'msg = "step # g-115-0042 of the plan"\n',
        'assert d == {"k": "g-115-0042 # note"}\n',
    ):
        out = _strip(line)
        assert out == line, f"id deleted from a string literal: {out!r}"


def test_g13_still_strips_a_docstring_that_contains_a_hash():
    """The other side of the same branch. A docstring is genuine whole-line
    comment context and carries no bare '#' to split on, so it must still be
    stripped — including when the docstring text itself contains a '#', which
    is exactly the shape the in-string-hash guard above must not swallow."""
    line = '    """See # noqa handling for g-115-2397."""\n'
    out = _strip(line)
    assert "g-115-2397" not in out, f"docstring id was not stripped: {out!r}"


def test_g14_spares_a_production_config_default():
    """The one hop-1 hit that landed in PRODUCTION code rather than test data:
    core/scripts/inactivity-detector.py:43 promoted as DEFAULT_TARGET_ASP = "".
    G14 excludes core/scripts/tests/** but NOT production code, so the comment
    heuristic was the only thing standing between this constant and deletion."""
    line = 'DEFAULT_TARGET_ASP = "asp-001"   # framework-maintenance home\n'
    out = _st.apply_global_regex(line, _g14_rule(), "core/scripts/inactivity-detector.py")
    assert '"asp-001"' in out, f"production constant was deleted: {out!r}"
    assert out == line
