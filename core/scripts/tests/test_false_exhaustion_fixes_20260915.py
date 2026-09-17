"""EXECUTION coverage for the 2026-09-15 false-exhaustion fixes (zeta/cc-02 stop).

WHAT THIS PINS
--------------
(1) stop-failure-hook.sh records the StopFailure payload's ``error.error_type``
    in the crash-marker. Until 2026-09-15 it wrote the literal
    ``context_exhaustion`` for EVERY API-error turn end (rate limit, overload,
    5xx, auth, billing ...), and /boot then announced "context exhaustion
    detected" -- a label the fleet repeated as a diagnosis.
(2) stop-hook.sh appends the context-budget banner (guard-6380's falsifier for
    the harness ``<total_tokens>`` marker) to EVERY BLOCK reason, so the true
    number rides beside the false one on every turn-end -- including the wakeup
    ticks the iteration battery never sees. The banner is passed VERBATIM
    (guard-2676: its text is a contract with BANNER_RE elsewhere), so the
    assertions below match the banner's own shapes, not a re-typed copy.
(3) loop-exhaustion-fence.sh passes the sensor's zone into the decision module
    so a firing NAMES it instead of "unrecorded", and spells its verdict
    LOOP-STALL (what the predicate proves: no phase advance) rather than
    LOOP-EXHAUSTION (what it does not prove). Two agents read the old name over
    a reason that had disclaimed it (alpha 09-11, zeta 09-15).

All three changes are reason-/marker-only: the hook still BLOCKs (asserted), the
fence's ladder is untouched, and nothing here writes a stop signal.

HARNESS REUSE from test_stop_hook_gate_integration / test_stop_hook_block_streak:
same tmp PROJECT_ROOT, same production-shaped env (MIND_SID / MIND_AGENT
scrubbed per guard-1742, STORAGE_BACKEND pinned per guard-955), same BASH
resolution (guard-580). The diary store's name is built from parts so this file
never carries it beside a write call (same convention as the fence's own test).
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import time

from test_stop_hook_block_streak import _reason  # noqa: E402
from test_stop_hook_gate_integration import (  # noqa: E402
    AGENT,
    BASH,
    RUNNER_SID,
    _agent_dir,
    _blocked,
    _drive,
    _run_hook_as_runner,
)

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
DIARY_STORE_NAME = "execution-" + "diary.jsonl"

SENSOR_NORMAL = {  # guard-6380's measured incident values
    "used_pct": 28, "pct_to_autocompact": 58.3, "zone": "normal",
    "headroom_tokens": 200000, "updated_at": "2026-09-09T12:00:00",
    "env_seen": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000",
                 "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"},
}


def _scrubbed_env(extra=None):
    env = os.environ.copy()
    env.pop("MIND_SID", None)
    env.pop("MIND_AGENT", None)
    env["STORAGE_BACKEND"] = "local"
    env.update(extra or {})
    return env


def _run_stop_failure(root, payload: dict) -> subprocess.CompletedProcess:
    """Fire stop-failure-hook.sh the way a StopFailure event does."""
    return subprocess.run(
        [BASH, str(root / "core" / "scripts" / "stop-failure-hook.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        timeout=120, env=_scrubbed_env(),
    )


def _marker(root) -> str:
    return (_agent_dir(root) / "session" / "crash-marker").read_text(
        encoding="utf-8").strip()


# ---------------------------------------------------------------- (1) marker

def test_crash_marker_records_the_payloads_error_type(tmp_path):
    _, root = _drive(tmp_path)
    proc = _run_stop_failure(root, {
        "session_id": RUNNER_SID, "hook_event_name": "StopFailure",
        "error": {"error_type": "rate_limit",
                  "reason": "Rate limited", "message": "429"},
    })
    assert proc.returncode == 0, proc.stderr
    _ts, kind, sid = _marker(root).split(" ")
    assert kind == "rate_limit"
    assert sid == f"sid={RUNNER_SID}"
    assert "context_exhaustion" not in _marker(root)


def test_crash_marker_falls_back_to_unknown_without_an_error_block(tmp_path):
    """An older payload shape (no `error`) must still leave a breadcrumb --
    and an honest one, never the old hardcoded label."""
    _, root = _drive(tmp_path)
    proc = _run_stop_failure(root, {"session_id": RUNNER_SID})
    assert proc.returncode == 0, proc.stderr
    _, kind, _ = _marker(root).split(" ")
    assert kind == "unknown"


def test_crash_marker_keeps_its_three_token_shape_on_a_hostile_error_type(tmp_path):
    """/boot parses `<ts> <type> sid=<sid>`; a payload string with spaces or
    shell metacharacters must neither widen it nor land in the file verbatim."""
    _, root = _drive(tmp_path)
    hostile = "rate limit; $(rm -rf /) sid=evil"
    proc = _run_stop_failure(root, {
        "session_id": RUNNER_SID, "error": {"error_type": hostile}})
    assert proc.returncode == 0, proc.stderr
    parts = _marker(root).split(" ")
    assert len(parts) == 3, parts
    assert parts[2] == f"sid={RUNNER_SID}"
    assert "$(" not in parts[1] and ";" not in parts[1]


# ---------------------------------------------------------------- (2) banner

def test_block_reason_carries_the_context_banner_even_without_a_sensor_file(tmp_path):
    """The fixture has no context-budget.json, so the banner's own degradation
    line is what must appear -- verbatim, not a re-format (guard-2676)."""
    proc, _root = _drive(tmp_path)
    assert _blocked(proc)
    reason = _reason(proc)
    assert "CTX: unavailable" in reason, reason[-400:]


def test_block_reason_banner_reflects_a_present_sensor_file(tmp_path):
    """The marker read 0 while the sensor read zone normal / 200,000 tokens to
    autocompact (guard-6380). After the fix that sensor line is IN the BLOCK
    reason, beside whatever the marker says, and the decision is unchanged."""
    proc, root = _drive(tmp_path)
    assert _blocked(proc)
    (_agent_dir(root) / "session" / "context-budget.json").write_text(
        json.dumps(SENSOR_NORMAL), encoding="utf-8")
    again = _run_hook_as_runner(root)
    assert _blocked(again), "the banner must never change the decision"
    reason = _reason(again)
    assert "zone normal" in reason, reason[-400:]
    assert "to-compact 200,000 tokens" in reason, reason[-400:]
    # The re-entry imperative first, the sensor line last (additive, like the
    # streak and fence lines before it).
    assert reason.index("Skill('aspirations')") < reason.index("CTX: raw")


def test_the_banner_wiring_is_additive_only():
    """Mirror of the fence's own additive-only proof: the sensor line reaches
    the reason string and NOTHING else -- one definition + one concatenation --
    and the decision stays unconditional."""
    text = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    assert 'ctx_msg = (" " + _cm) if _cm else ""' in text
    assert text.count("ctx_msg") == 2, text.count("ctx_msg")
    assert 'CTX_MSG="$CTX_MSG"' in text, "banner not exported into the payload"
    assert 'print(json.dumps({"decision": "block"' in text


# ---------------------------------------------------------------- (3) fence

def _fire_fence(root, *, sensor: dict | None, blocks: int = 5):
    """Drive the fence WRAPPER into its pause rung: a diary frozen an hour ago
    and `blocks` turn-ends for this sid logged after it. Returns the process."""
    sdir = _agent_dir(root) / "session"
    diary = sdir / DIARY_STORE_NAME
    diary.write_text("{}\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(diary, (old, old))
    now = datetime.datetime.now().replace(microsecond=0).isoformat()
    log = root / "core" / "logs" / "stop-hook.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(
        f"{now} BLOCK sid={RUNNER_SID} agent={AGENT}\n" for _ in range(blocks)),
        encoding="utf-8")
    sensor_path = sdir / "context-budget.json"
    if sensor is None:
        if sensor_path.exists():
            sensor_path.unlink()
    else:
        sensor_path.write_text(json.dumps(sensor), encoding="utf-8")
    return subprocess.run(
        [BASH, str(root / "core" / "scripts" / "loop-exhaustion-fence.sh")],
        capture_output=True, text=True, timeout=120,
        env=_scrubbed_env({"MIND_AGENT": AGENT, "HOOK_SID": RUNNER_SID,
                           "HOOK_LOG": str(log)}),
    )


def test_fence_verdict_is_spelled_stall_and_names_the_sensor_zone(tmp_path):
    _, root = _drive(tmp_path)
    proc = _fire_fence(root, sensor=dict(SENSOR_NORMAL, zone="tight"))
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.startswith("LOOP-STALL PAUSE:"), proc.stdout[:200]
    assert "LOOP-EXHAUSTION" not in proc.stdout
    assert "context budget zone: tight" in proc.stdout, proc.stdout
    assert "cause NOT established" in proc.stdout  # the disclaimer survives
    # pause rung writes NOTHING (the ladder is untouched)
    assert not (_agent_dir(root) / "session" / "stop-requested").exists()


def test_fence_says_unrecorded_when_the_sensor_file_is_absent(tmp_path):
    """Fail-open half: no sensor file -> no flag -> the module's own
    'unrecorded' wording, never a crash, never a hold on the pause rung."""
    _, root = _drive(tmp_path)
    proc = _fire_fence(root, sensor=None)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "context budget zone: unrecorded" in proc.stdout, proc.stdout


def test_fence_zone_is_named_but_never_decisive(tmp_path):
    """zone=fresh with a frozen diary still PAUSES: the zone is carried in the
    reason and does not enter decide() -- the design pin from g-115-8939."""
    _, root = _drive(tmp_path)
    proc = _fire_fence(root, sensor=dict(SENSOR_NORMAL, zone="fresh"))
    assert proc.returncode == 1
    assert proc.stdout.startswith("LOOP-STALL PAUSE:")
    assert "context budget zone: fresh" in proc.stdout
