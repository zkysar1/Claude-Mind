#!/usr/bin/env python3
"""Handoff-Aging Escalation — bash-enforce precheck Phase 0.5b.2b (4).

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

Exit codes: always 0. Use the JSON output's `applied` count to determine
what changed.

Usage:
    python3 handoff-aging-check.py [--apply] [--escalate-hours N]
                                   [--agent <name>]                  # default $MIND_AGENT
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

    return {
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
    args = p.parse_args()
    result = run(args)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
