"""Pin that recurring-close.sh closes out phase-4-execute ().

phase-4 IS the execution phase, so its duration is the single most valuable
thing the phase-cost report can attribute. On the recurring path it was
unmeasurable: the digest puts phase-4's phase-END at the tail of the Phase-4
body, and the recurring shortcut never walks that far — its terminal imperative
sends the LLM straight to Skill(aspirations-spark) and LOOP_CONTINUE. So the
marker got a start and never an end, and phase-cost-report.py reported those
records as `in_flight` rather than as a measurement.

Nothing failed. An unmatched start is "detectable-not-fatal" per record, which
is true and is exactly why it survived: in AGGREGATE it meant a recurring-heavy
agent had no execution-duration signal at all.

Measured before the fix, and the CONTRAST is the fingerprint rather than the
raw delta — every phase this script WRAPS paired exactly while the one phase
nobody emits an end for did not:

    alpha / cc-07   phase-4-execute  start=6 end=2   (delta -4)
    bravo / cc-05   phase-4-execute  start=9 end=4   (delta -5)
    both boxes      phase-5-verify, phase-8-state-update,
                    phase-12-learning-gate            delta 0

So the test that matters is not "the script mentions phase-4-execute" — it is
that the SHIPPED line, run in the shape the script runs it, actually lands a
`phase_end` record. The command is EXTRACTED from recurring-close.sh and
executed, never retyped here, so a hand-copy cannot pass while the real one
rots (guard-920).

Hermetic via MIND_AGENT_DIR, so no test touches the live diary — writing a
stray phase_end into a real diary would corrupt the very pairing metric this
goal exists to restore.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

SCRIPT_DIR = Path(__file__).resolve().parent.parent
RECURRING_CLOSE = SCRIPT_DIR / "recurring-close.sh"
DIARY_SH = SCRIPT_DIR / "execution-diary.sh"

EMIT_RE = re.compile(
    r'^(bash "\$SCRIPT_DIR/execution-diary\.sh" phase-end phase-4-execute.*?\|\| true)',
    re.DOTALL | re.MULTILINE,
)


def _script_text():
    return RECURRING_CLOSE.read_text(encoding="utf-8")


def _extract_emit():
    """Pull the emit command out of the SHIPPED script (guard-920)."""
    m = EMIT_RE.search(_script_text())
    assert m, (
        "the phase-4-execute phase-end emit is GONE from recurring-close.sh — "
        "recurring closes are silently unmeasurable again (g-115-4886)"
    )
    return m.group(1)


def _run_emit(emit_cmd, agent_dir, goal_id="g-TEST-4886"):
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env["STORAGE_BACKEND"] = "local"  # guard-955
    env["SCRIPT_DIR"] = str(SCRIPT_DIR)
    env["GOAL_ID"] = goal_id
    return subprocess.run(
        [BASH, "-c", emit_cmd], env=env, capture_output=True, text=True, timeout=120
    )


def _diary(agent_dir):
    p = agent_dir / "session" / "execution-diary.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ── structural: the emit exists, and its POSITION is load-bearing ──────────

def test_emit_precedes_the_wrapped_phases():
    """Ordering, not just presence.

    The end must be recorded BEFORE phase-5-verify's start or the diary reads
    out of chronological order and a naive pairing walk mis-attributes spans.
    """
    text = _script_text()
    emit_at = text.index('phase-end phase-4-execute')
    verify_at = text.index('\nrun_phase verify')
    assert emit_at < verify_at, (
        "the phase-4-execute end is emitted AFTER the wrapped phases begin — "
        "it must precede run_phase verify to preserve diary ordering"
    )


def test_emit_is_unconditional_and_failure_suppressed():
    """Telemetry must never block a close, and must never be skipped."""
    emit = _extract_emit()
    assert emit.rstrip().endswith("|| true"), (
        "the emit can abort a recurring close — telemetry must fail open "
        "(mirrors iteration-close.sh::_emit_marker)"
    )
    # No `if`/`&&` guard on the emit line itself: an end that fires only
    # sometimes reintroduces unpaired starts, which is the defect.
    assert not emit.lstrip().startswith(("if ", "[[", "[ ")), (
        "the emit is conditional — a sometimes-emitted end is the same defect"
    )


# ── behavioural: the SHIPPED line actually lands a record ──────────────────

def test_shipped_emit_lands_a_phase_end_record(tmp_path):
    agent_dir = tmp_path / "agent"
    r = _run_emit(_extract_emit(), agent_dir)
    assert r.returncode == 0, f"emit failed: rc={r.returncode} stderr={r.stderr[:400]}"

    entries = _diary(agent_dir)
    ends = [
        e for e in entries
        if e.get("entry_type") == "phase_end" and e.get("phase") == "phase-4-execute"
    ]
    assert len(ends) == 1, (
        f"expected exactly one phase_end/phase-4-execute record, got "
        f"{len(ends)} in {entries!r}"
    )
    assert ends[0].get("goal_id") == "g-TEST-4886", (
        "the goal_id did not reach the record — phase-cost pairing and every "
        "other goal_id-keyed consumer key on this field"
    )


def test_positive_control_without_the_emit_nothing_lands(tmp_path):
    """rb-245: prove the assertion above can FAIL.

    Without this, a diary that recorded phase_end for some unrelated reason
    would make the test pass with the emit deleted.
    """
    agent_dir = tmp_path / "agent"
    (agent_dir / "session").mkdir(parents=True)
    entries = _diary(agent_dir)
    assert entries == [], "fixture diary was not empty before the emit ran"

    # run a DIFFERENT diary command (a start, not an end) and confirm the
    # end-specific assertion would not be satisfied by it
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env["STORAGE_BACKEND"] = "local"
    subprocess.run(
        [BASH, DIARY_SH.as_posix(), "phase-start", "phase-4-execute", "--goal", "g-TEST-4886"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    ends = [
        e for e in _diary(agent_dir)
        if e.get("entry_type") == "phase_end" and e.get("phase") == "phase-4-execute"
    ]
    assert ends == [], (
        "a phase-START satisfied the phase-END assertion — the behavioural "
        "test above is not discriminating"
    )


def test_emit_pairs_a_prior_start(tmp_path):
    """End-to-end shape: start then the shipped end => delta 0."""
    agent_dir = tmp_path / "agent"
    (agent_dir / "session").mkdir(parents=True)
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env["STORAGE_BACKEND"] = "local"
    subprocess.run(
        [BASH, DIARY_SH.as_posix(), "phase-start", "phase-4-execute", "--goal", "g-TEST-4886"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    _run_emit(_extract_emit(), agent_dir)

    entries = _diary(agent_dir)
    starts = sum(
        1 for e in entries
        if e.get("entry_type") == "phase_start" and e.get("phase") == "phase-4-execute"
    )
    ends = sum(
        1 for e in entries
        if e.get("entry_type") == "phase_end" and e.get("phase") == "phase-4-execute"
    )
    assert starts == 1 and ends == 1, f"expected 1/1, got {starts}/{ends}"
