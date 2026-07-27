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
    line = "GOAL = ''\n"  # no leading/inline # → not a comment
    assert _strip(line) == line


def test_g13_pattern_is_canonical_goalid_regex():
    """Pin the pattern string itself to the canonical form so a future edit that
    re-narrows the second group is caught even if no strip case exercises it."""
    assert _g13_rule()["pattern"] == r"g-\d{3}-\d{2,4}"
