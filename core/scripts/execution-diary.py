#!/usr/bin/env python3
"""Execution diary — append-only reasoning breadcrumb trail.

Captures key decision points, failures, findings, and approach changes during
goal execution. Unlike WM slots (which are overwritten), the diary is cumulative.
It survives autocompact because it's on disk, and the postcompact restore reads
the last N entries to inject into fresh context.

Subcommands:
  append       — Add a diary entry from stdin JSON
  read         — Read recent entries (--limit, --since, --goal)
  summary      — Generate compressed summary for post-compact injection
  trim         — Remove entries older than N hours
  phase-start  — Emit a phase-start marker (Tier 0 token-cost telemetry)
  phase-end    — Emit a phase-end marker (Tier 0 token-cost telemetry)

Phase markers (Tier 0, plan: i-had-one-agent-luminous-reddy.md):
  Each phase-start/phase-end pair brackets a loop phase. phase-cost-report.py
  consumes these to compute per-phase wall-clock duration (token-cost proxy).
  Pairing is greedy by time order on (phase_name, iteration) key.
  Nested phases are allowed — a sub-phase can start while its parent is open.
  Name convention: "phase-<num>[-<sub>]" chosen by the emitting SKILL.md.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import AGENT_DIR, assert_agent_dir

# : fail loud at import time if MIND_AGENT unset; replaces the
# opaque `None / "session"` TypeError class the next line would otherwise raise.
assert_agent_dir("execution-diary")

DIARY_PATH = AGENT_DIR / "session" / "execution-diary.jsonl"


def _advance_heartbeat():
    """Sync the runner-heartbeat with this diary write.

    Recovery-gate.sh Condition 2 (heartbeat-stale) and Condition 2.7 (diary-
    stale) are designed as independent multi-signal liveness probes. They
    drifted apart in practice: heartbeat-tick fires ONLY at Phase -0.5 of each
    iteration, but diary appends fire at every phase boundary and finding
    emission. A single goal spanning >30 min between iteration boundaries
    (zeta g-271-19 took 75 min on 2026-05-13) staled heartbeat while diary
    appends kept happening, triggering false-positive recovery.

    Re-sync mechanism: every successful diary write ticks the heartbeat by
    direct file touch. The two staleness signals now move together.

    Why direct touch (not heartbeat-tick.sh subprocess): on Windows + Git
    Bash, Python's subprocess.run cannot propagate env vars (including
    MIND_AGENT) reliably to the bash child — empirically verified
    2026-05-14. Calling heartbeat-tick.sh that way produces an empty
    AGENT_DIR and a touch failure. The state gate below (refuse when
    state=IDLE) mirrors heartbeat-tick.sh's guard, so the contract is
    preserved. team-state.agent_status.last_active is not synced from
    here — Phase -0.5 (the canonical heartbeat-tick caller) covers that on
    each iteration boundary, and team-state staleness has its own thresholds.

    Fail-open: any error swallowed. A diary write must NEVER fail because
    heartbeat propagation had an opinion.
    """
    # DO NOT add a mkdir(parents=True) call here — every caller of this
    # function (cmd_append, _emit_phase_marker) has already created the
    # session/ parent dir before its diary write. Adding it here would be
    # cognitive-load dead code: the second writer never observes the dir
    # absent.
    try:
        state_file = AGENT_DIR / "session" / "agent-state"
        if state_file.exists():
            state = state_file.read_text(encoding="utf-8").strip()
            if state == "IDLE":
                return  # mirrors heartbeat-tick.sh:67-70 state gate
        hb = AGENT_DIR / "session" / "runner-heartbeat"
        hb.touch()
    except Exception:
        pass

VALID_ENTRY_TYPES = {
    "decision", "failure", "finding", "approach_change",
    "observation", "state_update",
    "phase_start", "phase_end",
    # Scorer Sovereignty Layer B (): a sanctioned deviation from the
    # scorer's top pick, logged by scorer_verdict_gate.py. Distinct type so the
    # Layer C audit can filter overrides via `read --entry-type scorer_override`
    # rather than grepping content (esp. the audited force-override safety valve).
    "scorer_override",
}


def _is_observer_session():
    """Return True if MIND_SID is set and differs from running-session-id.

    Parallel to guard-135, guard-340, guard-517 and the 7th witness in
    session-save-id.sh: observer sessions (assistant/reader coexisting with
    the autonomous runner on the same agent) must not write to runner-owned
    diary entries — their phase markers create dangling pairs and their
    writes interleave with the runner's stream (canonical incident:
    2026-05-10 bravo observer phase_start phase-4-execute for g-001-01).

    Fail-open at every degraded condition (MIND_SID empty, file absent,
    file unreadable) to match stop-hook.sh Gate 0 / session-save-id.sh
    witness fallbacks. Bootstrap (no runner yet) is fall-through.
    """
    sid = os.environ.get("MIND_SID", "")
    if not sid:
        return False
    runner_file = AGENT_DIR / "session" / "running-session-id"
    if not runner_file.exists():
        return False
    try:
        runner_sid = runner_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(runner_sid) and runner_sid != sid


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def read_entries():
    """Read all diary entries."""
    if not DIARY_PATH.exists():
        return []
    entries = []
    with open(DIARY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def cmd_append(args):
    """Add a diary entry from stdin JSON."""
    if _is_observer_session():
        sys.exit(0)
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: No input on stdin", file=sys.stderr)
        sys.exit(1)

    try:
        entry = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(entry, dict):
        print("ERROR: Entry must be a JSON object", file=sys.stderr)
        sys.exit(1)

    # Validate entry_type — fail-close (). Previously fail-open (WARN + write)
    # let coop_stop slip through in session 54. rb-387 captured the pattern; this flip
    # makes the allow-list authoritative at write time, matching the defence-in-depth
    # verify-learning lint at .claude/skills/verify-learning/SKILL.md Section 308.
    etype = entry.get("entry_type", "")
    if etype not in VALID_ENTRY_TYPES:
        # Missing entry_type fails too (-adjacent, 2026-07-17): an
        # entry keyed "type" instead of "entry_type" previously passed this
        # gate and crashed live-phase-emit.sh on every heartbeat tick until
        # the tail advanced.
        print(f"ERROR: Unknown entry_type '{etype}' — valid types: {', '.join(sorted(VALID_ENTRY_TYPES))}",
              file=sys.stderr)
        sys.exit(1)

    # Auto-add timestamp if missing
    if "timestamp" not in entry:
        entry["timestamp"] = now_iso()

    # Ensure required fields
    if "content" not in entry:
        print("ERROR: Entry must have 'content' field", file=sys.stderr)
        sys.exit(1)

    # Append atomically
    DIARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DIARY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    _advance_heartbeat()
    print(f"ok: {entry.get('entry_type', '?')} @ {entry['timestamp']}")


def cmd_read(args):
    """Read recent diary entries."""
    entries = read_entries()

    # Filter by goal
    if args.goal:
        entries = [e for e in entries if e.get("goal_id") == args.goal]

    # Filter by time. Parse first so an invalid --since fails fast instead of
    # silently returning the unfiltered list (violates caller intent — see 
    # audit,  fix). Exit 2 follows the "usage error" convention.
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since)
        except ValueError:
            print(
                f"ERROR: Invalid --since timestamp: {args.since}. "
                "Use ISO format (e.g., 2026-04-17T12:00:00).",
                file=sys.stderr,
            )
            sys.exit(2)
        entries = [e for e in entries if _parse_ts(e) and _parse_ts(e) >= since_dt]

    # Apply limit (from the end)
    if args.limit and args.limit > 0:
        entries = entries[-args.limit:]

    if args.json:
        print(json.dumps(entries, ensure_ascii=False, default=str))
    else:
        for entry in entries:
            ts = entry.get("timestamp", "")
            time_part = ts[11:16] if len(ts) >= 16 else ts
            goal = entry.get("goal_id", "")
            etype = entry.get("entry_type", "")
            content = str(entry.get("content", ""))[:200]
            print(f"[{time_part}] {goal} {etype}: {content}")


def cmd_summary(args):
    """Generate compressed summary of recent entries."""
    entries = read_entries()
    if not entries:
        print("no diary entries")
        return

    # Last N entries
    limit = args.limit or 10
    recent = entries[-limit:]

    # Group by goal
    by_goal = {}
    for e in recent:
        gid = e.get("goal_id", "unknown")
        by_goal.setdefault(gid, []).append(e)

    lines = []
    for gid, goal_entries in by_goal.items():
        types = {}
        for e in goal_entries:
            etype = e.get("entry_type", "?")
            types[etype] = types.get(etype, 0) + 1
        type_str = ", ".join(f"{k}:{v}" for k, v in types.items())
        last_content = str(goal_entries[-1].get("content", ""))[:100]
        lines.append(f"{gid}: {len(goal_entries)} entries ({type_str}) — last: {last_content}")

    print(f"Diary: {len(entries)} total, {len(recent)} recent")
    for line in lines:
        print(f"  {line}")


def cmd_trim(args):
    """Remove entries older than N hours."""
    entries = read_entries()
    if not entries:
        print("no entries to trim")
        return

    hours = args.hours or 8
    cutoff = datetime.now() - timedelta(hours=hours)
    kept = []
    removed = 0

    for entry in entries:
        ts = _parse_ts(entry)
        if ts and ts < cutoff:
            removed += 1
        else:
            kept.append(entry)

    if removed == 0:
        print(f"no entries older than {hours}h")
        return

    # Rewrite file with kept entries
    tmp = DIARY_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in kept:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    os.replace(str(tmp), str(DIARY_PATH))

    print(f"trimmed {removed} entries older than {hours}h, {len(kept)} remaining")


# POST_RECOVERY_EDIT_OVERRIDE="User-directed framework fix for hung-autocompact false-positive recovery; implementing before /start delta to prevent immediate repeat."
def _maintain_execute_in_flight(kind, phase):
    """Write/clear the execute-in-flight sentinel based on phase transitions.

    The sentinel marks "agent is mid-Phase-4-execute" for recovery-gate.sh
    Path A and Path C suppressors. Deep code work can run >60 min without a
    phase boundary or diary write (canonical incident: 2026-05-22 delta
    g-115-1017 Phase 4 = 1h 31m), staling all liveness signals. Without the
    sentinel, recovery-gate false-positive-fires on actively-working agents.

    Lifecycle:
      - phase_start phase-4-execute  → write sentinel
      - phase_end   phase-4-execute  → delete sentinel
      - phase_start of any other phase → delete sentinel (defensive)

    Fail-open: any error swallowed. Diary writes must NEVER fail because
    sentinel maintenance had an opinion.
    """
    try:
        sentinel = AGENT_DIR / "session" / "execute-in-flight"
        if kind == "phase_start" and phase == "phase-4-execute":
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(now_iso(), encoding="utf-8")
        elif kind == "phase_end" and phase == "phase-4-execute":
            sentinel.unlink(missing_ok=True)
        elif kind == "phase_start":
            sentinel.unlink(missing_ok=True)
    except Exception:
        pass


# Goal-shaped token, e.g.  / g-xw-20260730T020622-01 / pt-003.
# Used only for malformed-call adoption below — deliberately narrow.
_GOAL_TOKEN_RE = re.compile(r"^(?:g|pt)-[A-Za-z0-9][A-Za-z0-9-]*$")


def _emit_phase_marker(kind, phase, iteration, goal_id, note):
    """Shared implementation for phase-start / phase-end markers."""
    if _is_observer_session():
        sys.exit(0)
    phase = (phase or "").strip()
    if not phase:
        print("ERROR: --phase (or positional name) is required", file=sys.stderr)
        sys.exit(2)
    # Malformed-call adoption (): a phase quoted together with extra
    # tokens ("phase-4-execute ") used to be accepted VERBATIM — the
    # goal id landed in the phase string, goal_id stayed empty, and every
    # goal_id-keyed consumer broke at once: stranded-claim-sweep's
    # _diary_has_entry_after keep-signal found no post-claim entry and released
    # a live mid-execution claim (the completed-without-claim incident), the
    # exact-match _maintain_execute_in_flight below never armed the recovery
    # suppressor, and phase-cost pairing keyed on the padded name. Split the
    # string: first token is the phase; a goal-shaped token becomes goal_id
    # (an explicit --goal always wins); anything else folds into the note.
    tokens = phase.split()
    if len(tokens) > 1:
        phase = tokens[0]
        extras = []
        for tok in tokens[1:]:
            if not goal_id and _GOAL_TOKEN_RE.match(tok):
                goal_id = tok
                print(f"note: adopted goal_id={tok} from space-embedded phase "
                      f"argument (pass --goal {tok} instead)", file=sys.stderr)
            else:
                extras.append(tok)
        if extras:
            note = f"{note} {' '.join(extras)}" if note else " ".join(extras)
    entry = {
        "entry_type": kind,
        "phase": phase,
        "timestamp": now_iso(),
        "content": note or f"{kind} {phase}",
    }
    if iteration is not None:
        entry["iteration"] = iteration
    if goal_id:
        entry["goal_id"] = goal_id
    DIARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DIARY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    _advance_heartbeat()
    _maintain_execute_in_flight(kind, phase)
    print(f"ok: {kind} {phase} @ {entry['timestamp']}")


def cmd_phase_start(args):
    _emit_phase_marker("phase_start", args.phase, args.iter, args.goal, args.note)


def cmd_phase_end(args):
    _emit_phase_marker("phase_end", args.phase, args.iter, args.goal, args.note)


def _parse_ts(entry):
    """Parse timestamp from entry, return datetime or None."""
    ts = entry.get("timestamp", "")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def build_parser():
    parser = argparse.ArgumentParser(description="Execution diary — append-only reasoning trail")
    sub = parser.add_subparsers(dest="command", required=True)

    # append
    sub.add_parser("append", help="Add entry from stdin JSON")

    # read
    p_read = sub.add_parser("read", help="Read recent entries")
    p_read.add_argument("--limit", type=int, default=None, help="Max entries to return (from end)")
    p_read.add_argument("--since", type=str, default=None, help="Only entries after this ISO timestamp")
    p_read.add_argument("--goal", type=str, default=None, help="Filter by goal_id")
    p_read.add_argument("--json", action="store_true", help="Output as JSON array")

    # summary
    p_sum = sub.add_parser("summary", help="Compressed summary of recent entries")
    p_sum.add_argument("--limit", type=int, default=10, help="Max entries to summarize")

    # trim
    p_trim = sub.add_parser("trim", help="Remove entries older than N hours")
    p_trim.add_argument("--hours", type=int, default=8, help="Hours threshold (default: 8)")

    # phase-start / phase-end (Tier 0 telemetry)
    for name, helptxt in (
        ("phase-start", "Emit a phase-start marker"),
        ("phase-end", "Emit a phase-end marker"),
    ):
        p = sub.add_parser(name, help=helptxt)
        p.add_argument("phase", type=str, help="Canonical phase name, e.g. phase-0 or phase-0-zombies")
        p.add_argument("--iter", type=int, default=None, help="Iteration id (optional; for pairing across nested phases)")
        p.add_argument("--goal", type=str, default=None, help="Current goal_id (optional)")
        p.add_argument("--note", type=str, default=None, help="Optional short note")

    return parser


DISPATCH = {
    "append": cmd_append,
    "read": cmd_read,
    "summary": cmd_summary,
    "trim": cmd_trim,
    "phase-start": cmd_phase_start,
    "phase-end": cmd_phase_end,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
