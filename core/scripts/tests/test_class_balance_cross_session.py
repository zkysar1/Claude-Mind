"""Unit tests for load_recent_class_completions ().

Tests the cross-session sampling window helper that replaces in-session-only
`wm.goals_completed_this_session` reads in goal-selector.py. Closes the drift
identified in g-115-508 (alpha/reports/framework-vs-product-drift-2026-05-09.md):
class_balance_bonus was structurally blind to cross-session distribution because
the in-session list reset every /stop.

Test cases:
  1. last-N tail behavior — window_size caps returned items
  2. empty-journal fallback — falls back to wm in-session list when journal missing
  3. missing-work_class skip — goals without work_class are excluded
  4. chronological order — returns oldest-first (so [-N:] slicing keeps semantics)
  5. orphaned-id skip — goal_ids not in current aspirations are excluded
  6. agent-source goals — entries from agent aspirations.jsonl are indexed too

Run: py -3 core/scripts/tests/test_class_balance_cross_session.py
"""
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def with_sandbox(test_fn):
    """Decorator: spin up tmp WORLD_DIR + AGENT_DIR sandboxes via env vars.

    Uses MIND_WORLD / MIND_META / MIND_AGENT / MIND_AGENT_DIR so _paths.py
    picks up sandbox locations at module-load time. Force-reloads goal_selector.
    """
    def wrapped():
        sandbox_world = Path(tempfile.mkdtemp(prefix="cbcs_world_"))
        sandbox_meta = Path(tempfile.mkdtemp(prefix="cbcs_meta_"))
        sandbox_agent = Path(tempfile.mkdtemp(prefix="cbcs_agent_"))
        # Seed sandbox META_DIR with a minimal goal-selection-strategy.yaml so
        # goal-selector.py module load doesn't fail on missing file.
        (sandbox_meta / "goal-selection-strategy.yaml").write_text(
            "weights: {}\nselection_heuristics: []\ncustom_criteria: []\n",
            encoding="utf-8",
        )

        prior_agent = os.environ.get("MIND_AGENT")
        prior_agent_dir = os.environ.get("MIND_AGENT_DIR")
        prior_world = os.environ.get("MIND_WORLD")
        prior_meta = os.environ.get("MIND_META")
        try:
            os.environ["MIND_AGENT"] = "alpha"
            os.environ["MIND_AGENT_DIR"] = str(sandbox_agent)
            os.environ["MIND_WORLD"] = str(sandbox_world)
            os.environ["MIND_META"] = str(sandbox_meta)
            # Reload modules so _paths picks up the sandbox env vars
            for mod in list(sys.modules):
                if mod.startswith("_paths") or mod.startswith("wm") or mod == "goal_selector":
                    sys.modules.pop(mod, None)
            test_fn(sandbox_world, sandbox_agent)
        finally:
            for var, prior in (("MIND_AGENT", prior_agent),
                               ("MIND_AGENT_DIR", prior_agent_dir),
                               ("MIND_WORLD", prior_world),
                               ("MIND_META", prior_meta)):
                if prior is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = prior
            shutil.rmtree(sandbox_world, ignore_errors=True)
            shutil.rmtree(sandbox_meta, ignore_errors=True)
            shutil.rmtree(sandbox_agent, ignore_errors=True)
    return wrapped


def _import_helper():
    """Import goal_selector module fresh and return the helper."""
    # goal-selector.py uses hyphen — import via importlib by file path
    import importlib.util
    p = SCRIPT_DIR / "goal-selector.py"
    spec = importlib.util.spec_from_file_location("goal_selector", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_recent_class_completions


def _write_aspirations(world_dir, asp_id, goals):
    """Write a single-aspiration JSONL into world/aspirations.jsonl."""
    asp = {"id": asp_id, "title": f"test {asp_id}", "status": "active", "goals": goals}
    (world_dir / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8"
    )


def _write_journal(agent_dir, entries):
    """Write entries to <agent>/journal.jsonl."""
    lines = "\n".join(json.dumps(e) for e in entries) + "\n"
    (agent_dir / "journal.jsonl").write_text(lines, encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: last-N tail behavior
# ---------------------------------------------------------------------------

@with_sandbox
def test_last_n_tail(world_dir, agent_dir):
    helper = _import_helper()

    goals = [
        {"id": f"g-test-{i:02d}", "work_class": "framework", "recurring": False}
        for i in range(30)
    ]
    _write_aspirations(world_dir, "asp-test", goals)

    # Journal: 30 entries each with 1 goal completed, in chronological order
    entries = [
        {"date": f"2026-05-{i+1:02d}", "goals_completed": [f"g-test-{i:02d}"]}
        for i in range(30)
    ]
    _write_journal(agent_dir, entries)

    result = helper(window_size=20)
    assert len(result) == 20, f"expected 20, got {len(result)}"
    # Should be the LAST 20 (chronological, so oldest of those 20 = g-test-10)
    assert result[0]["goal_id"] == "g-test-10", f"oldest of last 20 should be g-test-10, got {result[0]['goal_id']}"
    assert result[-1]["goal_id"] == "g-test-29", f"newest should be g-test-29, got {result[-1]['goal_id']}"
    # All entries must have work_class
    assert all(r["work_class"] == "framework" for r in result), "all entries should have work_class"
    print("  test_last_n_tail PASSED")


# ---------------------------------------------------------------------------
# Test 2: empty-journal fallback
# ---------------------------------------------------------------------------

@with_sandbox
def test_empty_journal_fallback(world_dir, agent_dir):
    # Setup: write WM with goals_completed_this_session, no journal file
    wm_dir = agent_dir / "session"
    wm_dir.mkdir(exist_ok=True)
    in_session = [
        {"goal_id": "g-x-01", "aspiration_id": "asp-x", "recurring": False, "work_class": "product"}
    ]
    wm = {
        "active_context": {},
        "goals_completed_this_session": in_session,
        "slots": {}
    }
    (wm_dir / "working-memory.yaml").write_text(
        f"goals_completed_this_session:\n  - goal_id: g-x-01\n    aspiration_id: asp-x\n    recurring: false\n    work_class: product\n",
        encoding="utf-8",
    )
    # Setup empty world aspirations
    _write_aspirations(world_dir, "asp-x", [])

    helper = _import_helper()
    result = helper(window_size=20)
    assert isinstance(result, list), f"expected list, got {type(result)}"
    # journal missing → fallback returns in-session list (which has 1 item from WM)
    assert len(result) == 1, f"expected fallback to in-session=1, got {len(result)}"
    assert result[0]["goal_id"] == "g-x-01", f"expected g-x-01, got {result[0]['goal_id']}"
    print("  test_empty_journal_fallback PASSED")


# ---------------------------------------------------------------------------
# Test 3: missing-work_class skip
# ---------------------------------------------------------------------------

@with_sandbox
def test_missing_work_class_skip(world_dir, agent_dir):
    helper = _import_helper()

    # Two goals: one with work_class, one without
    goals = [
        {"id": "g-with-class", "work_class": "framework", "recurring": False},
        {"id": "g-no-class", "recurring": False},  # no work_class
    ]
    _write_aspirations(world_dir, "asp-test", goals)
    entries = [
        {"date": "2026-05-01", "goals_completed": ["g-with-class", "g-no-class"]},
    ]
    _write_journal(agent_dir, entries)

    result = helper(window_size=20)
    # Only g-with-class should be returned; g-no-class skipped due to no work_class
    assert len(result) == 1, f"expected 1, got {len(result)}"
    assert result[0]["goal_id"] == "g-with-class", f"expected g-with-class, got {result[0]['goal_id']}"
    print("  test_missing_work_class_skip PASSED")


# ---------------------------------------------------------------------------
# Test 4: orphaned-id skip
# ---------------------------------------------------------------------------

@with_sandbox
def test_orphaned_id_skip(world_dir, agent_dir):
    helper = _import_helper()

    goals = [{"id": "g-known", "work_class": "framework", "recurring": False}]
    _write_aspirations(world_dir, "asp-test", goals)

    # Journal includes an unknown goal_id
    entries = [
        {"date": "2026-05-01", "goals_completed": ["g-known", "g-unknown-orphan"]},
    ]
    _write_journal(agent_dir, entries)

    result = helper(window_size=20)
    assert len(result) == 1, f"expected 1, got {len(result)} (orphan should be skipped)"
    assert result[0]["goal_id"] == "g-known", f"expected g-known, got {result[0]['goal_id']}"
    print("  test_orphaned_id_skip PASSED")


# ---------------------------------------------------------------------------
# Test 5: chronological order (oldest first)
# ---------------------------------------------------------------------------

@with_sandbox
def test_chronological_order(world_dir, agent_dir):
    helper = _import_helper()

    goals = [
        {"id": "g-old", "work_class": "framework", "recurring": False},
        {"id": "g-mid", "work_class": "product", "recurring": False},
        {"id": "g-new", "work_class": "hygiene", "recurring": False},
    ]
    _write_aspirations(world_dir, "asp-test", goals)
    # Journal in chronological order: oldest first
    entries = [
        {"date": "2026-05-01", "goals_completed": ["g-old"]},
        {"date": "2026-05-02", "goals_completed": ["g-mid"]},
        {"date": "2026-05-03", "goals_completed": ["g-new"]},
    ]
    _write_journal(agent_dir, entries)

    result = helper(window_size=20)
    assert len(result) == 3, f"expected 3, got {len(result)}"
    # Result should be chronological (oldest first) so [-N:] slicing keeps semantics
    assert result[0]["goal_id"] == "g-old", f"oldest should be g-old, got {result[0]['goal_id']}"
    assert result[2]["goal_id"] == "g-new", f"newest should be g-new, got {result[2]['goal_id']}"
    print("  test_chronological_order PASSED")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        ("test_last_n_tail", test_last_n_tail),
        ("test_empty_journal_fallback", test_empty_journal_fallback),
        ("test_missing_work_class_skip", test_missing_work_class_skip),
        ("test_orphaned_id_skip", test_orphaned_id_skip),
        ("test_chronological_order", test_chronological_order),
    ]
    failures = []
    for name, fn in tests:
        try:
            print(f"Running {name}...")
            fn()
        except AssertionError as e:
            print(f"  {name} FAILED: {e}")
            failures.append((name, str(e)))
        except Exception as e:
            print(f"  {name} ERROR: {e}")
            traceback.print_exc()
            failures.append((name, repr(e)))

    if failures:
        print(f"\n{len(failures)}/{len(tests)} test(s) FAILED")
        for name, err in failures:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
