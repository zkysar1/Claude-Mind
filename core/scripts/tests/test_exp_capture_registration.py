""" — exp_capture WM slot registration (worker->reducer experience bridge).

SCOPE, and why it is narrower than test_spark_capture_bridge.py.

The TRANSPORT (body-merge fork -> capture -> generalize-down, and its
content-hash dedup) is slot-AGNOSTIC: `_dedup_append` keys off ARRAY_SLOTS
membership, so it treats exp_capture exactly as it treats spark_capture, and
test_spark_capture_bridge.py already proves that machinery end to end.
Re-deriving it here would add a second copy of a proof that cannot fail
independently -- a test whose only failure mode is the other file's failure.

What is genuinely NEW for exp_capture, and therefore what this file pins, is the
REGISTRATION at each of the four hand-mirrored sync sites plus the config, and
the two behavioural survival properties that membership buys. Those CAN fail
independently: each is a separate hand-edit in a separate file.

The dedup case IS re-derived, deliberately (see its docstring) -- it is the one
transport behaviour whose correctness depends on exp_capture's OWN entry shape
rather than on the shared machinery.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CORE_SCRIPTS = Path(__file__).resolve().parent.parent          # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

SLOT = "exp_capture"
SIBLING = "spark_capture"


def _entry(goal_id: str, execution_summary: str,
           outcome_class: str = "deep",
           key_decisions=None, surprise_level: int = 5,
           verbatim_anchors=None,
           category: str = "cross-box-bodies",
           ts: str = "2026-08-07T00:00:00") -> dict:
    """One exp_capture item in the shape  specifies.

    `_item_ts` is present because the append endpoint stamps every dict item --
    a fixture without it would exercise a shape production never produces
    (guard-920: replicate the literal production arg shape).
    """
    return {
        "goal_id": goal_id,
        "category": category,
        "execution_summary": execution_summary,
        "outcome_class": outcome_class,
        "key_decisions": key_decisions if key_decisions is not None else [],
        "surprise_level": surprise_level,
        "verbatim_anchors": verbatim_anchors if verbatim_anchors is not None else [],
        "_item_ts": ts,
    }


def _daemon_constant(name: str) -> set:
    """AST-read a constant from the daemon endpoint without importing it (the
    module pulls in server-side deps this test does not need)."""
    src = (PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "wm_write.py").read_text(
        encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in the daemon mirror")


# ---------------------------------------------------------------------------
# 1. MEMBERSHIP at all four hand-mirrored sync sites
# ---------------------------------------------------------------------------

def test_membership_present_on_both_sides():
    """wm-append.sh / wm-reset.sh are DAEMON-ONLY, so the daemon copy is the LIVE
    path -- a CLI-only edit would leave the bridge inert in production while every
    CLI-level test in this file still passed (guard-2323). Four sites: two
    constants x two files, each a separate hand-edit."""
    for const in ("ARRAY_SLOTS", "RESET_SURVIVING_SLOTS"):
        assert SLOT in getattr(wm, const), f"wm.py {const} lost {SLOT} (g-306-199)"
        assert SLOT in _daemon_constant(const), (
            f"daemon wm_write.py {const} lost {SLOT} -- the LIVE wm-append/"
            f"wm-reset path would diverge from the CLI (g-306-199)")


def test_registered_alongside_its_sibling_not_instead_of_it():
    """Anti-vacuity (guard-1220): every assertion above passes if someone RENAMES
    spark_capture to exp_capture rather than ADDING it. The bridge would then be
    'registered' and the spark lane silently dead. Both must be present."""
    for const in ("ARRAY_SLOTS", "RESET_SURVIVING_SLOTS"):
        cli, daemon = getattr(wm, const), _daemon_constant(const)
        assert {SLOT, SIBLING} <= cli, (
            f"wm.py {const} must carry BOTH capture slots -- exp_capture is an "
            f"addition, not a replacement (g-306-199)")
        assert {SLOT, SIBLING} <= daemon, (
            f"daemon {const} must carry BOTH capture slots (g-306-199)")


# ---------------------------------------------------------------------------
# 2. SURVIVAL properties that membership buys
# ---------------------------------------------------------------------------

def test_reset_preserves_exp_capture():
    """body-merge delivers at consolidate Step -1; wm-reset runs at Step 5 of the
    SAME run. Without the RESET_SURVIVING_SLOTS exemption the narratives are wiped
    ~5 steps after arriving, before the retrospective could encode any experience
    .md from them -- the g-306-176 failure, verbatim, on the experience lane."""
    items = [_entry("g-306-199", "must outlive the Step-5 reset")]
    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpdir:
        # BODY_WM_PATH is the ONLY correct redirect -- patching wm.WM_PATH is a
        # no-op for I/O and would target the live agent's WM (guard-862).
        os.environ["BODY_WM_PATH"] = str(Path(tmpdir) / "working-memory.yaml")
        try:
            wm.cmd_init(SimpleNamespace())
            data = wm.read_wm()
            data["slots"][SLOT] = items
            wm.write_wm(data)

            wm.cmd_reset(SimpleNamespace())

            after = wm.read_wm()
            assert after["slots"].get(SLOT) == items, (
                "wm-reset wiped exp_capture -- consolidate Step 5 would destroy "
                "what Step -1 delivered in the same run (g-306-199)")
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original


def test_prune_does_not_evict_stale_populated_capture():
    """The scalar-eviction predicate is `slot_name not in ARRAY_SLOTS and ...
    slot_val is not None`, and a non-empty list is not None. A worker Body that
    executes a goal and then waits >120min for the reducer to consolidate would
    otherwise lose every narrative it captured."""
    items = [_entry("g-306-199-b", "captured long before the reducer consolidated")]
    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["BODY_WM_PATH"] = str(Path(tmpdir) / "working-memory.yaml")
        try:
            wm.cmd_init(SimpleNamespace())
            data = wm.read_wm()
            data["slots"][SLOT] = items
            # Stamp the slot far past evict_threshold_minutes (120).
            data.setdefault("slot_meta", {})[SLOT] = {
                "updated_at": "2020-01-01T00:00:00",
                "accessed_at": "2020-01-01T00:00:00",
                "update_count": 1,
            }
            wm.write_wm(data)

            wm.cmd_prune(SimpleNamespace(dry_run=False, json=True))

            after = wm.read_wm()
            assert after["slots"].get(SLOT) == items, (
                "wm-prune evicted a populated exp_capture -- ARRAY_SLOTS "
                "membership is what prevents this (g-306-199)")
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original


# ---------------------------------------------------------------------------
# 3. BOUND
# ---------------------------------------------------------------------------

def test_array_limit_is_configured_and_tighter_than_the_spark_lane():
    """The slot is reset-surviving, so a window where the retrospective never runs
    would otherwise grow it without bound. The cap is deliberately BELOW
    spark_capture's: an exp_capture entry carries an execution narrative plus
    verbatim_anchors (several times a spark's size) and one is written per
    EXECUTED goal rather than per spark-worthy insight, which is often zero."""
    import yaml
    cfg = yaml.safe_load(
        (PROJECT_ROOT / "core" / "config" / "memory-pipeline.yaml").read_text(
            encoding="utf-8"))
    limits = (cfg.get("working_memory_pruning") or {}).get("array_limits") or {}
    assert isinstance(limits.get(SLOT), int) and limits[SLOT] > 0, (
        "exp_capture has no array_limits entry -- a reset-surviving slot with no "
        "cap grows the WM without bound (g-306-199)")
    assert limits[SLOT] <= limits.get(SIBLING, limits[SLOT]), (
        "exp_capture's cap must not exceed spark_capture's -- its entries are "
        "larger and are written per executed goal (g-306-199)")


# ---------------------------------------------------------------------------
# 4. DEDUP DISTINCTNESS -- re-derived here on purpose
# ---------------------------------------------------------------------------

def test_identical_narratives_from_distinct_goals_both_survive():
    """body-merge unions array slots by CONTENT HASH. Two goals whose narratives
    read identically -- routine, plausible for repetitive work like a sweep run
    twice -- would collapse into one entry and silently lose the second goal's
    experience.

    This IS covered generically for spark_capture, and is re-derived here rather
    than inherited because the property depends on exp_capture's OWN entry shape:
    it holds only because goal_id is a REQUIRED field that varies. A future edit
    making goal_id optional, or encoding it outside the hashed body, would break
    this for exp_capture while leaving the spark lane's proof green.
    """
    merge_mod = _load_merge()
    a = _entry("g-306-500", "ran the sweep; nothing found", outcome_class="routine")
    b = _entry("g-306-501", "ran the sweep; nothing found", outcome_class="routine")
    assert merge_mod._content_hash(a) != merge_mod._content_hash(b), (
        "identical narratives from distinct goals hash the same -- the second "
        "goal's experience would vanish at generalize-down (g-306-199)")
    merged = merge_mod._dedup_append([a], [b])
    assert merged == [a, b], (
        "dedup dropped a distinct-goal entry with an identical narrative")

    # Positive control: a byte-identical entry MUST still dedup, or the test
    # above would pass under a hash function that never collides at all.
    assert merge_mod._dedup_append([a], [dict(a)]) == [a], (
        "byte-identical entries no longer dedup -- the distinctness assertion "
        "above proves nothing without this control")


def _load_merge():
    """Import body-merge.py by path (`body-merge` is not a legal module name)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "body_merge_exp", CORE_SCRIPTS / "body-merge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["body_merge_exp"] = mod
    spec.loader.exec_module(mod)
    return mod
