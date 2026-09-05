"""EXECUTION coverage for the stop-hook consecutive-BLOCK streak line ().

WHAT THIS PINS, AND WHY IT NEEDED ITS OWN FILE
----------------------------------------------
The hook has always written every BLOCK to ``core/logs/stop-hook.log`` with
agent+sid, and has never read it back -- so its refusal message is byte-identical
at streak 1 and at streak 200. A session already failing against that exact string
receives it again with nothing new in it.

The change adds ONE line past a threshold. Everything that could make it dangerous
is a NON-goal the tests below pin as hard as the feature itself:

  * the hook still BLOCKs at EVERY streak length (asserted in every test here,
    including the ones where the streak line is expected to appear),
  * nothing writes ``stop-requested`` / ``stop-loop`` / ``agent-state``,
  * the log read fails open -- an absent or unreadable log leaves the decision
    AND the message byte-identical to the pre-change hook.

That last one is asserted by DIFFING against a mutated hook with the feature's
concat removed, rather than by re-typing the expected string here. A hand-copied
expectation would drift the moment the base message is edited and would then be
asserting nothing (rb-5146: source text proves wiring exists, never that it runs).

THE PREDICATE UNDER TEST IS PHASE ADVANCE, NOT WALL TIME
--------------------------------------------------------
The streak counts BLOCKs since the execution diary last moved, because a loop that
is ADVANCING writes its diary between turns and that resets the count however often
it blocks. ``test_an_advancing_diary_never_surfaces_a_streak`` is the test that
matters most here -- it is what keeps this line off a healthy loop -- and its
mutation twin proves the mtime comparison is load-bearing rather than decorative.

HARNESS REUSE (deliberate, not incidental)
------------------------------------------
Everything comes from ``test_stop_hook_gate_integration``: the same tmp PROJECT_ROOT,
the same production-shaped environment (MIND_SID / MIND_AGENT scrubbed per
guard-1742, STORAGE_BACKEND pinned per guard-955), the same BASH resolution
(guard-580). A second fixture would drift from that one silently.

Note ``_build_runner_root`` does NOT create an execution diary. That is the
fail-open case arriving for free, and the streak tests below create the diary
explicitly -- so an absent diary is never mistaken for a configured one.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from test_stop_hook_gate_integration import (  # noqa: E402
    _agent_dir,
    _blocked,
    _drive,
    _hook_log,
    _run_hook_as_runner,
)

# The production line the mutation proofs edit, kept as a module constant so a
# reword cannot turn a mutation test into a silent no-op that still passes.
MTIME_COMPARISON = "        if when >= advanced:"
STREAK_CONCAT = "    + streak_msg\n"
STREAK_MARKER = "STALL: this is BLOCK #"


def _reason(proc) -> str:
    """The reason string out of the hook's decision payload.

    Scans for the JSON line rather than parsing all of stdout: the hook prints
    other diagnostics on some paths, and a whole-stdout json.loads would fail for
    reasons unrelated to what these tests assert.
    """
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if "reason" in payload:
            return payload["reason"]
    raise AssertionError(f"no decision payload in stdout: {proc.stdout!r}")


def _diary(root: Path) -> Path:
    return _agent_dir(root) / "session" / "execution-diary.jsonl"


def _write_diary(root: Path, age_seconds: float = 0.0) -> Path:
    """Create the diary and age its mtime. age=0 means 'just advanced'."""
    d = _diary(root)
    d.write_text('{"phase": "start", "goal_id": "g-fixture"}\n', encoding="utf-8")
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(d, (t, t))
    return d


def _signals_untouched(root: Path) -> None:
    """The non-goal: this feature may never move the loop's control signals."""
    sess = _agent_dir(root) / "session"
    assert not (sess / "stop-requested").exists()
    assert not (sess / "stop-loop").exists()
    assert (sess / "agent-state").read_text(encoding="utf-8").strip() == "RUNNING"


# --------------------------------------------------------------------------
# 1. Frozen diary -> the Nth message carries the streak line, and still BLOCKs
# --------------------------------------------------------------------------

def test_a_frozen_diary_surfaces_the_streak_at_the_threshold(tmp_path):
    """Three BLOCKs with no phase advance: silent, silent, then the streak line.

    The first two assertions are the half that stops this being a counter that
    fires on everything -- the line is absent below the threshold.
    """
    proc, root = _drive(tmp_path)
    _write_diary(root, age_seconds=3600)  # last phase advance: an hour ago

    reasons = [_reason(proc)]
    procs = [proc]
    for _ in range(2):
        p = _run_hook_as_runner(root)
        procs.append(p)
        reasons.append(_reason(p))

    # THE NON-GOAL, asserted first: every one of them still BLOCKs.
    assert all(_blocked(p) for p in procs)

    # Run 1 predates the diary write, so the meaningful window is runs 2 and 3.
    assert STREAK_MARKER not in reasons[1], "fired below the threshold"
    assert STREAK_MARKER in reasons[2], "did not fire AT the threshold"
    assert "BLOCK #3" in reasons[2]
    assert "execution-diary mtime" in reasons[2]
    _signals_untouched(root)


def test_the_streak_line_is_appended_not_substituted(tmp_path):
    """The base refusal survives intact -- the streak is an addition, not a rewrite."""
    proc, root = _drive(tmp_path)
    _write_diary(root, age_seconds=3600)
    for _ in range(2):
        proc = _run_hook_as_runner(root)

    reason = _reason(proc)
    assert STREAK_MARKER in reason
    assert "Your FIRST action MUST be: Skill('aspirations')" in reason
    assert reason.index("Skill('aspirations')") < reason.index(STREAK_MARKER)


def test_the_threshold_is_read_from_the_environment(tmp_path):
    """STALL_THRESHOLD is the env-with-default convention, not a new config key."""
    proc, root = _drive(tmp_path)
    _write_diary(root, age_seconds=3600)
    second = _run_hook_as_runner(root, extra_env={"STALL_THRESHOLD": "2"})

    assert _blocked(second)
    assert "BLOCK #2" in _reason(second)


# --------------------------------------------------------------------------
# 2. Advancing diary -> the streak line never appears
# --------------------------------------------------------------------------

def test_an_advancing_diary_never_surfaces_a_streak(tmp_path):
    """A loop that keeps advancing phases never sees this line, however often it
    blocks. This is the test that keeps the feature off a healthy loop."""
    proc, root = _drive(tmp_path)
    _write_diary(root)

    reasons = [_reason(proc)]
    procs = [proc]
    for _ in range(4):
        p = _run_hook_as_runner(root)
        procs.append(p)
        reasons.append(_reason(p))
        _write_diary(root)  # phase advanced: diary mtime moves past that BLOCK

    assert all(_blocked(p) for p in procs)
    assert not any(STREAK_MARKER in r for r in reasons), \
        "streak surfaced on a loop that was advancing phases"
    _signals_untouched(root)


def test_mutation_neutralizing_the_mtime_comparison_fires_on_a_healthy_loop(tmp_path):
    """Positive control for the test above.

    With ``when >= advanced`` neutralized the streak counts every BLOCK ever
    logged, so the advancing-diary scenario starts firing. That is what proves
    the comparison -- not merely the presence of a counter -- is what suppresses
    the line on a healthy loop.
    """
    def mutate(text: str) -> str:
        assert MTIME_COMPARISON in text, "mutation anchor drifted from the hook"
        return text.replace(MTIME_COMPARISON, "        if True:")

    proc, root = _drive(tmp_path, mutate=mutate)
    _write_diary(root)
    reasons = [_reason(proc)]
    for _ in range(4):
        p = _run_hook_as_runner(root)
        reasons.append(_reason(p))
        _write_diary(root)

    assert any(STREAK_MARKER in r for r in reasons), \
        "mutation did not change behaviour -- the real test proves nothing"


# --------------------------------------------------------------------------
# 3. Absent / unreadable log -> decision AND message identical to pre-change
# --------------------------------------------------------------------------

def _reason_without_the_feature(tmp_path_factory_dir) -> str:
    """Today's message: the same hook with the streak concat removed."""
    def mutate(text: str) -> str:
        assert STREAK_CONCAT in text, "concat anchor drifted from the hook"
        return text.replace(STREAK_CONCAT, "")

    proc, _root = _drive(tmp_path_factory_dir, mutate=mutate)
    assert _blocked(proc)
    return _reason(proc)


def test_an_absent_diary_leaves_the_message_byte_identical(tmp_path, tmp_path_factory):
    """No diary at all -> stat() raises -> fail-open, and the reason is unchanged.

    Diffed against the feature-removed hook rather than a hand-copied string, so
    the assertion cannot rot into a tautology when the base message is edited.
    """
    baseline = _reason_without_the_feature(tmp_path_factory.mktemp("baseline"))

    proc, root = _drive(tmp_path)
    assert not _diary(root).exists()
    assert _blocked(proc)
    assert _reason(proc) == baseline
    _signals_untouched(root)


def test_an_unreadable_log_leaves_the_message_byte_identical(tmp_path, tmp_path_factory):
    """The log path is a DIRECTORY: read_text raises, and the hook is unchanged."""
    baseline = _reason_without_the_feature(tmp_path_factory.mktemp("baseline"))

    proc, root = _drive(tmp_path)
    _write_diary(root, age_seconds=3600)

    log = root / "core" / "logs" / "stop-hook.log"
    log.unlink(missing_ok=True)
    log.mkdir()  # every read of it now raises IsADirectoryError

    after = _run_hook_as_runner(root)
    assert _blocked(after), "an unreadable log must not change the decision"
    assert _reason(after) == baseline
    _signals_untouched(root)


def test_the_hook_still_logs_its_block_line(tmp_path):
    """Guards the input this feature reads: if the hook stopped logging BLOCKs the
    streak would silently never fire, and nothing else in the suite would notice."""
    _proc, root = _drive(tmp_path)
    assert " BLOCK " in _hook_log(root)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
