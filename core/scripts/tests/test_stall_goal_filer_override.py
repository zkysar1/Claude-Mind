"""test_stall_goal_filer_override.py - regression test for .

Verifies that stall-goal-filer.py's file_goal() forwards override_just to the
daemon's add-goal endpoint via the `overrides={"Duplication": ...}` argument
of `_rt.aspirations_add_goal`. Without this, goal-duplication-gate over-fires
on stall warnings whose context keywords overlap with recent non-self
completions, silently consuming the Unblock that should have been filed.

Source: bravo finding msg-20260509-044142-bravo-875 (framework-gap, actionable):
"stall-goal-filer.sh ran cleanly but failed to file 1 of 1 stall warnings due
to goal-duplication-gate refusal (overlap with 6 recent non-self completions)."

REWRITTEN 2026-05-17 (g-115-886): the prior subprocess.run-mocking shape was
broken by H2 Wave 2 daemon cutover — stall-goal-filer.py now calls
_rt.aspirations_add_goal directly (no subprocess). Tests now patch
mod._rt.aspirations_add_goal and assert on the (asp_id, goal, source,
overrides) call signature. Daemon response shape is the dict
{"ok": True, "goal_id": "...", "aspiration_id": "...", "source": "...",
"goal": {...}} returned by mind_api/src/endpoints/aspirations_write.py
add_goal handler.

Cases covered:
  1. file_goal(asp, goal) -> overrides=None (not passed)
  2. file_goal(asp, goal, just) -> overrides={"Duplication": just}
  3. file_goal happy path -> returns goal_id from top-level daemon response
     (NOT nested goal.id — rb-1041 defensive shape, same as
      blocker-recheck.py:166 / cargo-cult-detector.py:227)
  4. file_goal _rt.RtError -> returns None (failure path unchanged)
  5. (existing) main_loop_constructs_override_just_from_warning -> end-to-end

Run: py -3 -m pytest core/scripts/tests/test_stall_goal_filer_override.py -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load_filer_module():
    """Load stall-goal-filer.py by spec because the filename has hyphens."""
    spec = importlib.util.spec_from_file_location(
        "stall_goal_filer",
        CORE_SCRIPTS / "stall-goal-filer.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load stall-goal-filer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _daemon_add_goal_response(goal_id: str, asp_id: str = "asp-240"):
    """Shape returned by mind_api/src/endpoints/aspirations_write.py add_goal."""
    return {
        "ok": True,
        "goal_id": goal_id,
        "aspiration_id": asp_id,
        "source": "world",
        "goal": {"id": goal_id, "title": "x", "status": "pending"},
    }


def test_no_override_omits_flag():
    """Case 1: file_goal without override_just must call _rt.aspirations_add_goal
    with overrides=None (or omitted) so the daemon receives no
    X-Mind-Override-Duplication header."""
    mod = _load_filer_module()
    captured_calls = []

    def fake_add_goal(asp_id, goal, source="world", overrides=None):
        captured_calls.append({"asp_id": asp_id, "goal": goal,
                                "source": source, "overrides": overrides})
        return _daemon_add_goal_response("g-test-001", asp_id)

    with patch.object(mod._rt, "aspirations_add_goal", side_effect=fake_add_goal):
        gid = mod.file_goal("asp-240", {"title": "x"})

    assert gid == "g-test-001", f"expected g-test-001, got {gid!r}"
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["overrides"] is None, (
        f"file_goal without override_just must pass overrides=None; "
        f"got overrides={call['overrides']!r}"
    )
    assert call["asp_id"] == "asp-240"
    assert call["source"] == "world"


def test_override_appends_flag_and_justification():
    """Case 2: file_goal with override_just must pass
    overrides={"Duplication": <just>} so _rt.aspirations_add_goal emits the
    X-Mind-Override-Duplication header."""
    mod = _load_filer_module()
    captured_calls = []

    def fake_add_goal(asp_id, goal, source="world", overrides=None):
        captured_calls.append({"asp_id": asp_id, "goal": goal,
                                "source": source, "overrides": overrides})
        return _daemon_add_goal_response("g-test-002", asp_id)

    just = "stall-warning stall:sid-X:2026-05-09T04:12:28: g-115-487 audit"
    with patch.object(mod._rt, "aspirations_add_goal", side_effect=fake_add_goal):
        gid = mod.file_goal("asp-240", {"title": "x"}, just)

    assert gid == "g-test-002", f"expected g-test-002, got {gid!r}"
    call = captured_calls[0]
    assert call["overrides"] == {"Duplication": just}, (
        f"file_goal with override_just must pass overrides={{'Duplication': just}}; "
        f"got overrides={call['overrides']!r}"
    )


def test_override_passes_through_failure_path():
    """Case 3: _rt.RtError with override_just still returns None.

    The override forwarding must not change failure semantics — a real
    add-goal failure (daemon refusal, network error) still yields None so
    the caller can surface the framework break.
    """
    mod = _load_filer_module()

    def raise_rt_error(asp_id, goal, source="world", overrides=None):
        raise mod._rt.RtError("add-goal: aspiration not found")

    captured_stderr = io.StringIO()
    with patch.object(mod._rt, "aspirations_add_goal", side_effect=raise_rt_error), \
         patch.object(mod.sys, "stderr", captured_stderr):
        gid = mod.file_goal("asp-NONEXISTENT", {"title": "x"}, "any justification")

    assert gid is None, f"expected None on RtError, got {gid!r}"
    assert "add-goal failed" in captured_stderr.getvalue(), (
        f"failure should log to stderr; got {captured_stderr.getvalue()!r}"
    )


def test_returns_top_level_goal_id_not_nested():
    """Case 3b: file_goal returns the TOP-LEVEL goal_id field from the daemon
    response, NOT the nested goal.id.

    rb-1041 defensive shape: daemon returns
    {"ok": True, "goal_id": "g-...", ..., "goal": {"id": "g-...", ...}}.
    A naive record.get("id") returns None on every success because "id" is
    only nested. Same bug pattern as cargo-cult-detector.py:227 +
    blocker-recheck.py:166 fixed elsewhere. This test pins the file_goal
    fallback chain (`record.get("goal_id") or record.get("id")`) so a future
    refactor cannot silently re-introduce the None-on-success regression
    that produces "failed to file goal" log spam for every successful
    stall warning.
    """
    mod = _load_filer_module()

    def fake_add_goal(asp_id, goal, source="world", overrides=None):
        # Top-level goal_id only — legacy nested "id" intentionally omitted.
        return {"ok": True, "goal_id": "g-test-003",
                "aspiration_id": asp_id, "source": source,
                "goal": {"title": "x"}}

    with patch.object(mod._rt, "aspirations_add_goal", side_effect=fake_add_goal):
        gid = mod.file_goal("asp-240", {"title": "x"}, "test-just")

    assert gid == "g-test-003", (
        f"file_goal must read top-level goal_id from daemon response; "
        f"got {gid!r} (probably reading legacy nested .id)"
    )


def test_main_loop_constructs_override_just_from_warning():
    """Case 4: main()'s call to file_goal builds override_just from the stall_tag.

    Smoke-tests the end-to-end wiring: a synthetic warning produces a non-empty
    override_just whose text references the stall_tag, agent name, and bravo
    finding (the audit trail breadcrumb). We monkey-patch file_goal to capture
    the third argument without writing anything.
    """
    mod = _load_filer_module()
    captured_args = []

    def fake_file_goal(asp_id, goal, override_just=None):
        captured_args.append((asp_id, goal, override_just))
        return "g-test-003"

    # Build minimal sandbox: synthetic warnings file in tmpdir,
    # asp-240 stub with empty goals list returned by read_asp,
    # and rate-limit/skip checks bypassed via monkey patches.
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="stall_filer_test_"))
    try:
        # Place a warnings file at <PROJECT_ROOT>/<agent>/session/loop-stall-warnings.jsonl
        # by setting MIND_AGENT and stubbing PROJECT_ROOT. Easier: stub read_asp
        # and the warning path directly via the module functions.
        warning = {
            "sid": "test-sid-XYZ",
            "first_block_ts": "2026-05-09T04:12:28",
            "last_block_ts": "2026-05-09T04:12:50",
            "consecutive_blocks": 4,
            "threshold": 4,
            "window_sec": 30,
            "detected_at": "2026-05-09T04:12:55",
        }
        warn_path = tmpdir / "alpha" / "session" / "loop-stall-warnings.jsonl"
        warn_path.parent.mkdir(parents=True, exist_ok=True)
        warn_path.write_text(json.dumps(warning) + "\n", encoding="utf-8")

        # Stub asp read to return empty asp-240
        empty_asp = {"id": "asp-240", "goals": []}
        # Stub PROJECT_ROOT and _paths.agent_dir so warn_path resolves to our sandbox
        with patch.object(mod, "PROJECT_ROOT", tmpdir), \
             patch.object(mod._paths, "agent_dir", side_effect=lambda name: tmpdir / name), \
             patch.object(mod, "read_asp", return_value=empty_asp), \
             patch.object(mod, "resolve_world_aspirations_path",
                          return_value=tmpdir / "world" / "aspirations.jsonl"), \
             patch.object(mod, "infer_last_goal", return_value="g-mock-99: stub"), \
             patch.object(mod, "rewrite_warnings"), \
             patch.object(mod, "file_goal", side_effect=fake_file_goal):
            # Force MIND_AGENT via argv
            old_argv = sys.argv
            sys.argv = ["stall-goal-filer.py", "--agent", "alpha"]
            try:
                rc = mod.main()
            finally:
                sys.argv = old_argv

        assert rc == 0, f"main() should exit 0, got {rc}"
        assert len(captured_args) == 1, (
            f"expected exactly 1 file_goal invocation, got {len(captured_args)}"
        )
        asp_id, goal, override_just = captured_args[0]
        assert asp_id == "asp-240"
        assert override_just is not None, (
            "main() must pass override_just to file_goal — without it, "
            "the duplication gate consumes stall warnings silently"
        )
        # Audit-trail breadcrumbs that callers expect to find in
        # world/goal-duplication-overrides.jsonl
        assert "stall-warning" in override_just, (
            f"override_just must mention 'stall-warning' for audit; got {override_just!r}"
        )
        assert "alpha" in override_just, (
            f"override_just must name the agent; got {override_just!r}"
        )
        assert "test-sid-XYZ" in override_just or "stall:test-sid-XYZ" in override_just, (
            f"override_just must reference the stall sid; got {override_just!r}"
        )
        assert "g-115-487" in override_just, (
            f"override_just must cite the implementing goal for traceability; "
            f"got {override_just!r}"
        )
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    tests = [
        ("no_override_omits_flag", test_no_override_omits_flag),
        ("override_appends_flag_and_justification", test_override_appends_flag_and_justification),
        ("override_passes_through_failure_path", test_override_passes_through_failure_path),
        ("main_loop_constructs_override_just_from_warning",
         test_main_loop_constructs_override_just_from_warning),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failed.append(name)

    if failed:
        print(f"\n{len(failed)}/{len(tests)} test(s) failed: {failed}")
        return 1
    print(f"\n{len(tests)}/{len(tests)} test(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
