#!/usr/bin/env python3
"""Handoff-Aging Escalation — bash-enforce precheck Phase 0.5b.2b ().

Scan the world + agent goal queues for cross-agent handoff goals
(`handoff_to` set, routed to an agent OTHER than self) that have aged past
`handoff_aging.escalate_hours` (default 72) with no recent board escalation,
and post one coordination-board visibility note per aged handoff so the
target agent does not miss it.

WHY THIS EXISTS (the gap g-115-1524 closes): Phase 0.5b.2b was previously
LLM-executed pseudocode with NO bash backstop — unlike its sibling Phase
0.5b.1b (inbox-alert age escalation), which IS bash-enforced via
`inbox-alert-age-check.{py,sh}` (g-115-848). Surfaced by a fresh-eyes-review
on 2026-06-18: the agent's `proactive_escalation_log` was EMPTY despite six
cross-agent handoffs aged 78-782h (one to alpha at 782h / 32 days). An
LLM-only phase silently skips under abbreviation; a bash gate runs every
iteration. This script is that gate (rb-428 sentinel-gate family).

Called by aspirations-precheck Phase 0.5b.2b. Reads the world + agent queues
via the daemon (`_rt.aspirations_read`). Dry-run by default; pass --apply to
actually post the board notes.

Cooldown (g-115-1531 — SHARED + DURABLE): a handoff is re-escalated at most
once per `escalate_hours` window across the WHOLE TEAM. The cooldown record is
the escalation board post itself — before posting, the sweep scans the
coordination board (`board-read.sh`) for an existing `handoff-aged` post for
this goal_id (from ANY agent) within the window. This replaced the original
per-agent WM `proactive_escalation_log` cooldown, which had two production-
confirmed bugs (2026-06-18, ~30 duplicate posts for ~7 handoffs): (1) N-agent
duplicate — each of the 6 agents kept its OWN WM log, so all 6 escalated the
same cross-agent handoff independently; (2) non-durable — a WM reset between
iterations wiped the log and re-fired. A shared, durable board scan fixes both:
one post per window regardless of which agent runs the gate, and board posts
persist across WM resets.

Fail-open at every layer (the action is ADDITIVE board visibility, never a
destructive mutation, so — unlike `defer-recheck.py`'s guard-383 fatal
posture for its destructive defer-clearing aggregate — a half-view from one
unreachable source merely means fewer escalations this run, recoverable on
the next sweep; aborting the precheck would be strictly worse):
  - Missing config block          → escalate_hours = 72 (YAML default)
  - daemon unreachable (either src)→ that source yields [], stderr note, continue
  - board-read scan fails          → empty cooldown set (everything eligible fires)
  - board-post failure (per goal)  → log to stderr; --apply continues to remaining

INBOUND PASS (g-115-5811) — the symmetric half, added 2026-08-11. Everything
above is OUTBOUND: handoffs routed AWAY from self, escalated to the board so
the target agent sees them. There was no pass over work routed TO self, so the
one queue an agent is responsible for DRAINING was the only queue nothing aged.
Measured twice in one day (2026-08-11): a HIGH user directive sat pending
through four cycles, and a HIGH goal sat 111h after its block was cleared —
both found BY HAND during unrelated sweeps, with no automated surface producing
either. The inbound view is emitted under the `inbound` key; every pre-existing
key keeps its name, meaning and value, so existing readers are unaffected.

Three things about it that are load-bearing rather than incidental:
  - It ages on handoff_created_at with a created_at (then started) FALLBACK.
    Measured live: only 2 of 196 inbound goals carry handoff_created_at while
    196 carry created_at, so a handoff_created_at-only pass reports a 2-of-196
    view that is indistinguishable from a clean queue. `age_basis` is reported
    per row because on most rows the age is a created_at proxy, NOT routing age.
  - intended_agent == 'either' is NOT inbound. It means unrouted and is the
    dominant value (898 of 1520 pending), so counting it would swallow most of
    the queue.
  - It is REPORT-ONLY and posts nothing. The outbound pass posts because its
    reader is another agent; this pass's reader is the agent already running it.

Exit codes: always 0. Use the JSON output's `applied` count to determine
what changed.

Usage:
    python3 handoff-aging-check.py [--apply] [--escalate-hours N]
                                   [--agent <name>]                  # default $MIND_AGENT
                                   [--inbound-max-report N]          # non-HIGH cap (default 5)
                                   [--no-inbound]                    # skip the inbound pass
                                   [--board-escalation-log <path>]   # tests only
                                   [--no-board]                      # tests only
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover)
from _runtime_bash import bash_cmd  # : Windows-safe bash resolution


def _parse_iso(s):
    """Parse an ISO-8601 timestamp robustly. Return None on parse failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.rstrip("Z"))
    except Exception:
        return None


def _age_hours(iso_ts: str, now: dt.datetime):
    """Hours between `now` and the parsed timestamp. None on parse failure."""
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


def _load_escalate_hours(args) -> float:
    """Resolve escalate_hours from CLI > YAML handoff_aging.escalate_hours > 72.
    Fail-open: missing YAML or missing key falls back to 72.
    """
    if args.escalate_hours is not None:
        return float(args.escalate_hours)
    try:
        import yaml  # type: ignore
        with open(CORE_ROOT / "config" / "aspirations.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        block = (cfg.get("handoff_aging") or {})
        return float(block.get("escalate_hours", 72))
    except Exception as exc:
        sys.stderr.write(
            "handoff-aging-check: config load failed (%s) — using default 72\n" % exc)
        return 72.0


def _load_inbound_max_report(args) -> int:
    """Cap on reported NON-HIGH inbound rows. CLI > YAML > 5.

    Same fail-open shape as _load_escalate_hours. HIGH rows are reported in
    full regardless of this cap (see _inbound_pass) — the cap exists to stop a
    ~200-row backlog being emitted whole, not to hide priority signal.
    """
    cli = getattr(args, "inbound_max_report", None)
    if cli is not None:
        return int(cli)
    try:
        import yaml  # type: ignore
        with open(CORE_ROOT / "config" / "aspirations.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        block = (cfg.get("handoff_aging") or {})
        return int(block.get("inbound_max_report", 5))
    except Exception as exc:
        sys.stderr.write(
            "handoff-aging-check: inbound config load failed (%s) — using default 5\n" % exc)
        return 5


def _resolve_self_agent(args) -> str:
    """Resolve the bound agent name. CLI --agent > $MIND_AGENT > ''.

    When unresolved (''), no real handoff_to equals '' so every routed-
    elsewhere handoff is treated as eligible — fail toward escalating
    (additive, safe) rather than silently suppressing.
    """
    if args.agent:
        return args.agent
    return os.environ.get("MIND_AGENT", "") or ""


def _read_goals(source: str) -> list:
    """Return list of pending/in-progress goals from world or agent queue.

    FAIL-OPEN (contrast defer-recheck.py guard-383): defer-recheck makes a
    source RtError FATAL because it merges sources to drive a DESTRUCTIVE
    defer-clear — a silent [] there would clear defers on a half-view. This
    sweep's only action is ADDITIVE board escalation, so a missing source
    just escalates fewer handoffs this run (recoverable next sweep). Aborting
    the precheck would be worse. Each goal is tagged with `_source` and
    `_aspiration_id` for downstream context.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        sys.stderr.write(
            "handoff-aging-check: %s read failed (%s) — fail-open, skipping source\n"
            % (source, e.body or e))
        return []
    except Exception as e:
        sys.stderr.write(
            "handoff-aging-check: %s read raised (%s) — fail-open, skipping source\n"
            % (source, e))
        return []
    data = _rt.tolerant_decode_aggregate(f"[handoff-aging-check] {source}", out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            if isinstance(g, dict):
                g["_source"] = source
                g["_aspiration_id"] = asp.get("id")
                goals.append(g)
    return goals


def _escalate_window_str(escalate_hours: float) -> str:
    """board-read --since needs an int+unit duration; round up + 1h margin so
    the read window safely covers the full escalate_hours cooldown window."""
    import math
    return "%dh" % (int(math.ceil(escalate_hours)) + 1)


def _read_recent_escalations(escalate_hours: float, now: dt.datetime,
                             board_log_path: Path = None) -> set:
    """Return the SET of goal_ids that already have a `handoff-aged`
    coordination-board post within `escalate_hours` — from ANY agent.

    THE SHARED, DURABLE COOLDOWN (g-115-1531). The board post this script makes
    (`_post_board`, tagged `handoff-aged,<goal_id>,<handoff_to>`) IS the cooldown
    record: it is shared (every agent reads the same coordination board) and
    durable (board posts persist in world/board/, unlike the per-agent WM
    `proactive_escalation_log` slot each agent kept SEPARATELY and that WM resets
    wiped). Scanning the board before posting therefore fixes BOTH original bugs
    at once — the N-agent duplicate (6 agents each escalating the same
    cross-agent handoff, observed 2026-06-18: ~30 posts for ~7 handoffs) and the
    non-durable cooldown (a WM reset between iterations re-fired the same agent).

    Single source of truth (communication-clarity rule 5): the escalation post
    and the cooldown record are ONE artifact — no separate ledger to keep in sync.

    `board_log_path` (tests only): read a JSON list of post dicts directly,
    bypassing the daemon/subprocess board scan.

    FAIL-OPEN: any read failure yields an empty set → no cooldown → eligible
    handoffs re-escalate (additive, board posts are cheap, recoverable next
    sweep). Same direction as the prior empty-log fail-open.
    """
    posts = []
    if board_log_path is not None:
        try:
            with open(board_log_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            posts = data if isinstance(data, list) else []
        except Exception:
            posts = []
    else:
        try:
            proc = subprocess.run(
                bash_cmd(SCRIPT_DIR / "board-read.sh",
                         "--channel", "coordination",
                         "--type", "status",
                         "--since", _escalate_window_str(escalate_hours),
                         "--json"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        posts.append(json.loads(line))
                    except Exception:
                        continue
            else:
                sys.stderr.write(
                    "handoff-aging-check: board-read.sh exit=%d stderr=%s — "
                    "fail-open (no cooldown this sweep)\n"
                    % (proc.returncode, (proc.stderr or "").strip()[:200]))
        except Exception as exc:
            sys.stderr.write(
                "handoff-aging-check: board-read.sh exception (%s) — fail-open\n" % exc)

    recent = set()
    for p in posts:
        if not isinstance(p, dict):
            continue
        tags = p.get("tags") or []
        if "handoff-aged" not in tags:
            continue
        age = _age_hours(p.get("timestamp") or p.get("ts"), now)
        if age is None or age >= escalate_hours:
            continue  # outside the cooldown window (or unparseable) — does not suppress
        for t in tags:
            # The escalation tags the goal_id (`g-*`); agent-name tags never do.
            if isinstance(t, str) and t.startswith("g-"):
                recent.add(t)
    return recent


def _post_board(goal: dict, handoff_to: str, age_hours: float, no_board: bool) -> tuple:
    """Post one coordination-board visibility note. Returns (ok, detail).

    When `no_board` is True (tests), skip the subprocess and return (True, "no_board").
    """
    title = goal.get("title", "") or ""
    goal_id = goal.get("id", "") or ""
    age_h = age_hours if age_hours is not None else 0.0
    msg = "Handoff aged %.0fh: %s [%s] waiting on %s" % (age_h, title, goal_id, handoff_to)
    if no_board:
        return True, "no_board"
    try:
        tags = "handoff-aged,%s,%s" % (goal_id, handoff_to)
        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "board-post.sh",
                     "--channel", "coordination",
                     "--type", "status",
                     "--tags", tags),
            input=msg,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "posted"
        sys.stderr.write(
            "handoff-aging-check: board-post.sh exit=%d stderr=%s\n"
            % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return False, "board_post_nonzero:%d" % proc.returncode
    except Exception as exc:
        sys.stderr.write(
            "handoff-aging-check: board-post.sh exception (%s) — skipping post\n" % exc)
        return False, "board_post_exception:%s" % exc.__class__.__name__


def _inbound_age(g: dict, now: dt.datetime) -> tuple:
    """Age of an INBOUND goal, with an explicit fallback chain.

    Returns (age_hours, basis) where basis names WHICH field produced the age,
    or (None, None) when no field parses.

    THE FALLBACK IS NOT DEFENSIVE POLISH — WITHOUT IT THIS PASS SEES ALMOST
    NOTHING. Measured on the live queue 2026-08-11 (cc-08): of the 196 pending
    goals routed to alpha, only 2 carry `handoff_created_at` while 196 carry
    `created_at`. A pass aged solely on handoff_created_at would therefore
    report a 2-goal view of a 196-goal backlog — a number that looks like a
    nearly-clean queue and is indistinguishable from one (guard-1802: measure
    what the predicate EXCLUDES, not what it returns).

    `basis` is returned rather than discarded because it changes what the age
    MEANS: on 194 of those 196 the age is a created_at proxy (how long the goal
    has existed) and NOT how long it has been routed to this agent. Reporting
    the number without the basis would silently overstate routing age.
    """
    for field, basis in (("handoff_created_at", "handoff_created_at"),
                         ("created_at", "created_at"),
                         ("started", "started")):
        age = _age_hours(g.get(field), now)
        if age is not None:
            return age, basis
    return None, None


def _inbound_pass(goals: list, self_agent: str, escalate_hours: float,
                  max_report: int, now: dt.datetime) -> dict:
    """Second pass: goals routed TO self that nothing else ages ().

    The outbound pass above enumerates handoffs routed AWAY from self. There was
    no symmetric pass, so the one queue an agent is responsible for DRAINING was
    the one queue nothing aged — measured twice in one day on 2026-08-11
    (a HIGH user directive sat pending through four cycles; a HIGH goal sat 111h
    after its block was cleared), both found BY HAND during unrelated sweeps.

    PREDICATE, measured against the live population rather than assumed:
      status == pending AND (intended_agent == self OR handoff_to == self)
    `intended_agent` is a plain string or None on every live row (never a list),
    and its dominant value is 'either' (898 of 1520 pending) meaning UNROUTED —
    'either' is deliberately NOT inbound, or the pass would swallow 59% of the
    queue and mean nothing. `handoff_to` is set on only 24 pending rows but is
    included because it is the EXPLICIT routing and the outbound pass skips it
    for self by construction (line ~300); on this box it contributes 1 goal the
    intended_agent predicate misses.

    REPORT-ONLY, and that is deliberate. The outbound pass posts to the board
    because its reader is ANOTHER agent who would otherwise never see the
    handoff. This pass's reader is the agent already running it — the precheck
    consumes this stdout every iteration — so a board post would add fleet noise
    with no new reader. Escalation here means NAMING the backlog in the output.

    BOUNDED, because the raw count is noise: 196 candidates on this box. The
    signal is everything HIGH plus the oldest few of the rest. The suppressed
    count is reported so a bounded view is never mistaken for the whole queue.
    """
    scanned_pending = 0
    matched = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        if g.get("status") != "pending":
            continue
        scanned_pending += 1
        if not self_agent:
            continue  # unresolved self: no row can be inbound — say nothing
        if g.get("intended_agent") != self_agent and g.get("handoff_to") != self_agent:
            continue
        age, basis = _inbound_age(g, now)
        if age is None:
            matched.append({
                "goal_id": g.get("id", ""),
                "title": g.get("title", ""),
                "priority": g.get("priority"),
                "age_hours": None,
                "age_basis": None,
                "routed_by": "handoff_to" if g.get("handoff_to") == self_agent else "intended_agent",
            })
            continue
        matched.append({
            "goal_id": g.get("id", ""),
            "title": g.get("title", ""),
            "priority": g.get("priority"),
            "age_hours": round(age, 2),
            "age_basis": basis,
            "routed_by": "handoff_to" if g.get("handoff_to") == self_agent else "intended_agent",
        })

    undateable = [m for m in matched if m["age_hours"] is None]
    aged = [m for m in matched
            if m["age_hours"] is not None and m["age_hours"] >= escalate_hours]
    aged.sort(key=lambda m: m["age_hours"], reverse=True)

    # Everything HIGH, plus the oldest non-HIGH up to the cap. HIGH is never
    # truncated: a HIGH goal silently dropped by an output bound reproduces the
    # exact failure this pass exists to fix.
    high = [m for m in aged if m.get("priority") == "HIGH"]
    rest = [m for m in aged if m.get("priority") != "HIGH"]
    reported = high + rest[:max(0, int(max_report))]

    return {
        "self_agent": self_agent,
        "escalate_hours": escalate_hours,
        "scanned_pending": scanned_pending,
        "matched_count": len(matched),
        "aged_count": len(aged),
        "high_count": len(high),
        "undateable_count": len(undateable),
        "reported": reported,
        "suppressed_count": max(0, len(aged) - len(reported)),
        "max_report": int(max_report),
        "age_basis_breakdown": {
            b: sum(1 for m in matched if m["age_basis"] == b)
            for b in ("handoff_created_at", "created_at", "started")
        },
    }


def run(args) -> dict:
    """Main sweep. Returns the JSON-shape result dict (also printed to stdout)."""
    escalate_hours = _load_escalate_hours(args)
    self_agent = _resolve_self_agent(args)
    now = dt.datetime.now()
    goals = _read_goals("world") + _read_goals("agent")
    board_log_path = Path(args.board_escalation_log) if args.board_escalation_log else None
    recent_escalations = _read_recent_escalations(escalate_hours, now, board_log_path)

    candidates = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        if g.get("status") not in ("pending", "in-progress"):
            continue
        ht = g.get("handoff_to")
        if not ht or ht == self_agent:
            continue  # only goals routed to ANOTHER agent
        created = g.get("handoff_created_at")
        if not created:
            continue
        age = _age_hours(created, now)
        if age is None or age < escalate_hours:
            continue
        goal_id = g.get("id", "")
        candidates.append({
            "goal_id": goal_id,
            "title": g.get("title", ""),
            "handoff_to": ht,
            "age_hours": round(age, 2),
            "blocker_id": "handoff_%s" % goal_id,
            "on_cooldown": goal_id in recent_escalations,
        })

    fired = []
    skipped_cooldown = []
    failed = []
    if args.apply:
        for c in candidates:
            if c["on_cooldown"]:
                skipped_cooldown.append(c["goal_id"])
                continue
            full = next((g for g in goals if g.get("id") == c["goal_id"]), None)
            if full is None:
                continue
            ok, detail = _post_board(full, c["handoff_to"], c["age_hours"], args.no_board)
            # No separate cooldown write: the board post above IS the shared,
            # durable cooldown record the next sweep (any agent) reads.
            if ok:
                fired.append({
                    "goal_id": c["goal_id"],
                    "handoff_to": c["handoff_to"],
                    "age_hours": c["age_hours"],
                    "detail": detail,
                })
            else:
                failed.append({
                    "goal_id": c["goal_id"],
                    "handoff_to": c["handoff_to"],
                    "detail": detail,
                })

    result = {
        "mode": "apply" if args.apply else "dry_run",
        "self_agent": self_agent,
        "escalate_hours": escalate_hours,
        "scanned": len(goals),
        "candidates": candidates,
        "candidate_count": len(candidates),
        "applied": len(fired),
        "fired": fired,
        "skipped_cooldown": skipped_cooldown,
        "failed": failed,
    }
    # Inbound pass (). ADDITIVE: every key above is unchanged in name,
    # meaning and value, so existing readers of this JSON are unaffected. The
    # inbound view lives entirely under its own `inbound` key.
    # getattr rather than attribute access: run() is importable and callers
    # build their own Namespace (the test helper does). A hard access makes
    # every pre-existing caller AttributeError the moment a new optional flag
    # lands — which is exactly what happened when this pass was first wired.
    if not getattr(args, "no_inbound", False):
        result["inbound"] = _inbound_pass(
            goals, self_agent, escalate_hours,
            _load_inbound_max_report(args), now)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="Actually post board notes and append cooldown entries (default: dry-run).")
    p.add_argument("--escalate-hours", type=float, default=None,
                   help="Override the aging threshold (default: config handoff_aging.escalate_hours or 72).")
    p.add_argument("--agent", default=None,
                   help="Self agent name; handoffs routed to this agent are skipped (default: $MIND_AGENT).")
    p.add_argument("--board-escalation-log", default=None,
                   help="Test-only: path to a JSON file of coordination-board posts standing in for the live board scan.")
    p.add_argument("--no-board", action="store_true",
                   help="Test-only: skip the board-post.sh subprocess and pretend it succeeded.")
    p.add_argument("--inbound-max-report", type=int, default=None,
                   help="Cap on reported NON-HIGH inbound rows (default: config "
                        "handoff_aging.inbound_max_report or 5). HIGH rows are never capped.")
    p.add_argument("--no-inbound", action="store_true",
                   help="Skip the inbound pass entirely (escape hatch; the outbound "
                        "result keys are unaffected either way).")
    args = p.parse_args()
    result = run(args)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
