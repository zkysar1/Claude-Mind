"""Regression tests for iteration-close.sh `_repair_utilization_pending` ().

WHY THIS EXISTS
The Phase 4.26 utilization backstop was dead on the autonomous loop's hot path, via
three mutually-masking defects:

  1. `utilization-gate.sh` is registered as a PreToolUse matcher for `Skill`, and only
     acts when the invoked skill is `aspirations-state-update`. The hot path stopped
     invoking that skill — it runs `Bash: iteration-close.sh --phase state-update`.
     Measured across 5 agents' skill-invocation ledgers: 15 fires / 12,325 invocations
     (0.12%); bravo 0 / 2,552.
  2. The `--infer` repair that actually clears `utilization_pending` lived ONLY in
     `do_learning_gate`. Its consumer, `phase-4-26-gate.sh`, lives in
     `do_state_update`. Loop order is verify -> state-update -> learning-gate, so the
     repair ran one full phase AFTER the gate that needed it. Every manifest reached
     the gate with `utilization_pending=true` and no method recorded.
  3. `phase-4-26-gate.py:108` falsy-checks `retrieval_performed`, a key the real
     retrieval path never writes — so the gate returns a vacuous pass before reaching
     the utilization block (g-115-3113). That inertness is the only reason (1)+(2)
     never wedged the loop.

The fix extracted the repair into `_repair_utilization_pending` and calls it from
`do_state_update` immediately BEFORE the gate. ORDERING IS THE ENTIRE FIX — a
behavior test alone would still pass if someone moved the call back after the gate,
so `test_repair_precedes_gate_in_state_update` guards the ordering structurally.

The behavior tests extract the shell function and run it against a STUB
`utilization-feedback.sh`, so no real utilization counters are ever mutated.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import sys
import pathlib
# guard-580: resolve bash explicitly — a bare 'bash' argv[0] hits System32 WSL on win32.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ITER_CLOSE = PROJECT_ROOT / "core" / "scripts" / "iteration-close.sh"
PATHS_SH = PROJECT_ROOT / "core" / "scripts" / "_paths.sh"


# ---------------------------------------------------------------- structural


_FUNC_DEF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{")


def _func_span(src_lines, name):
    """(start, end) 1-indexed line numbers of a column-0 bash function.

    End is the LAST column-0 `}` before the next column-0 function definition, NOT
    the first one: these functions embed `python3 -c '...'` sources whose dict
    literals close with `}` at column 0, which truncates a naive scan mid-body.
    """
    start = None
    for i, line in enumerate(src_lines, 1):
        if line.startswith(name + "() {"):
            start = i
            break
    assert start is not None, f"function {name}() not found in iteration-close.sh"

    limit = len(src_lines)
    for j in range(start + 1, len(src_lines) + 1):
        if _FUNC_DEF.match(src_lines[j - 1]):
            limit = j - 1
            break
    closes = [j for j in range(start, limit + 1) if src_lines[j - 1] == "}"]
    assert closes, f"no column-0 closing brace found for {name}()"
    return start, closes[-1]


def test_repair_precedes_gate_in_state_update():
    """THE  REGRESSION GUARD.

    The repair must be CALLED inside do_state_update, and must appear BEFORE the
    phase-4-26-gate.sh invocation that consumes its output. Moving it back after the
    gate (or out of do_state_update entirely) restores the original defect while every
    behavior test still passes.
    """
    lines = ITER_CLOSE.read_text(encoding="utf-8").splitlines()
    su_start, su_end = _func_span(lines, "do_state_update")

    body = list(enumerate(lines[su_start - 1:su_end], su_start))
    repair = [n for n, t in body
              if re.match(r"\s*_repair_utilization_pending\s*$", t)]
    gate = [n for n, t in body if "phase-4-26-gate.sh" in t and not t.lstrip().startswith("#")]

    assert repair, (
        "REGRESSION: do_state_update no longer calls _repair_utilization_pending. "
        "The Phase 4.26 gate is back to seeing un-repaired manifests (g-115-3123)."
    )
    assert gate, "do_state_update no longer invokes phase-4-26-gate.sh — check this test's assumptions"
    assert min(repair) < min(gate), (
        f"REGRESSION: repair at line {min(repair)} runs AFTER the gate at line "
        f"{min(gate)}. Ordering IS the fix — the producer must run before its consumer."
    )


def test_learning_gate_keeps_backstop_call():
    """do_learning_gate keeps a (now no-op) call for crash-resume paths that skip
    state-update. Losing it silently drops coverage for operator retries."""
    lines = ITER_CLOSE.read_text(encoding="utf-8").splitlines()
    lg_start, lg_end = _func_span(lines, "do_learning_gate")
    body = lines[lg_start - 1:lg_end]
    assert any(re.match(r"\s*_repair_utilization_pending\s*$", t) for t in body), (
        "do_learning_gate lost its backstop call to _repair_utilization_pending"
    )


def test_no_duplicated_infer_logic():
    """The two-tier feedback ladder must exist in exactly ONE place in this file.

    Before the fix the ladder was inline in do_learning_gate; the fix hoisted it into
    the shared helper. A second copy means the two call sites can drift apart.
    """
    src = ITER_CLOSE.read_text(encoding="utf-8")
    calls = re.findall(r"utilization-feedback\.sh\"?\s+--goal\s+\"\$GOAL_ID\"\s+--infer", src)
    assert len(calls) == 1, (
        f"expected exactly 1 --infer call site in iteration-close.sh, found {len(calls)} "
        "— the ladder was duplicated instead of shared"
    )


# ---------------------------------------------------------------- behavioral


def _extract_helper():
    """Return the source text of `_repair_utilization_pending()` alone.

    Sourcing iteration-close.sh outright would execute its arg-parsing main body, so
    the function is lifted out and evaluated in isolation.
    """
    lines = ITER_CLOSE.read_text(encoding="utf-8").splitlines()
    start, end = _func_span(lines, "_repair_utilization_pending")
    return "\n".join(lines[start - 1:end])


@pytest.fixture
def harness(tmp_path):
    """Run the extracted helper against a stub utilization-feedback.sh.

    Returns run(manifest, *, goal_id, stub_rc) -> (returncode, stdout, stderr, argv_log).
    """
    agent_dir = tmp_path / "agents" / "testagent"
    (agent_dir / "session").mkdir(parents=True)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    argv_log = script_dir / "argv.log"

    def run(manifest, *, goal_id="g-test-001", stub_rc=0):
        ret = agent_dir / "session" / "retrieval-session.json"
        if manifest is None:
            if ret.exists():
                ret.unlink()
        elif isinstance(manifest, str):
            ret.write_text(manifest, encoding="utf-8")   # raw text (corrupt-JSON case)
        else:
            ret.write_text(json.dumps(manifest), encoding="utf-8")

        # Stub records argv. `stub_rc` applies ONLY to the --infer call so the
        # fallback leg can succeed and its stdout be asserted; a stub that failed
        # both legs would make every rc look like the total-failure path.
        (script_dir / "utilization-feedback.sh").write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> "{argv_log}"\n'
            'for a in "$@"; do [ "$a" = "--infer" ] && exit ' + str(stub_rc) + '; done\n'
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(script_dir / "utilization-feedback.sh", 0o755)
        if argv_log.exists():
            argv_log.unlink()

        harness_sh = tmp_path / "harness.sh"
        # `_paths.sh` is sourced FIRST because production does exactly that
        # (iteration-close.sh:69) and the extracted helper calls into it —
        # `retrieval_session_path` since . Omitting it made this
        # harness a shape production never runs: the helper died `command not
        # found` (rc=127) while the real script was fine, i.e. the test failed
        # for a reason that could not happen in production (guard-920 — the
        # tested shape must BE the production shape).
        #
        # ORDER MATTERS. _paths.sh assigns AGENT_DIR itself, so the tmp
        # override is re-assigned AFTER the source or it would be clobbered
        # and every case would read the LIVE agent dir.
        harness_sh.write_text(
            "set -euo pipefail\n"
            f'source "{PATHS_SH}"\n'
            f'AGENT_DIR="{agent_dir}"\n'
            f'GOAL_ID="{goal_id}"\n'
            f'SCRIPT_DIR="{script_dir}"\n'
            '_CURRENT_PHASE="state-update"\n'
            + _extract_helper()
            + "\n_repair_utilization_pending\n",
            encoding="utf-8",
        )
        p = subprocess.run([BASH, str(harness_sh)], capture_output=True,
                           text=True, timeout=60)
        log = argv_log.read_text(encoding="utf-8").splitlines() if argv_log.exists() else []
        return p.returncode, p.stdout, p.stderr, log

    return run


def _real_pending(goal_id="g-test-001"):
    """The shape a genuine `retrieve.sh --goal <id>` writes.

    Note what is ABSENT: `retrieval_performed`. The real retrieval path never emits
    that key — assuming it does is the g-115-3113 defect, and this fixture exists so
    these tests cannot repeat it.
    """
    return {
        "schema_version": 2,
        "goal_id": goal_id,
        "tree_nodes_loaded": ["a", "b"],
        "supplementary_items": ["rb-001"],
        "utilization_pending": True,
        "utilization_method": None,
    }


def test_real_pending_manifest_triggers_balanced_infer(harness):
    rc, out, err, log = harness(_real_pending())
    assert rc == 0
    assert log, "REGRESSION: a real manifest with utilization_pending=true was not repaired"
    assert "--goal g-test-001 --infer --confidence balanced" in log[0], log
    assert "inferred utilization feedback" in out


def test_no_retrieval_stub_is_left_alone(harness):
    """The stub carries retrieval_performed=False and pending=false — nothing to score."""
    m = _real_pending()
    m["retrieval_performed"] = False
    m["utilization_pending"] = False
    rc, out, err, log = harness(m)
    assert rc == 0
    assert log == [], f"repaired a no-retrieval stub: {log}"


def test_stub_shape_with_pending_true_is_still_skipped(harness):
    """Belt-and-braces: even a malformed stub claiming pending must not be repaired.

    `retrieval_performed is False` is the authoritative stub discriminator (the same
    one pre-apply-consult-gate.py:203 and iteration-close.sh:1554 use). Falling back to
    a truthiness check here would resurrect the g-115-3113 class.
    """
    m = _real_pending()
    m["retrieval_performed"] = False
    rc, out, err, log = harness(m)
    assert rc == 0
    assert log == [], f"stub discriminator ignored: {log}"


def test_other_goals_manifest_is_not_touched(harness):
    rc, out, err, log = harness(_real_pending(goal_id="g-other-999"),
                                goal_id="g-test-001")
    assert rc == 0
    assert log == [], f"repaired another goal's manifest: {log}"


def test_already_repaired_manifest_is_idempotent_noop(harness):
    """Proves calling the helper at BOTH sites costs one JSON read, not two repairs."""
    m = _real_pending()
    m["utilization_pending"] = False
    m["utilization_method"] = "infer"
    rc, out, err, log = harness(m)
    assert rc == 0
    assert log == [], f"re-ran feedback on an already-repaired manifest: {log}"


def test_missing_manifest_is_a_silent_noop(harness):
    rc, out, err, log = harness(None)
    assert rc == 0
    assert log == []


def test_unparseable_manifest_fails_open(harness):
    """A corrupt manifest must not abort the phase — callers run under `set -e`."""
    rc, out, err, log = harness("{not json")
    assert rc == 0, f"corrupt manifest aborted the phase: {err}"
    assert log == [], f"ran feedback off an unparseable manifest: {log}"


def test_schema_v1_falls_back_to_all_unknown(harness):
    """rc=4 is the documented schema_version<2 signal."""
    rc, out, err, log = harness(_real_pending(), stub_rc=4)
    assert rc == 0
    assert len(log) == 2, f"expected infer then all-unknown fallback, got {log}"
    assert "--infer --confidence balanced" in log[0]
    assert "--all-unknown" in log[1]
    assert "fell back to --all-unknown" in out


def test_other_failures_warn_and_leave_pending(harness):
    """Any non-4 failure warns and leaves pending=true so a later phase retries —
    it must NOT silently escalate to --all-unknown, which would record a method and
    permanently mask the failure."""
    rc, out, err, log = harness(_real_pending(), stub_rc=7)
    assert rc == 0, "a feedback failure must never abort the phase"
    assert len(log) == 1, f"escalated past --infer on a non-4 failure: {log}"
    assert "rc=7" in err and "will retry" in err
