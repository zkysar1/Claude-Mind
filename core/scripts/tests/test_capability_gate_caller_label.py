"""test_capability_gate_caller_label.py — regression test for .

Pins TWO fixes to capability-gate, both of which were silent gaps rather than
wrong answers: the gate was returning the right decision while telling nobody
who asked and telling nobody why it refused.

FIX 1 — the ledger's `caller` column was empty for this gate.
`_gate_log.log()` has always accepted a `caller` kwarg and 2890 distinct
callsite labels populate it across 75152 live records (`store_dupe_warn.main`,
`gates.prose_verification.evaluate goal=g-100-02`, ...). BOTH `_gate_log` call
sites in `gates/capability.py` omitted it, so the fleet's most-fired gate wrote
`caller=None` on every record.

  MEASURED COST: this goal asked "which caller emitted payload_hash
  b04ad6159b5d?" (alpha session aae8287f, 2026-07-19 07:57-07:58). Answering it
  required correlating a DIFFERENT gate's record logged 1s earlier that happened
  to share the payload_hash, because the capability-gate records named nobody.

  SHAPE MATTERS, and `caller_context` alone is the wrong value: it is a
  2-valued enum (create-blocker|defer) naming the enforcement PATH, whereas
  `caller` holds a CALLSITE label. So the label is `module.function ctx=<path>`,
  matching the established convention. A test that asserted only "caller is not
  None" would pass on the semantically-wrong value, so the assertions below pin
  the module.function prefix AND the ctx suffix separately.

FIX 2 — the evidence-error diagnostic was STDOUT-only.
Measured 2026-07-30 pre-fix: 320 bytes to stdout, 0 to stderr. A wrapper that
captures stdout as the gate's DATA and surfaces only stderr saw an empty error
channel plus a bare exit 1, and so could not distinguish a malformed-evidence
refusal from any other refusal. That is how one caller retried the same bad
payload 3x in 47s before correcting it on the 4th attempt.

DISCRIMINATION (guard-1943 — a test that passes against both the fixed and the
broken code certifies nothing): every assertion here is bound to the CALLER
layer via a real subprocess, and each one fails if its fix is reverted:
  - drop `caller=` from either _gate_log call site -> the caller assertions fail
  - pass bare `caller_context` instead of the label -> the prefix assertion fails
  - remove the stderr mirror -> test_evidence_error_is_mirrored_to_stderr fails
  - a stdout-shape regression -> test_stdout_payload_unchanged fails

Run: STORAGE_BACKEND=local python -m pytest \
       core/scripts/tests/test_capability_gate_caller_label.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

GATE_PY = CORE_SCRIPTS / "capability-gate.py"

_FAILURE_REASON = "need user to deploy the service"

# The label the gate must now write. Split into prefix/suffix so a regression
# that keeps SOME value but loses the shape still fails (see SHAPE MATTERS).
_CALLER_PREFIX = "gates.capability:evaluate"


def _run(*extra, caller_context="create-blocker"):
    """Invoke the gate through its real CLI entry point.

    Deliberately a subprocess, not a direct evaluate() call: the defect being
    pinned is in the wiring between argparse, evaluate(), and _gate_log, and an
    in-process call that hand-passes caller_context would exercise a shape the
    production caller never takes (rb-5235).
    """
    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", _FAILURE_REASON,
        "--intended-participants", "user",
        "--caller-context", caller_context,
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


# ---------------------------------------------------------------- FIX 2 ----

def test_evidence_error_is_mirrored_to_stderr():
    """A malformed --evidence payload must announce itself on STDERR."""
    r = _run("--evidence", "not-json-at-all", caller_context="defer")
    assert r.returncode == 1, f"expected refusal exit 1, got {r.returncode}"
    assert r.stderr.strip(), (
        "stderr was EMPTY on an evidence refusal -- this is the exact pre-fix "
        "state (320B stdout / 0B stderr) that let a caller retry blind 3x"
    )
    assert "capability-gate" in r.stderr
    assert "evidence" in r.stderr.lower()
    # The remedy pointer is the actionable half -- without it the caller knows
    # something is wrong but not what shape is expected.
    assert "evidence-envelope.md" in r.stderr, (
        "stderr must name the convention carrying the {type,id,claim} shape"
    )


def test_stdout_payload_unchanged_on_evidence_error():
    """stderr is ADDITIVE -- stdout must still carry the machine-readable JSON.

    Guards the fix against becoming a breaking change for any consumer that
    parses the gate's stdout.
    """
    r = _run("--evidence", "not-json-at-all", "--output", "json",
             caller_context="defer")
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload["would_block"] is True
    assert "evidence_error" in payload
    assert "reason" in payload


# ---------------------------------------------------------------- FIX 1 ----

def _run_logged(tmp_path, *extra, caller_context="create-blocker"):
    """Invoke the gate with telemetry REDIRECTED to a tmp meta dir.

    Two env facts make this the only workable shape, and both are deliberate
    upstream design rather than obstacles:

    1. `_gate_log.log()` is a silent no-op whenever PYTEST_CURRENT_TEST is set
       (g-248-102), so a test asserting on firing records MUST opt in with
       GATE_LOG_ALLOW_PYTEST=1. Without it this test would read an unchanged
       record count and report "the gate wrote nothing" -- which is exactly the
       false negative the first draft of this file hit.
    2. Opting in without redirecting the destination writes SYNTHETIC records
       into the production ledger and skews the noop/pass ratios the retirement
       evaluator scores. MIND_META (+ MIND_WORLD) point them at a tmpdir.

    STORAGE_BACKEND=local is pinned so the record lands directly in
    gate-firings.jsonl rather than the own-cloud spool (guard-955 also requires
    the pin for any test runner).
    """
    import os
    tmp_meta = tmp_path / "meta"
    tmp_world = tmp_path / "world"
    tmp_meta.mkdir(parents=True, exist_ok=True)
    tmp_world.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["MIND_META"] = str(tmp_meta)
    env["MIND_WORLD"] = str(tmp_world)
    env["GATE_LOG_ALLOW_PYTEST"] = "1"
    env["STORAGE_BACKEND"] = "local"

    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", _FAILURE_REASON,
        "--intended-participants", "user",
        "--caller-context", caller_context,
        *extra,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          env=env)
    rows = []
    ledger = tmp_meta / "gate-firings.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8",
                                     errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("gate_id") == "capability-gate":
                rows.append(r)
    return proc, rows


def test_caller_is_recorded_on_the_main_path(tmp_path):
    """The noop/block/pass/override path must name its callsite."""
    proc, rows = _run_logged(tmp_path, caller_context="create-blocker")
    assert proc.returncode in (0, 1), (
        f"unexpected exit {proc.returncode}: {proc.stderr[:300]}"
    )
    assert rows, "the gate wrote no capability-gate record at all"
    caller = rows[-1].get("caller")
    assert caller, (
        "caller is None/empty on the MAIN path -- this is the pre-fix state that "
        "left the fleet's most-fired gate anonymous in its own ledger"
    )
    assert caller.startswith(_CALLER_PREFIX), (
        f"caller must be a CALLSITE label starting {_CALLER_PREFIX!r}, got "
        f"{caller!r} -- a bare enforcement-path enum (create-blocker|defer) is "
        f"the wrong KIND of value for this field"
    )
    assert "ctx=create-blocker" in caller, (
        f"the enforcement path must ride as a ctx= suffix, got {caller!r}"
    )


def test_caller_is_recorded_on_the_evidence_error_path(tmp_path):
    """The evidence-error branch is a SEPARATE _gate_log call site.

    It is the branch that produced the forensic dead-end this goal investigated,
    so it gets its own assertion: fixing only the main path would leave exactly
    the records that mattered still anonymous.
    """
    proc, rows = _run_logged(tmp_path, "--evidence", "not-json-at-all",
                             caller_context="defer")
    assert proc.returncode == 1
    assert rows, "the evidence-error branch wrote no record"
    rec = rows[-1]
    assert (rec.get("extra") or {}).get("decision_path") == "evidence-error", (
        "expected the evidence-error branch; got "
        f"{(rec.get('extra') or {}).get('decision_path')!r}"
    )
    caller = rec.get("caller")
    assert caller, "caller is None/empty on the EVIDENCE-ERROR path"
    assert caller.startswith(_CALLER_PREFIX), f"got {caller!r}"
    assert "ctx=defer" in caller, (
        f"caller_context must be preserved verbatim in the label, got {caller!r}"
    )


def test_decision_is_block_not_fail_open_on_evidence_error(tmp_path):
    """Companion pin for : this branch REFUSES, it does not fail open.

    Kept here because the caller fix touches the same _gate_log call, and a
    careless edit to the decision argument would otherwise re-arm
    gate-retirement-eval's investigate-on-fail_open rule.
    """
    _proc, rows = _run_logged(tmp_path, "--evidence", "not-json-at-all",
                              caller_context="defer")
    evid = [r for r in rows
            if (r.get("extra") or {}).get("decision_path") == "evidence-error"]
    assert evid, "no evidence-error records found"
    assert evid[-1].get("decision") == "block", (
        f"evidence-error must log decision=block, got {evid[-1].get('decision')!r}"
    )
