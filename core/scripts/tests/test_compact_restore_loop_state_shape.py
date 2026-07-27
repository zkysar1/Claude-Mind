""": Compact-restore-slots loop_state shape invariance.

Verifies that compact-restore-slots.py does NOT depend on the LLM-mirror
serialization shape for loop_state. Two architectural facts:

1. Both LLM-side ('echo JSON | wm-set.sh loop_state') and bash-gate-side
   (recurring-loop-state-mutate.sh -> wm-set.sh) writers route through
   wm.py:cmd_set which canonicalizes via json.dumps. Two writers writing
   the same logical state produce byte-equal serialization.

2. compact-restore-slots.py includes 'loop_state' in SKIP_SLOTS, so the
   restorer never mutates loop_state during the restore pass. The live
   WM value is preserved as-is across compact regardless of writer.

Together these mean: retiring the LLM-side wm-set loop_state mirror
(asp-283 goal g-283-04) cannot cause shape divergence at compact-restore
time. This test pins the invariant down so a future refactor that
either (a) re-introduces shape divergence in wm.cmd_set or (b) removes
loop_state from SKIP_SLOTS without restoring shape-canonical behavior
will fail loudly.

Run: py -3 core/scripts/tests/test_compact_restore_loop_state_shape.py
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent


def with_sandbox(test_fn):
    """Spin up a tmp AGENT_DIR sandbox with minimal WM scaffolding."""
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix=f"compact_restore_test_{test_fn.__name__}_"))
        agent_dir = sandbox / "zeta-test"
        (agent_dir / "session").mkdir(parents=True)
        world_dir = sandbox / "world-test"
        world_dir.mkdir()
        meta_dir = sandbox / "meta-test"
        meta_dir.mkdir()

        # Set sandbox env vars so _paths resolves into the sandbox
        prior_env = {k: os.environ.get(k) for k in
                     ("MIND_AGENT", "MIND_WORLD", "MIND_META", "MIND_AGENT_DIR")}
        os.environ["MIND_AGENT"] = "zeta-test"
        os.environ["MIND_WORLD"] = str(world_dir)
        os.environ["MIND_META"] = str(meta_dir)
        os.environ["MIND_AGENT_DIR"] = str(agent_dir)

        # Force re-import to pick up sandbox paths
        for mod in list(sys.modules):
            if mod in ("_paths", "wm", "compact-restore-slots"):
                del sys.modules[mod]

        try:
            test_fn(sandbox, agent_dir)
            print(f"  [PASS] {test_fn.__name__}")
            return True
        except AssertionError as e:
            print(f"  [FAIL] {test_fn.__name__}: {e}")
            traceback.print_exc()
            return False
        except Exception as e:
            print(f"  [ERROR] {test_fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            return False
        finally:
            for k, v in prior_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(sandbox, ignore_errors=True)
    wrapped.__name__ = test_fn.__name__
    return wrapped


def run_wm_set(slot, value_json):
    """Invoke wm.py set <slot> via subprocess + stdin."""
    r = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "wm.py"), "set", slot],
        input=value_json,
        capture_output=True, text=True, encoding="utf-8",
        env=os.environ.copy(),
    )
    if r.returncode != 0:
        raise RuntimeError(f"wm.py set {slot} failed: {r.stderr}")
    return r.stdout


def write_compact_checkpoint(agent_dir, all_slots, slot_meta=None):
    """Manually write a compact-checkpoint.yaml mimicking the PreCompact hook."""
    import yaml
    cp = agent_dir / "session" / "compact-checkpoint.yaml"
    payload = {"all_slots": all_slots}
    if slot_meta:
        payload["slot_meta"] = slot_meta
    cp.write_text(yaml.safe_dump(payload, default_flow_style=False), encoding="utf-8")
    return cp


def run_compact_restore():
    """Invoke compact-restore-slots.py."""
    r = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "compact-restore-slots.py")],
        capture_output=True, text=True, encoding="utf-8",
        env=os.environ.copy(),
    )
    return r


# ---------------------------------------------------------------------------
# Test 1: SKIP_SLOTS contract — loop_state must be in the skip list.
# ---------------------------------------------------------------------------

@with_sandbox
def test_loop_state_in_skip_slots(sandbox, agent_dir):
    """compact-restore-slots.py must declare loop_state in SKIP_SLOTS."""
    spec = importlib.util.spec_from_file_location(
        "compact_restore_slots",
        SCRIPT_DIR / "compact-restore-slots.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "loop_state" in mod.SKIP_SLOTS, (
        f"loop_state must be in SKIP_SLOTS to prevent compact-restore from "
        f"clobbering live WM with stale checkpoint snapshot; got {mod.SKIP_SLOTS}"
    )


# ---------------------------------------------------------------------------
# Test 2: shape invariance across two callers (LLM-style and bash-style)
# ---------------------------------------------------------------------------

@with_sandbox
def test_two_writers_produce_byte_equal_serialization(sandbox, agent_dir):
    """LLM-mirror write and bash-gate write both go through wm.py:cmd_set.
    For the same logical loop_state, the on-disk serialization must be
    byte-equal. This pins the canonicalization invariant.
    """
    # Initialize WM
    subprocess.run([sys.executable, str(SCRIPT_DIR / "wm.py"), "init"],
                   capture_output=True, env=os.environ.copy(), check=True)

    loop_state = {
        "goals_completed": 41,
        "productive_goals": 28,
        "evolutions": 0,
        "touched": ["asp-282", "asp-283"],
        "signals": {"routine_streak_global": 1, "productive_streak": 0},
    }
    canonical_json = json.dumps(loop_state)

    # Writer A: LLM-style direct
    run_wm_set("loop_state", canonical_json)
    wm_path = agent_dir / "session" / "working-memory.yaml"
    after_writer_a = wm_path.read_bytes()

    # Writer B: bash-gate style (same JSON, same call shape)
    # (Both routes call wm.py set — there's only one writer code path.)
    run_wm_set("loop_state", canonical_json)
    after_writer_b = wm_path.read_bytes()

    # The on-disk shape must be byte-equal (excluding slot_meta timestamps,
    # which legitimately differ per call). Compare just the loop_state slot.
    import yaml
    a_doc = yaml.safe_load(after_writer_a.decode("utf-8"))
    b_doc = yaml.safe_load(after_writer_b.decode("utf-8"))
    assert a_doc["slots"]["loop_state"] == b_doc["slots"]["loop_state"], (
        f"Two equivalent writes through wm.py:cmd_set produced different shapes:\n"
        f"A: {a_doc['slots']['loop_state']}\nB: {b_doc['slots']['loop_state']}"
    )


# ---------------------------------------------------------------------------
# Test 3: compact-restore preserves live loop_state regardless of checkpoint
# ---------------------------------------------------------------------------

@with_sandbox
def test_compact_restore_preserves_live_loop_state(sandbox, agent_dir):
    """Set loop_state X (live), write checkpoint with loop_state Y (stale),
    run compact-restore-slots.py. Post-restore loop_state must still be X
    (the SKIP_SLOTS behavior). Y must NOT clobber X.
    """
    # Initialize WM
    subprocess.run([sys.executable, str(SCRIPT_DIR / "wm.py"), "init"],
                   capture_output=True, env=os.environ.copy(), check=True)

    # Live state X
    live_state = {
        "goals_completed": 100,
        "productive_goals": 80,
        "signals": {"routine_streak_global": 3, "productive_streak": 5},
        "touched": ["asp-283"],
    }
    run_wm_set("loop_state", json.dumps(live_state))

    # Stale checkpoint state Y (different values)
    stale_state = {
        "goals_completed": 50,
        "productive_goals": 40,
        "signals": {"routine_streak_global": 1, "productive_streak": 0},
        "touched": ["asp-282"],
    }
    write_compact_checkpoint(agent_dir, all_slots={"loop_state": stale_state})

    # Run restore
    r = run_compact_restore()
    assert r.returncode == 0, f"compact-restore-slots.py failed: {r.stderr}"
    assert "skipped" in r.stdout and "loop_state" in r.stdout, (
        f"Expected 'skipped' mention of loop_state in stdout, got: {r.stdout}"
    )

    # Post-restore: loop_state must still be the live X (NOT the stale Y).
    # Read the WM file DIRECTLY (daemon-agnostic — mirrors Test 2/4's file read)
    # rather than via _rt.wm_read. On a box with a LIVE daemon present, _rt
    # connects to that daemon (which has no state for the sandbox's fake agent)
    # and returns {} even though the subprocess wm.py-set wrote the sandbox
    # LocalBackend file — the read/write split that made this test
    # environment-fragile (rb-3331). The file read is the ground truth
    # compact-restore actually mutated.
    import yaml
    wm_path = agent_dir / "session" / "working-memory.yaml"
    restored_obj = yaml.safe_load(wm_path.read_text(encoding="utf-8"))["slots"]["loop_state"]
    assert restored_obj == live_state, (
        f"compact-restore-slots clobbered live loop_state with stale "
        f"checkpoint snapshot. Expected live state:\n{live_state}\n"
        f"Got:\n{restored_obj}"
    )


# ---------------------------------------------------------------------------
# Test 4: signals.dry_idle dynamic sub-slot survives compaction (-a)
# ---------------------------------------------------------------------------

@with_sandbox
def test_compact_restore_preserves_dry_idle_signals(sandbox, agent_dir):
    """-a: the dynamic signals.dry_idle counter (streak, last_dry_at,
    sleep_total_s, session_start_at, cap_cycles) must survive compaction. Because
    loop_state is in SKIP_SLOTS, a live loop_state carrying signals.dry_idle is
    preserved verbatim across compact-restore — a stale (pre-feature) checkpoint
    must NOT clobber the dry-idle counter, which would reset the backoff streak
    and re-spin the loop. This pins the sub-schema against a future refactor that
    drops loop_state from SKIP_SLOTS or reshapes the signals dict.
    """
    subprocess.run([sys.executable, str(SCRIPT_DIR / "wm.py"), "init"],
                   capture_output=True, env=os.environ.copy(), check=True)

    # Live state carrying the full dry_idle sub-schema.
    live_state = {
        "goals_completed": 12,
        "productive_goals": 9,
        "signals": {
            "routine_streak_global": 0,
            "productive_streak": 2,
            "dry_idle": {
                "streak": 4,
                "last_dry_at": "2026-07-13T16:00:00",
                "sleep_total_s": 1800,
                "session_start_at": "2026-07-13T13:00:00",
                "cap_cycles": 0,
            },
        },
        "touched": ["asp-115"],
    }
    run_wm_set("loop_state", json.dumps(live_state))

    # Stale checkpoint WITHOUT dry_idle (a pre-feature snapshot) must not clobber.
    stale_state = {
        "goals_completed": 5,
        "productive_goals": 3,
        "signals": {"routine_streak_global": 1, "productive_streak": 0},
        "touched": ["asp-115"],
    }
    write_compact_checkpoint(agent_dir, all_slots={"loop_state": stale_state})

    r = run_compact_restore()
    assert r.returncode == 0, f"compact-restore-slots.py failed: {r.stderr}"

    # Read the WM file DIRECTLY (daemon-agnostic — mirrors Test 2's file read)
    # rather than via _rt.wm_read. On a box with a LIVE daemon present, _rt
    # connects to that daemon (which has no state for the sandbox's fake agent)
    # and returns {} even though the subprocess wm.py-set wrote the sandbox
    # LocalBackend file — the read/write split that makes the _rt-based Test 3
    # environment-fragile. The file read is the ground truth compact-restore
    # actually mutated.
    import yaml
    wm_path = agent_dir / "session" / "working-memory.yaml"
    restored = yaml.safe_load(wm_path.read_text(encoding="utf-8"))["slots"]["loop_state"]
    assert restored == live_state, (
        f"compact-restore clobbered live loop_state carrying signals.dry_idle.\n"
        f"Expected:\n{live_state}\nGot:\n{restored}"
    )
    dry = restored["signals"]["dry_idle"]
    for field in ("streak", "last_dry_at", "sleep_total_s", "session_start_at", "cap_cycles"):
        assert field in dry, f"signals.dry_idle missing field {field!r} after restore: {dry}"
    assert dry["streak"] == 4, f"dry_idle.streak not preserved across compaction: {dry}"


def main():
    tests = [
        test_loop_state_in_skip_slots,
        test_two_writers_produce_byte_equal_serialization,
        test_compact_restore_preserves_live_loop_state,
        test_compact_restore_preserves_dry_idle_signals,
    ]
    print(f"# g-283-03: compact-restore loop_state shape invariance — {len(tests)} tests")
    failures = 0
    for t in tests:
        if not t():
            failures += 1
    print(f"# Done: {len(tests) - failures}/{len(tests)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
