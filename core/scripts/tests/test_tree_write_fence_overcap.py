"""Over-cap write advisory on the tree write fence ( remedy).

The finding this remedies: one-off tree-node fold goals do not hold — a folded
node re-grows because every agent's loop appends to it and NOTHING tells the
writer, at the moment of writing, that the node is already unreadable
(measured: vinheim-web-stack re-grew 26.9KB -> 80KB after its split; four
over-cap nodes grew +16,470 B in one overnight window while zero fold goals
were selected fleet-wide). The advisory converts every source event (a touch
of an over-cap node) into a visible obligation at the only chokepoint that
fires for every LLM tree write: the fence's PostToolUse `record`.

Three test families:

  1. CONSTANT PINS — the fence deliberately carries copies of the detector's
     constants (importing tree.py + yaml onto the hook hot path was rejected).
     A pinned copy is only safe with a test asserting equality with the SSOT,
     the same pattern test_goal_claim_commit_gate.py uses for STALE_GRACE.
  2. BOTH-BUCKET behavior — guard-4374: code that sorts items into two buckets
     and reports from one needs a discriminating control in EACH bucket,
     asserted on the REPORTED artifact (the verdict field / stderr banner),
     not on a helper. A mutant that classifies everything (or nothing) as
     over-cap must fail these.
  3. CHANNEL — the banner goes to stderr and the stdout JSON verdict stays
     parseable (the wrapper discards stdout; stderr is what surfaces).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tree_write_fence as F  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent.parent
NODE_REL = "knowledge/tree/system/overcap-demo.md"

# > 25k tokens at 2.3 B/tok needs > 57,500 bytes. 80,000 is comfortably over
# while staying cheap to write; 500 bytes is comfortably under.
OVER_BYTES = 80_000
UNDER_BYTES = 500


def _mknode(tmp_path, n_bytes):
    p = tmp_path / NODE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "---\ntopic: overcap-demo\n---\n\n# Overcap Demo\n\n"
    body += "x" * max(0, n_bytes - len(body))
    p.write_text(body, encoding="utf-8")
    return p


# ------------------------------------------------------------- constant pins


def test_chars_per_token_pins_tree_py():
    """Fence ratio == tree.py CHARS_PER_TOKEN (textual read — importing tree.py
    pulls yaml + its full module init, which these tests should not need)."""
    src = (REPO / "core/scripts/tree.py").read_text(encoding="utf-8")
    m = re.search(r"^CHARS_PER_TOKEN\s*=\s*([0-9.]+)", src, re.MULTILINE)
    assert m, "tree.py no longer defines CHARS_PER_TOKEN at module level"
    assert float(m.group(1)) == F.CHARS_PER_TOKEN, (
        "tree_write_fence.CHARS_PER_TOKEN (%s) drifted from tree.py (%s) — "
        "the advisory and the detector no longer agree on what a token is"
        % (F.CHARS_PER_TOKEN, m.group(1)))


def test_read_cap_pins_tree_yaml():
    """Fence cap == tree.yaml pruning.distill_token_cap (the detector's SSOT)."""
    src = (REPO / "core/config/tree.yaml").read_text(encoding="utf-8")
    m = re.search(r"^\s*distill_token_cap:\s*(\d+)", src, re.MULTILINE)
    assert m, "tree.yaml no longer defines pruning.distill_token_cap"
    assert int(m.group(1)) == F.READ_CAP_TOKENS, (
        "tree_write_fence.READ_CAP_TOKENS (%s) drifted from tree.yaml (%s)"
        % (F.READ_CAP_TOKENS, m.group(1)))


# ------------------------------------------------- both-bucket behavior


def test_record_flags_over_cap_node(tmp_path):
    node = _mknode(tmp_path, OVER_BYTES)
    out = F.record(node, tmp_path / "baselines")
    assert out["recorded"] is True
    oc = out.get("over_cap")
    assert oc, "over-cap node produced no over_cap verdict — advisory is dead"
    assert oc["cap"] == F.READ_CAP_TOKENS
    # est must be the getsize/ratio arithmetic, not a constant someone pinned
    assert oc["est_tokens"] == int(os.path.getsize(node) / F.CHARS_PER_TOKEN)
    assert oc["est_tokens"] > F.READ_CAP_TOKENS


def test_record_stays_silent_under_cap(tmp_path):
    """The discriminating control (guard-4374): without this, a mutant that
    marks EVERY node over_cap keeps the suite green while the banner becomes
    wallpaper on every tree touch fleet-wide."""
    node = _mknode(tmp_path, UNDER_BYTES)
    out = F.record(node, tmp_path / "baselines")
    assert out["recorded"] is True
    assert "over_cap" not in out, (
        "under-cap node flagged over_cap — the advisory fires on everything")


def test_boundary_is_strictly_greater_than_cap(tmp_path):
    """At exactly the cap the node still reads whole — no banner.

    The byte count is SEARCHED, not computed as int(cap * ratio): 25000 * 2.3
    is 57499.999... in binary float, so the closed-form lands the node at est
    24,999 — one UNDER the boundary it claims to pin — and a `>` -> `>=` mutant
    survives. Measured 2026-08-19: this test's first version did exactly that.
    """
    at_cap_bytes = next(
        b for b in range(50_000, 70_000)
        if int(b / F.CHARS_PER_TOKEN) == F.READ_CAP_TOKENS)
    node = _mknode(tmp_path, at_cap_bytes)
    assert F.est_tokens(node) == F.READ_CAP_TOKENS  # the fixture IS at the cap
    out = F.record(node, tmp_path / "baselines")
    assert "over_cap" not in out


def test_out_of_scope_never_flags(tmp_path):
    p = tmp_path / "not-tree" / "big.md"
    p.parent.mkdir(parents=True)
    p.write_text("x" * OVER_BYTES, encoding="utf-8")
    out = F.record(p, tmp_path / "baselines")
    assert out["scoped"] is False and "over_cap" not in out


def test_est_tokens_unreadable_is_zero(tmp_path):
    assert F.est_tokens(tmp_path / "absent.md") == 0


# --------------------------------------------------------------- channel


def _run_main(node, agent_dir, op="record"):
    env = dict(os.environ)
    env["MIND_AGENT"] = "testagent"
    # _paths resolution is irrelevant here: main() only needs _default_paths to
    # return SOMETHING; a broken resolve returns (None, None) and main exits 0
    # silently, which would make this test vacuously green. Assert stdout
    # non-empty below to keep it honest.
    r = subprocess.run(
        [sys.executable, str(REPO / "core/scripts/tree_write_fence.py"),
         op, str(node)],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    return r


def test_banner_on_stderr_json_on_stdout(tmp_path):
    node = _mknode(tmp_path, OVER_BYTES)
    r = _run_main(node, tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip(), (
        "main() printed nothing — _default_paths failed and the run was "
        "vacuous; this test proves nothing about the banner")
    verdict = json.loads(r.stdout.strip().splitlines()[-1])
    assert verdict.get("over_cap"), "stdout verdict lost the over_cap field"
    assert "OVER-CAP" in r.stderr, "banner missing from stderr"
    assert str(node) in r.stderr
    # both consequences named — truncated reads AND deepening appends — so the
    # reader is told why it matters at either firing moment (Read or Edit)
    assert "TRUNCATED" in r.stderr
    assert "fold" in r.stderr.lower()


def test_no_banner_on_stderr_for_small_node(tmp_path):
    node = _mknode(tmp_path, UNDER_BYTES)
    r = _run_main(node, tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip(), "vacuous run — see test_banner_on_stderr note"
    assert "OVER-CAP" not in r.stderr
