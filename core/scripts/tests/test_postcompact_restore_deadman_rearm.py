"""Every compaction-resume banner must order the deadman re-arm FIRST (rb-4345).

A `SessionStart:compact` resume re-enters the aspirations loop MID-iteration,
so it reaches no terminal pair and runs the whole iteration on whatever
ScheduleWakeup net already existed — none, if the compaction landed before a
close. `.claude/rules/schedule-wakeup-correctness.md` and
`.claude/rules/return-protocol.md` both carry the rule, but a rule file is not
re-read on resume: the banner this script prints IS what the resuming model
reads. Measured silent loop deaths from the missing net: 7h (2026-07-19, cc-04)
and 7h47m (2026-08-11, cc-05); two more one-call lapses on alpha/cc-04
2026-09-11, which is what put the imperative here.

BOTH EMIT PATHS ARE PINNED, because they are reached by different failures and
a fix applied to one reads as applied to both (guard-4392). The degraded path
(no compact-checkpoint.yaml) is the one a crash-during-checkpoint produces —
exactly when the net is least likely to be armed.

ORDERING IS PART OF THE CONTRACT, not decoration: an imperative printed after
"resume the task" is read after the model has already reached for the task,
which is the observed failure. The re-arm line must precede the ACTION line.

Hermetic in the same shape as test_postcompact_restore_terminal_anchor.py:
`_paths.AGENT_DIR` / `_paths.WORLD_DIR` are patched to tmp dirs BEFORE the
module under test is imported, and env mutation happens in fixtures, never at
module level (guard-1165).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402

TARGET = CORE_SCRIPTS / "postcompact-restore.py"

SENTINEL = "<<autonomous-loop-dynamic>>"
HEADLINE = "MANDATORY FIRST CALL"

# Everything the imperative must OUTRANK. "ACTION:" alone was the whole list
# until 2026-09-13, and it was satisfied by a banner that buried the imperative
# ~85% down — below the goal anchor, loop state, the completed-goal list, the
# execution diary and the reasoning snapshot. Four consecutive compaction
# resumes on alpha/cc-04 then opened on a batched Bash entry-protocol call
# instead of the re-arm. A reader acts on the FIRST actionable thing in the
# banner, so the pin is "before anything actionable", not "before the ACTION
# line": the goal anchor is actionable, and it used to come first by design.
LATER_MARKERS = (
    "IN-FLIGHT GOAL",
    "LOOP STATE:",
    "EXECUTION DIARY",
    "REASONING SNAPSHOT",
    "ACTION:",
    "Re-enter /aspirations loop",
)


def _load_module(monkeypatch, tmp_path):
    agent_dir = tmp_path / "agents" / "deadmanagent"
    world_dir = tmp_path / "world"
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    world_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MIND_AGENT", "deadmanagent")
    monkeypatch.delenv("MIND_SID", raising=False)
    monkeypatch.setattr(_paths, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "WORLD_DIR", world_dir)

    spec = importlib.util.spec_from_file_location("pcr_deadman", TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # CHECKPOINT_PATH / ITERATION_CKPT_PATH are module-level constants built at
    # import from AGENT_DIR.NAME via body_state_path(), so patching _paths.AGENT_DIR
    # does NOT redirect them — they resolve under the real repo. Rebind them to the
    # tmp tree or main() reads live fleet state (and silently takes the degraded
    # branch, which is how this suite first failed).
    monkeypatch.setattr(
        mod, "CHECKPOINT_PATH", agent_dir / "session" / "compact-checkpoint.yaml")
    monkeypatch.setattr(
        mod, "ITERATION_CKPT_PATH", agent_dir / "session" / "iteration-checkpoint.json")
    return mod, agent_dir, world_dir


def _write_iteration_ckpt(agent_dir, goal_id="g-001-01"):
    p = agent_dir / "session" / "iteration-checkpoint.json"
    p.write_text(json.dumps({
        "goal_id": goal_id, "aspiration_id": "asp-001",
        "source": "world", "phase": "selected",
        "selected_at": "2026-09-11T19:00:00",
    }), encoding="utf-8")


def _assert_rearm_ordered_first(out):
    """The imperative is present, names the sentinel + delay, and PRECEDES
    whatever tells the reader to resume."""
    assert HEADLINE in out, "no re-arm imperative at all"
    assert SENTINEL in out, (
        "the prompt argument must be the literal sentinel — a slash-prefixed "
        "prompt is rejected at the user-invocable gate "
        "(schedule-wakeup-correctness.md anti-pattern B)")
    assert "delaySeconds=600" in out, "name the delay; an unsized net is advice"

    head = out.index(HEADLINE)
    seen_later = 0
    for later in LATER_MARKERS:
        if later in out:
            seen_later += 1
            assert head < out.index(later), (
                f"'{HEADLINE}' must come BEFORE '{later}' — an imperative read "
                "after the resume instruction is read after the model has "
                "already reached for the task")
    return seen_later


# --- path 1: full restore (compact-checkpoint.yaml present) ----------------

def test_full_restore_orders_the_rearm_first(monkeypatch, tmp_path, capsys):
    """The fixture is deliberately RICH. `if later in out` is vacuously true for
    a marker the banner never printed, so a thin checkpoint would let this pin
    pass while asserting nothing — which is how the pre-2026-09-13 pin passed
    for two days over a banner that buried the imperative."""
    mod, agent_dir, _world = _load_module(monkeypatch, tmp_path)
    _write_iteration_ckpt(agent_dir)          # -> IN-FLIGHT GOAL section
    (agent_dir / "session" / "compact-checkpoint.yaml").write_text(
        "encoding_queue: []\n"
        "active_context: {}\n"
        "all_slots:\n"
        "  loop_state:\n"
        "    goals_completed: 12\n"
        "    productive_goals: 12\n"
        "reasoning_snapshot:\n"
        "  key_decisions_this_session:\n"
        "    - held at tight zone\n",
        encoding="utf-8")

    mod.main()
    out = capsys.readouterr().out

    assert "CONTEXT RESTORED" in out, "precondition: the full banner ran"
    seen = _assert_rearm_ordered_first(out)
    assert seen >= 3, (
        "positive control: the ordering pin only means something against a "
        f"banner that actually printed the sections it must outrank (saw {seen} "
        f"of {len(LATER_MARKERS)}) — enrich the fixture, do not relax this")


# --- path 2: degraded restore (checkpoint missing) -------------------------

def test_degraded_restore_orders_the_rearm_first(monkeypatch, tmp_path, capsys):
    """The path a crash-during-checkpoint produces — i.e. exactly the resume
    least likely to be carrying a live net."""
    mod, agent_dir, _world = _load_module(monkeypatch, tmp_path)
    assert not (agent_dir / "session" / "compact-checkpoint.yaml").exists()
    _write_iteration_ckpt(agent_dir)

    mod.main()
    out = capsys.readouterr().out

    assert "degraded" in out, "precondition: the degraded banner ran"
    _assert_rearm_ordered_first(out)


# --- the negative control: this suite must be able to FAIL -----------------

def test_the_pin_would_catch_a_removal(monkeypatch, tmp_path):
    """A presence assertion that can never fail is not a pin. Prove the
    predicate discriminates by running it against the banner with the
    imperative stripped."""
    stripped = "\n".join(
        ln for ln in TARGET.read_text(encoding="utf-8").splitlines()
        if HEADLINE not in ln and SENTINEL not in ln)
    with pytest.raises(AssertionError):
        _assert_rearm_ordered_first(stripped)
