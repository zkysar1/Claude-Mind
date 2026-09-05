#!/usr/bin/env python3
"""dry-spin-guard.py -- loop-entry backstop for a NARRATED all-blocked yield
(g-357-88).

THE HOLE THIS FILLS. Every other loop-entry fast path CONSUMES state the
previous cycle must have WRITTEN: idle-tick.sh short-circuits on
blocked_sleep_until, dry-idle-cycle-cache.py needs a baseline written by
dry-idle-tick.py, quiescence-cycle-cache.py needs quiescence-last-cycle.json.
So a cycle that routed to all_blocked and then wrote NOTHING -- because the LLM
narrated B6.5/B7/B7.2 as done without running them -- is indistinguishable at
loop entry from a productive cycle, and the loop reloads the full ~75-minute
all-blocked handler back to back.

MEASURED on coach (claude-mind, zc-03) 2026-09-03 02:10Z, recorded in g-357-88's
own description (sibling g-357-89 covers the no-notify half): the pane reported
"Step B7.2 yield complete -- ScheduleWakeup armed for 600s" while
signals.consecutive_blocked_sleeps=0, blocked_sleep_until unset,
quiescence.last_check_at 16h stale, no live sleep job, and ZERO execution-diary
rows across the 66-minute handler. The cycle left no artifact of any kind.

WHAT MAKES THIS DETECTABLE AT ALL: goal-selector.py now writes
loop_state.signals.last_all_blocked at the moment it EMITS the all_blocked
verdict (a script, not the LLM), so the route itself leaves a trace a narrated
handler can neither fake nor suppress. This script reads that trace and asks one
question: did the handler that was supposed to follow it actually DO anything?

THE PREDICATE IS THREE INDEPENDENT ABSENCES, not one. A correctly-executed
handler writes blocked_sleep_until (B7), registers a Tier-A sleep job (B7.2),
and writes execution-diary rows (B6.5/B7/B7.2). Any ONE of those being present
means the handler ran, so the guard stands down. Only when the route is FRESH
and all three are absent is the cycle a dry spin. The redundancy is deliberate
and points the safe way: a missed detection costs one slow cycle (exactly
today's behavior), while a false detection would sleep through live work.

WHY THE AGE GATE IS SHORT AND FIXED. min_reentry_gap_s defaults to 120 --
deliberately the dry-idle base_seconds DEFAULT, not the live base_seconds. A
deployment that raises base_seconds to 7200 for flat 2-hour idle blocks
(g-357-90) must not widen this window to 2h, because the window bounds how long
a STALE marker can keep firing the guard. Past the gap, a genuine long sleep
elapsed and normal entry is correct.

Contract (mirrors dry-idle-cycle-cache.py / quiescence-cycle-cache.py):
  Exit 0 + empty stdout : MISS -- proceed to idle-tick / the full skill chain.
  Exit 0 + stdout text  : "=== DRY-SPIN GUARD ===" directive -- the caller MUST
                          NOT load the all-blocked handler; emit only the one
                          sleep tool call the directive names.

Every path fails open to a MISS. This runs on EVERY loop entry, so the common
path (no marker, or a marker whose sleep is already stamped) is ONE cheap WM
read and nothing else; the diary probe and the dry-idle-tick subprocess run only
after every cheap gate has already voted HIT.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from _paths import AGENT_DIR  # noqa: E402
import _harness_caps  # noqa: E402  (no-notify harness directive lines)
import _dry_idle  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581: never a bare 'bash' argv[0])

CORE_SCRIPTS = Path(__file__).resolve().parent

# Subprocess ceiling for the two helper calls on the HIT path. Generous: these
# are local script invocations, and a timeout fails open to a MISS.
SUBPROC_TIMEOUT_S = 30


# --- readers (each fail-open) ------------------------------------------------

def read_loop_state():
    """loop_state dict via wm-read.sh, or {} on any error.

    WHY THE WRAPPER AND NOT quiescence-gate._wm_read_loop_state, which is
    documented as "the canonical, corruption-tolerant, daemon-routed loop_state
    reader (no second implementation)" -- measured under g-357-88 and corroborated
    by the independent control recorded on g-115-7389: that reader goes through
    _rt.wm_read, and the PYTHON daemon client does not send the X-Mind-Sid
    header the SHELL
    client sends (_runtime.sh rt_curl). The daemon needs that header to resolve
    a Body's per-session WM, so on a worker Body the python path reads the
    AGENT-WIDE WM and returns null for loop_state. Measured 2026-09-04 (cc-07),
    same endpoint and same query from both clients:

        bash   rt_call GET /v1/wm/read?slot=loop_state&json=1 -> {"goals_completed": 82, ...}
        python _rt.rt_call(same)                              -> 'null\\n'
        python _rt.rt_call(same, headers={"X-Mind-Sid": ...}) -> {"goals_completed": 82, ...}

    The marker this guard reads is written to the BODY WM, so the canonical
    reader would return {} here and the guard could never fire on exactly the
    role that runs the loop hardest. This is NOT a second implementation of a
    loop_state read -- it is the store's own sanctioned wrapper, which is the
    documented way to read a store at all (bash-store-write-guard refuses a hand
    parser over working-memory.yaml).

    The underlying SID-parity defect is broader than this goal: it also makes
    dry-idle-cycle-cache._dry_signal() return {} on a worker, which pins that
    Layer-4 fast path's dry_active gate permanently False there. Relayed for the
    reducer rather than fixed inline -- changing _rt.rt_call's headers touches
    every python daemon client in the framework and is not a surgical change to
    make from inside this goal (implementation-discipline.md rule 6).

    COST, measured so nobody has to re-derive it before "optimizing" this back:
    ~150 ms per loop entry through the wrapper vs ~30 ms for the sibling's
    in-process read (cc-07, 3 runs). That 120 ms is paid once per ITERATION,
    against a loop entry that already runs a real `git fetch` + merge — so it is
    not a cost worth trading correctness for. Do not switch this to the
    in-process reader to reclaim it; on a worker that reader returns {} and the
    guard silently stops working, with no error to notice.

    Fail-open: {} on any error -> no marker -> MISS -> normal entry."""
    try:
        r = subprocess.run(
            bash_cmd(str(CORE_SCRIPTS / "wm-read.sh"), "loop_state", "--json"),
            capture_output=True, text=True, timeout=SUBPROC_TIMEOUT_S)
        if r.returncode != 0:
            return {}
        obj = json.loads(r.stdout or "null")
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    # wm-read.sh returns the slot VALUE directly; tolerate an {"value": ...}
    # envelope shape too rather than depending on which one it emits.
    if "value" in obj and isinstance(obj.get("value"), dict):
        obj = obj["value"]
    return obj


def read_marker(loop_state=None):
    """loop_state.signals.last_all_blocked, or None."""
    ls = read_loop_state() if loop_state is None else loop_state
    marker = ((ls.get("signals") or {}).get("last_all_blocked"))
    return marker if isinstance(marker, dict) else None


def _parse_iso(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        return None


def blocked_sleep_remaining(now):
    """Seconds remaining on blocked_sleep_until, or None if unset/unparseable.

    Reads through wm-read.sh for the SAME reason read_loop_state does, and the
    mismatch here would be sharper: B7 WRITES this field with `wm-set.sh`, which
    is bash and therefore sends X-Mind-Sid, landing it in the BODY WM. The
    sibling's `_rt.wm_read(slot=...)` would read the AGENT-WIDE one and see None
    on a worker -- a reader/writer store mismatch (guard-1978 class: which store
    does the writer write DURABLY, and is that the one I am reading?). This
    function is only reached after the cheap gates vote HIT, so the subprocess
    is off the common path."""
    try:
        r = subprocess.run(
            bash_cmd(str(CORE_SCRIPTS / "wm-read.sh"), "blocked_sleep_until"),
            capture_output=True, text=True, timeout=SUBPROC_TIMEOUT_S)
        raw = r.stdout.strip().strip('"') if r.returncode == 0 else None
    except Exception:
        return None
    wake = _parse_iso(raw)
    if wake is None:
        return None
    return (wake - now).total_seconds()


def sleep_job_pending():
    """True iff a background job is registered and pending.

    Fail-open direction here is TRUE (treat as 'a sleep exists' -> MISS): if we
    cannot tell whether the handler registered a sleep, we must not emit a
    second one."""
    try:
        r = subprocess.run(
            bash_cmd(str(CORE_SCRIPTS / "background-jobs.sh"), "has-pending"),
            capture_output=True, text=True, timeout=SUBPROC_TIMEOUT_S)
    except Exception:
        return True
    return r.returncode == 0


def diary_activity_after(marker_at_iso):
    """True iff the execution diary carries a row NEWER than the marker.

    This is the INDEPENDENT-WRITER signal: team-state/WM can be written by the
    same narrated step that lied, but a diary row exists only if a script ran.
    A row after the all_blocked route means the handler (or anything else) did
    real work, so this is not a dry spin.

    Read through execution-diary.sh, never by parsing the JSONL -- the store's
    own wrapper knows the schema, the store-of-record path and the daemon cache
    (bash-store-write-guard refuses a hand parser, correctly).

    Fail-open direction is TRUE (assume activity -> MISS): an unreadable diary
    must not license a sleep."""
    at = _parse_iso(marker_at_iso)
    if at is None:
        return True
    try:
        r = subprocess.run(
            bash_cmd(str(CORE_SCRIPTS / "execution-diary.sh"),
                     "read", "--limit", "1", "--json"),
            capture_output=True, text=True, timeout=SUBPROC_TIMEOUT_S)
        if r.returncode != 0:
            return True
        rows = json.loads(r.stdout or "[]")
    except Exception:
        return True
    if not isinstance(rows, list) or not rows:
        return False  # an empty diary is a genuine absence of activity
    newest = _parse_iso((rows[0] or {}).get("timestamp")) if isinstance(rows[0], dict) else None
    if newest is None:
        return True
    return newest > at


# --- pure decision -----------------------------------------------------------

def evaluate(marker, now, gap_s, blocked_remaining, sleep_registered_job,
             diary_active):
    """Pure decision. Returns (decision, reason) where decision is 'hit'|'miss'.

    Ordered cheap-to-expensive so cmd_check can gate the two subprocess probes
    behind the cheap MISSes. Every negative control the goal names appears here
    as its own named reason, so a test asserts on the REASON and not merely on
    the absence of output."""
    if not isinstance(marker, dict) or not marker:
        # An EMPTY dict is an absent marker, not a corrupt one -- keeping the
        # two reasons distinct matters because "marker-unparseable" would send
        # a reader looking for a writer bug that is not there.
        return ("miss", "no-marker")                    # control 5: older deployments
    if marker.get("sleep_registered"):
        return ("miss", "sleep-already-stamped")        # control 2
    at = _parse_iso(marker.get("at"))
    if at is None:
        return ("miss", "marker-unparseable")
    age = (now - at).total_seconds()
    if age < 0:
        return ("miss", "marker-in-future")             # clock skew -> safe direction
    if age >= gap_s:
        return ("miss", f"marker-stale:{int(age)}s>={int(gap_s)}s")   # control 3
    if blocked_remaining is not None and blocked_remaining > 0:
        return ("miss", "blocked-sleep-active")         # control 1: idle-tick owns it
    if sleep_registered_job:
        return ("miss", "sleep-job-registered")         # control 2 (job side)
    if diary_active:
        return ("miss", "diary-activity-after-marker")  # control 4: route executing
    return ("hit", "narrated-yield")


# --- HIT path ----------------------------------------------------------------

def _run_dry_idle_tick():
    """Run the canonical dry-idle tick and return its parsed verdict, or None.

    A scoped CALL to the single writer, never a local re-derivation of the
    backoff curve: dry-idle-tick.py advances loop_state.signals.dry_idle AND
    writes the dry-idle baseline cache, so the NEXT loop entry short-circuits
    through the ordinary Phase -0.5e.0b fast path instead of coming back here.
    The guard is therefore self-retiring for a sustained dry trough -- it fires
    once, hands the trough to the existing machinery, and stands down."""
    try:
        r = subprocess.run(
            [sys.executable, str(CORE_SCRIPTS / "dry-idle-tick.py"),
             "--executable-count", "0", "--quiescence-decision", "na"],
            capture_output=True, text=True, timeout=SUBPROC_TIMEOUT_S)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout or "{}")
    except Exception:
        return None


def _stamp_sleep(sleep_seconds):
    """Mark the marker's sleep as registered, through the single writer.

    IDEMPOTENCE, and the reason this op exists: without the stamp the guard
    would re-fire against the SAME marker on every entry inside the gap window.
    With it, the first fire is the only fire (evaluate returns
    'sleep-already-stamped' thereafter). Fail-soft -- a failed stamp costs at
    most one extra short cycle, never a wrong sleep."""
    try:
        subprocess.run(
            [sys.executable, str(CORE_SCRIPTS / "loop-state-bump-counters.py"),
             "--all-blocked-sleep", "--sleep-seconds", str(int(sleep_seconds))],
            capture_output=True, text=True, timeout=SUBPROC_TIMEOUT_S)
    except Exception as e:  # pragma: no cover - defensive
        print(f"[dry-spin-guard] sleep stamp skipped ({type(e).__name__}: {e})",
              file=sys.stderr)


def emit_directive(sleep_seconds, marker, streak):
    agent = os.environ.get("MIND_AGENT", "") or "unknown"
    print(
        "=== DRY-SPIN GUARD ===\n"
        f"The previous cycle routed to all_blocked at {marker.get('at')} "
        f"(sid {marker.get('sid') or 'unknown'}) and then wrote NOTHING: no\n"
        "blocked_sleep_until, no registered sleep job, no execution-diary row.\n"
        "That is the signature of a NARRATED B6.5/B7/B7.2 yield, not an executed\n"
        "one -- so the all-blocked handler must NOT be reloaded (that is the dry\n"
        f"spin this guard exists to stop). Dry-idle streak is now {streak}.\n"
        "DO NOT load the all-blocked handler. DO NOT run selection or execution.\n"
        "Emit exactly ONE tool call:\n"
        f"  Bash(\"MIND_AGENT={agent} DRY_SLEEP=1 bash core/scripts/interruptible-sleep.sh {sleep_seconds}\", run_in_background=true)\n"
        "When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.\n"
        + _harness_caps.no_notify_hint(sleep_seconds) +
        "The sleep is registered as a Tier-A background job by interruptible-sleep.sh,\n"
        "so stop-hook Gate 2.6 ALLOWs this turn-end (guard-967 / guard-1230).\n"
        "======================"
    )


# --- check subcommand --------------------------------------------------------

def cmd_check(args):
    now = datetime.now()
    cfg = _dry_idle.load_config()
    gap_s = cfg.get("min_reentry_gap_s", 120)

    marker = read_marker()

    # Cheap gates first: no marker / already stamped / stale age are decided on
    # the single WM read above, with no subprocess at all. This is the common
    # path on every healthy loop entry.
    decision, reason = evaluate(marker, now, gap_s,
                                blocked_remaining=None,
                                sleep_registered_job=False,
                                diary_active=False)
    if decision != "hit":
        if args.explain:
            print(f"[dry-spin-guard] miss: {reason}", file=sys.stderr)
        return 0

    # The cheap gates all voted HIT. Only now pay for the three real probes.
    blocked_remaining = blocked_sleep_remaining(now)
    job_pending = sleep_job_pending()
    diary_active = diary_activity_after(marker.get("at"))

    decision, reason = evaluate(marker, now, gap_s, blocked_remaining,
                                job_pending, diary_active)
    if decision != "hit":
        if args.explain:
            print(f"[dry-spin-guard] miss: {reason}", file=sys.stderr)
        return 0

    # Confirmed dry spin. Hand the trough to the canonical dry-idle machinery
    # and emit the one sleep directive.
    tick = _run_dry_idle_tick()
    if not isinstance(tick, dict) or not tick.get("dry"):
        # The tick declined (backoff disabled, or it does not consider this dry).
        # Fail open: a directive we cannot size is a directive we must not emit.
        if args.explain:
            print("[dry-spin-guard] miss: dry-idle-tick declined "
                  f"({json.dumps(tick) if tick else 'no output'})", file=sys.stderr)
        return 0
    sleep_seconds = int(tick.get("sleep_seconds") or 0)
    if sleep_seconds <= 0:
        if args.explain:
            print("[dry-spin-guard] miss: non-positive sleep_seconds", file=sys.stderr)
        return 0

    _stamp_sleep(sleep_seconds)
    emit_directive(sleep_seconds, marker, tick.get("streak", "?"))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Loop-entry dry-spin guard for narrated all-blocked yields "
                    "(g-357-88)")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="Emit a sleep directive if the previous "
                                     "all_blocked route wrote no sleep state.")
    p.add_argument("--explain", action="store_true",
                   help="Print the MISS reason on stderr (diagnostics only; "
                        "stdout stays empty so the caller contract is unchanged).")
    args = parser.parse_args()
    if args.cmd == "check":
        sys.exit(cmd_check(args))
    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
