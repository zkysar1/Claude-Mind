"""Tests for the --restart min-interval rate gate in mind-api-start.sh ().

WHY THIS GATE EXISTS. Nothing bounded how OFTEN a healthy daemon could be
recycled. guard-1151 (the claim-liveness gate immediately above it) is
CONDITION-gated, not RATE-gated: it refuses only when the calling agent's
in-flight claim is stale, so a caller with a fresh claim — or one passing no
MIND_AGENT at all — was completely unbounded. A caller in a retry loop
recycled the daemon ~25x in 12 minutes and every call was honoured.

WHAT THESE TESTS PIN, and why each earns its place:

  - SCOPE. The gate must sit ONLY on the healthy+--restart branch. The
    unhealthy / stale / orphan recovery paths must stay unrated: they restart a
    daemon that is ALREADY broken, and rate-limiting them would convert a
    transient crash into an outage that heals no sooner than the interval.
    Recycling a healthy daemon is an optimisation; restarting a dead one is
    recovery. This is the single most dangerous way to get the change wrong,
    and it is invisible to any behavioural test that only drives the healthy
    path — hence a structural assertion.
  - FAIL-OPEN. Every error path (absent, empty, garbage, future-dated stamp)
    must ALLOW. A bug in a rate-limiter must never be able to block a
    legitimate recycle.
  - STAMP-ON-RECYCLE-ONLY. The stamp is written where the recycle happens, not
    on the refusal. If a refusal stamped, a caller in a tight retry loop would
    keep pushing its own window forward and could never get through — turning a
    rate limit into a permanent lockout.
  - NO DRIFT. The behavioural tests execute the gate's decision logic EXTRACTED
    FROM THE SCRIPT ITSELF rather than a hand-copied duplicate, so the test
    cannot silently pass against a stale copy of the predicate.

NOT COVERED HERE, stated rather than implied: the end-to-end ALLOW path (stamp
older than the interval -> actual recycle) is not driven, because observing it
requires recycling a real healthy daemon. On a box serving a live fleet that is
the daemon-storm shape this gate exists to prevent. The REFUSE path WAS verified
end-to-end against the real script on cc-03 (Linux 6.8.0-136-generic,
2026-08-02): the refusal appeared in spawn.log with the rate-gate wording and the
live daemon's PID was unchanged across the call.
"""
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Never a bare "bash" argv[0] (guard-580): on Windows that resolves via
# CreateProcess, which searches System32 BEFORE PATH and reaches the WSL
# launcher — which sees the repo under /mnt/c, strips the env _paths.sh needs,
# and can hang past the timeout.
from _bash_helpers import BASH  # noqa: E402

SCRIPT = SCRIPT_DIR.parent / "mind-api-start.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8", errors="replace")


def _extract_gate_block() -> str:
    """Pull the live gate block out of the script so tests cannot drift from it."""
    src = _source()
    start = src.index('_rr_stamp="$RT_DIR/last-restart"')
    end = src.index("_log \"daemon healthy", start)
    return src[start:end]


def _decide(stamp_contents, *, minimum="60", force="0"):
    """Run the REAL extracted gate logic and report REFUSE vs ALLOW."""
    block = _extract_gate_block()
    # Neutralise the two side-effecting calls: _log is not defined in this
    # harness, and the stamp write belongs to the recycle path, not the decision.
    block = block.replace("_log ", "printf '%s\\n' ")
    block = re.sub(r"date \+%s > \"\$_rr_stamp\".*", "", block)
    script = f"""
set -u
RT_DIR="$1"
MIND_RESTART_MIN_INTERVAL_S="{minimum}"
MIND_RESTART_FORCE_RATE="{force}"
{block}
echo "ALLOW"
"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        if stamp_contents is not None:
            (Path(td) / "last-restart").write_text(str(stamp_contents), encoding="utf-8")
        p = subprocess.run([BASH, "-c", script, BASH, td],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode in (0, 3), p.stderr
        return "REFUSE" if "REFUSED --restart" in p.stdout else "ALLOW"


# ═══ the decision table ═══


def test_fresh_stamp_refuses():
    assert _decide(int(time.time())) == "REFUSE"


def test_stamp_older_than_interval_allows():
    assert _decide(int(time.time()) - 120) == "ALLOW"


def test_absent_stamp_fails_open():
    assert _decide(None) == "ALLOW", "first-ever restart must not be blocked"


def test_empty_stamp_fails_open():
    assert _decide("") == "ALLOW"


def test_garbage_stamp_fails_open():
    assert _decide("not-a-number") == "ALLOW"


def test_future_stamp_fails_open():
    """Clock skew must not be able to lock out restarts."""
    assert _decide(int(time.time()) + 9999) == "ALLOW"


def test_zero_interval_disables_the_gate():
    assert _decide(int(time.time()), minimum="0") == "ALLOW"


def test_override_env_bypasses():
    assert _decide(int(time.time()), force="1") == "ALLOW"


# ═══ scope: the load-bearing structural property ═══


def test_gate_is_scoped_to_the_healthy_restart_branch_only():
    """Recovery paths must stay unrated — a dead daemon must restart immediately.

    Mutation-sensitive: moving the gate below the healthy branch, or duplicating
    it into a recovery path, fails this.
    """
    src = _source()
    gate_at = src.index('_rr_stamp="$RT_DIR/last-restart"')
    # The gate must land AFTER the claim-liveness gate (same branch, guard-1151)
    claim_at = src.index("REFUSED --restart: claim on in-flight goal")
    assert claim_at < gate_at, "rate gate must sit inside the healthy+--restart branch"
    # ...and BEFORE that branch's recycle.
    recycle_at = src.index('_log "daemon healthy', gate_at)
    assert gate_at < recycle_at

    # The recovery branches must not mention the rate stamp at all.
    tail = src[recycle_at:]
    for marker in ("alive but not responding on port",
                   "stale PID file",
                   "alive but no port file"):
        assert marker in tail, f"expected recovery branch {marker!r} after the healthy branch"
    assert "last-restart" not in tail, (
        "a recovery path references the rate stamp — restarting an ALREADY-BROKEN "
        "daemon must never be rate-limited (that turns a crash into an outage)")


def test_stamp_is_written_on_recycle_not_on_refusal():
    """A refusal that stamped would push the caller's own window forward forever."""
    src = _source()
    refuse_at = src.index("REFUSED --restart: last recycle was")
    write_at = src.index('date +%s > "$_rr_stamp"')
    assert refuse_at < write_at, "stamp write must follow the refusal branch"
    # The refusal exits before reaching the write.
    between = src[refuse_at:write_at]
    assert "exit 3" in between, "refusal must exit before the stamp write is reached"


def test_override_and_tuning_envs_are_documented_in_the_refusal():
    """A gate that blocks without naming its escape hatch is a dead end (guard-1532)."""
    src = _source()
    msg_start = src.index("REFUSED --restart: last recycle was")
    msg = src[msg_start:msg_start + 700]
    assert "MIND_RESTART_FORCE_RATE=1" in msg
    assert "MIND_RESTART_MIN_INTERVAL_S" in msg
