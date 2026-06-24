#!/usr/bin/env python3
"""Inbox-Alert Age Escalation - scan  for aged alert-sweep Unblocks.

Closes finding (2) of g-115-822: when alert-sweep.sh files an Unblock for an
inbound alert email and no agent claims it within a few hours, the alert
silently ages. The bash gate is already in place upstream (alert-sweep.sh
files Unblock goals with `origin_signal=f"alert-email:{s3_key}"`); this
script is the precheck-side aging-escalation sweep - equivalent to Phase
0.5b.1 (blocker_age_hours) but for the goal-queue surface rather than the
working-memory `known_blockers` surface.

Called by aspirations-precheck Phase 0.5b.1b. Reads asp-115 via the daemon
(_rt.aspirations_read). Dry-run by default; pass --apply to actually fire
notifications.

Severity is determined by which configured interval the goal's age has passed.
`config.proactive_escalation.inbox_alert_age_hours.{high,medium}` are the
per-severity RE-NOTIFY intervals (a "high" alert re-notifies more frequently);
classification maps the LONGER-aged alert to the MORE-urgent "high" severity
(g-115-1539):
  - age >= max(high_hours, medium_hours) -> severity=high   (aged furthest; re-notify every high_hours)
  - age >= min(high_hours, medium_hours) -> severity=medium (aged moderately; re-notify every medium_hours)
  - otherwise                            -> skip (under threshold)
With the defaults (high=4h, medium=12h) a "high" alert is reached once aged past
12h and re-notifies every 4h (frequent); "medium" is reached at 4h and
re-notifies every 12h. (Before g-115-1539 the classifier checked high_hours
first, so with high<medium the medium branch was unreachable dead code.)

A goal that crosses the high threshold AFTER it already received a medium
notification re-fires under the high schedule: the cooldown lookup keys on the
SAME goal_id but compares the most-recent escalation age against the CURRENT
severity's threshold, so a fresh "upgraded to HIGH" notification reaches the
user as an alert ages further. Same pattern as Phase 0.5b.1 -> Phase B7 ladder.

Cooldown (g-115-1533 - SHARED + DURABLE, mirroring the g-115-1531 handoff
sibling): an aged alert is re-escalated at most once per cooldown window across
the WHOLE TEAM. The cooldown record is a coordination-board breadcrumb the
escalation posts (`inbox-alert-aged,<goal_id>,severity:<sev>`): before sending
the user email, the sweep scans the coordination board (`board-read.sh`) for an
existing `inbox-alert-aged` post for this goal_id (from ANY agent) within the
cooldown window. This replaced the original per-agent WM `proactive_escalation_log`
cooldown, which carried the SAME two bugs g-115-1531 fixed in handoff-aging-check:
(1) N-agent duplicate - each of the N agents kept its OWN WM log, so all N
escalated the same unclaimed alert independently (N DUPLICATE USER EMAILS,
arguably worse than the board-post duplication of the handoff sibling);
(2) non-durable - a WM reset between iterations wiped the log and re-fired.

WHY the board, not a new world-level ledger: the board IS the framework's
shared-durable-append primitive (locking + persistence handled by
board-post.sh / board-read.sh). Reusing it beats inventing a locked world-level
JSONL ledger (rb-1534 multi-writer race concern) and keeps BOTH age-escalation
sweeps on ONE pattern. The breadcrumb also gives partner agents cross-agent
visibility into aging unclaimed alerts (a coordination signal: "someone should
claim this"), so the post is genuinely useful, not just a cooldown hack.

KEY DIFFERENCE from the handoff sibling: there the board post is BOTH the
escalation and the cooldown; here the USER-FACING escalation is the email and
the board post is the SHARED cooldown record + agent-facing visibility.

Fail-open at every layer (the action is ADDITIVE escalation, never a
destructive mutation):
  - Missing config block          -> fall back to high=4, medium=12 (YAML defaults)
  - daemon unreachable            -> exit 0, empty `candidates`, stderr note
  - asp-115 not present in world  -> exit 0, empty `candidates`
  - board scan fails              -> empty cooldown set (everything eligible fires)
  - email-send failure (per goal) -> log to stderr; STILL post the board
                                    breadcrumb to avoid retry-storm; --apply
                                    continues to remaining candidates

Exit codes: always 0. Use the JSON output's `applied` count to determine
what changed.

Usage:
    python3 inbox-alert-age-check.py [--apply] [--asp-id asp-115]
                                     [--high-hours N] [--medium-hours N]
                                     [--board-escalation-log <path>]  # tests only
                                     [--no-email] [--no-board]        # tests only
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
from _runtime_bash import bash_cmd  # noqa: E402  # : Windows-safe bash resolution


def _parse_iso(s):
    """Parse an ISO-8601 timestamp robustly. Return None on parse failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        # Allow trailing Z for UTC (alert-sweep writes local-time, but tolerate either).
        return dt.datetime.fromisoformat(s.rstrip("Z"))
    except Exception:
        return None


def _age_hours(iso_ts: str, now: dt.datetime) -> float:
    """Hours between `now` and the parsed timestamp. None on failure."""
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


def _load_config(args) -> dict:
    """Resolve high_hours / medium_hours from CLI > YAML > YAML-default.
    Fail-open: missing YAML or missing keys fall back to (4, 12).
    """
    high = args.high_hours
    medium = args.medium_hours
    if high is not None and medium is not None:
        return {"high": float(high), "medium": float(medium)}
    try:
        import yaml  # type: ignore
        with open(CORE_ROOT / "config" / "aspirations.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        pe = (cfg.get("proactive_escalation") or {})
        block = (pe.get("inbox_alert_age_hours") or {})
        if high is None:
            high = block.get("high", 4)
        if medium is None:
            medium = block.get("medium", 12)
    except Exception as exc:
        # Fail-open: stderr note + YAML defaults.
        sys.stderr.write(
            "inbox-alert-age-check: config load failed (%s) - using defaults\n" % exc)
        if high is None:
            high = 4
        if medium is None:
            medium = 12
    return {"high": float(high), "medium": float(medium)}


def _read_aspiration(asp_id: str) -> dict:
    """Read a single aspiration from the world queue. Empty dict on failure."""
    try:
        import _rt
        raw = _rt.aspirations_read(source="world", asp_id=asp_id)
        return json.loads(raw) if raw else {}
    except Exception as exc:
        sys.stderr.write(
            "inbox-alert-age-check: aspirations_read(%s) failed (%s) - fail-open\n"
            % (asp_id, exc))
        return {}


def _classify_severity(age_hours: float, thresholds: dict) -> str:
    """Return "high", "medium", or "" (alert under both thresholds).

    The two configured values are per-severity RE-NOTIFY intervals, reused by
    run()'s cooldown (`recent_age < thresholds[sev]`): a "high" alert re-notifies
    every thresholds["high"] (default 4h, frequent), a "medium" every
    thresholds["medium"] (default 12h, less frequent). Classification maps the
    LONGER-aged alert to the MORE-urgent "high" severity -- so the age gate for
    "high" is the LARGER of the two intervals, NOT thresholds["high"]. This
    matches the module docstring's "upgraded to HIGH ... as an alert ages
    further" escalation intent.

    g-115-1539: the prior code checked `age >= thresholds["high"]` FIRST; with
    the defaults high(4) < medium(12) that branch caught everything >= 4h, so the
    medium branch was unreachable dead code and every aged alert classified
    "high" (re-notifying every 4h). Using max()/min() keeps the older->high
    mapping correct regardless of which config key holds the larger value.
    """
    if age_hours is None:
        return ""
    longer = max(thresholds["high"], thresholds["medium"])
    shorter = min(thresholds["high"], thresholds["medium"])
    if age_hours >= longer:
        return "high"
    if age_hours >= shorter:
        return "medium"
    return ""


def _escalate_window_str(max_window_hours: float) -> str:
    """board-read --since needs an int+unit duration; round up + 1h margin so
    the read window safely covers the full cooldown window (the largest of the
    severity thresholds)."""
    import math
    return "%dh" % (int(math.ceil(max_window_hours)) + 1)


def _read_recent_escalations(thresholds: dict, now: dt.datetime,
                             board_log_path: Path = None) -> dict:
    """Return {goal_id: most_recent_age_hours} for `inbox-alert-aged`
    coordination-board posts within the cooldown scan window - from ANY agent.

    THE SHARED, DURABLE COOLDOWN (g-115-1533, mirroring g-115-1531). The board
    breadcrumb `_post_board_cooldown` drops (tagged `inbox-alert-aged,<goal_id>,
    severity:<sev>`) IS the cooldown record: it is shared (every agent reads the
    same coordination board) and durable (board posts persist in world/board/,
    unlike the per-agent WM `proactive_escalation_log` slot each agent kept
    SEPARATELY and that WM resets wiped). Scanning the board before sending the
    user email therefore fixes BOTH original bugs at once - the N-agent
    duplicate (N agents each emailing the user about the same unclaimed alert)
    and the non-durable cooldown (a WM reset between iterations re-fired the same
    agent).

    Returns the SMALLEST age (most-recent post) per goal_id. The caller compares
    that against the CURRENT candidate's severity threshold (thresholds[sev]),
    preserving the prior severity-aware cooldown semantics - only the store
    moved from per-agent WM to the shared board.

    `board_log_path` (tests only): read a JSON list of post dicts directly,
    bypassing the daemon/subprocess board scan.

    FAIL-OPEN: any read failure yields an empty dict -> no cooldown -> eligible
    alerts re-escalate. Same direction as the prior empty-log fail-open.
    """
    # Scan back far enough to cover the longest cooldown window (the max threshold).
    max_window = max(thresholds.values()) if thresholds else 12.0
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
                         "--since", _escalate_window_str(max_window),
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
                    "inbox-alert-age-check: board-read.sh exit=%d stderr=%s - "
                    "fail-open (no cooldown this sweep)\n"
                    % (proc.returncode, (proc.stderr or "").strip()[:200]))
        except Exception as exc:
            sys.stderr.write(
                "inbox-alert-age-check: board-read.sh exception (%s) - fail-open\n" % exc)

    recent = {}
    for p in posts:
        if not isinstance(p, dict):
            continue
        tags = p.get("tags") or []
        if "inbox-alert-aged" not in tags:
            continue
        age = _age_hours(p.get("timestamp") or p.get("ts"), now)
        if age is None:
            continue
        for t in tags:
            # The breadcrumb tags the goal_id (`g-*`); severity/agent tags never do.
            if isinstance(t, str) and t.startswith("g-"):
                # Keep the MOST-RECENT (smallest age) post per goal_id.
                if t not in recent or age < recent[t]:
                    recent[t] = age
    return recent


def _classifier_subject(goal: dict) -> str:
    """Extract the classifier subject from the alert. Falls back to ''."""
    # The alert-sweep filer doesn't bake the classifier subject into a top-level
    # goal field - it lives in the description. The current alert-sweep.sh format
    # puts the subject after "Subject: " in the description. Best-effort regex
    # so a description-format drift degrades to empty rather than crashing.
    import re
    desc = goal.get("description", "") or ""
    m = re.search(r"^Subject:\s*(.+)$", desc, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return ""


def _send_email(goal: dict, severity: str, age_hours: float, no_email: bool) -> tuple:
    """Fire the notification via world/scripts/email-send.sh. Returns (ok, detail).

    When `no_email` is True (tests), skip the subprocess and return (True, "no_email").
    """
    title = goal.get("title", "")
    goal_id = goal.get("id", "")
    classifier = _classifier_subject(goal)
    age_h = age_hours if age_hours is not None else 0.0
    sev_label = severity.upper()
    subject = "Unclaimed alert >%dh: %s" % (int(age_h), title)
    body_lines = [
        "Alert-sweep filed an Unblock goal %.0f hours ago and no agent has claimed it yet."
        % age_h,
        "",
        "Goal: %s" % title,
        "Goal id: %s" % goal_id,
        "Severity: %s" % sev_label,
    ]
    if classifier:
        body_lines.append("Classifier subject: %s" % classifier)
    body_lines.append("")
    body_lines.append(
        "Action: claim the goal manually (one agent should run it), or "
        "investigate why no agent is picking up alert-sweep Unblocks. The "
        "goal will continue to age and re-notify per cooldown until claimed.")
    payload = {
        "InfoType": "Inbox Alert Age Escalation",
        "Title": subject,
        "InfoMessage": "\n".join(body_lines),
    }
    if no_email:
        return True, "no_email"
    try:
        world_dir = os.environ.get("WORLD_DIR")
        if not world_dir:
            # Resolve from local-paths.conf
            try:
                import _paths  # type: ignore
                world_dir = str(_paths.WORLD_DIR)
            except Exception:
                sys.stderr.write(
                    "inbox-alert-age-check: cannot resolve WORLD_DIR for email-send.sh - skipping notify\n")
                return False, "no_world_dir"
        email_script = Path(world_dir) / "scripts" / "email-send.sh"
        if not email_script.is_file():
            sys.stderr.write(
                "inbox-alert-age-check: email-send.sh not found at %s - skipping notify\n"
                % email_script)
            return False, "no_email_script"
        proc = subprocess.run(
            bash_cmd(email_script),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "sent"
        sys.stderr.write(
            "inbox-alert-age-check: email-send.sh exit=%d stderr=%s\n"
            % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return False, "email_send_nonzero:%d" % proc.returncode
    except Exception as exc:
        sys.stderr.write(
            "inbox-alert-age-check: email-send.sh exception (%s) - skipping notify\n" % exc)
        return False, "email_send_exception:%s" % exc.__class__.__name__


def _post_board_cooldown(goal_id: str, severity: str, no_board: bool) -> tuple:
    """Post the shared+durable cooldown breadcrumb to the coordination board.

    Returns (ok, detail). The post (tagged `inbox-alert-aged,<goal_id>,
    severity:<sev>`) IS the shared cooldown record the next sweep (any agent)
    reads via `_read_recent_escalations`, and a cross-agent visibility note that
    an alert is aging unclaimed. When `no_board` is True (tests), skip the
    subprocess and return (True, "no_board").
    """
    if no_board:
        return True, "no_board"
    try:
        tags = "inbox-alert-aged,%s,severity:%s" % (goal_id, severity)
        msg = ("Inbox alert aging unclaimed [%s] severity=%s -- claim the "
               "alert-sweep Unblock (one agent should run it)" % (goal_id, severity))
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
            "inbox-alert-age-check: board-post.sh exit=%d stderr=%s\n"
            % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return False, "board_post_nonzero:%d" % proc.returncode
    except Exception as exc:
        sys.stderr.write(
            "inbox-alert-age-check: board-post.sh exception (%s) - skipping post\n" % exc)
        return False, "board_post_exception:%s" % exc.__class__.__name__


def run(args) -> dict:
    """Main sweep. Returns the JSON-shape result dict (also printed to stdout)."""
    thresholds = _load_config(args)
    now = dt.datetime.now()
    asp = _read_aspiration(args.asp_id)
    goals = (asp.get("goals") or []) if isinstance(asp, dict) else []
    board_log_path = Path(args.board_escalation_log) if args.board_escalation_log else None
    recent_escalations = _read_recent_escalations(thresholds, now, board_log_path)

    candidates = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        if g.get("status") not in ("pending", "in-progress"):
            continue
        title = g.get("title", "") or ""
        if not title.startswith("Unblock"):
            continue
        sig = g.get("origin_signal", "") or ""
        if not sig.startswith("alert-email:"):
            continue
        age = _age_hours(g.get("created_at", ""), now)
        sev = _classify_severity(age, thresholds)
        if not sev:
            continue
        goal_id = g.get("id", "")
        # Severity-aware cooldown: a recent breadcrumb (from ANY agent) within
        # thresholds[sev] hours suppresses. Mirrors the prior _on_cooldown logic
        # (compare most-recent escalation age against the CURRENT severity's
        # threshold), but the store is now the shared board, not per-agent WM.
        recent_age = recent_escalations.get(goal_id)
        on_cooldown = recent_age is not None and recent_age < thresholds[sev]
        candidates.append({
            "goal_id": goal_id,
            "title": title,
            "age_hours": round(age, 2),
            "severity": sev,
            "blocker_id": "inbox_alert_%s" % goal_id,
            "origin_signal": sig,
            "on_cooldown": on_cooldown,
        })

    fired = []
    skipped_cooldown = []
    failed = []
    if args.apply:
        for c in candidates:
            if c["on_cooldown"]:
                skipped_cooldown.append(c["goal_id"])
                continue
            # Look up the full goal for the email payload.
            full = next((g for g in goals if g.get("id") == c["goal_id"]), None)
            if full is None:
                continue
            ok, detail = _send_email(full, c["severity"], c["age_hours"], args.no_email)
            # Post the shared+durable cooldown breadcrumb REGARDLESS of email
            # outcome - without it the next sweep tick (any agent) would retry
            # within the minute, spamming the email infra. The board post IS the
            # cooldown record the next sweep reads (fail-open: keep cooldown).
            _post_board_cooldown(c["goal_id"], c["severity"], args.no_board)
            if ok:
                fired.append({
                    "goal_id": c["goal_id"],
                    "severity": c["severity"],
                    "age_hours": c["age_hours"],
                    "detail": detail,
                })
            else:
                failed.append({
                    "goal_id": c["goal_id"],
                    "severity": c["severity"],
                    "detail": detail,
                })

    return {
        "mode": "apply" if args.apply else "dry_run",
        "asp_id": args.asp_id,
        "thresholds_hours": thresholds,
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
                   help="Actually send notifications and post board breadcrumbs (default: dry-run).")
    p.add_argument("--asp-id", default="asp-115",
                   help="Aspiration to scan (default: asp-115 - the alert-sweep target queue).")
    p.add_argument("--high-hours", type=float, default=None,
                   help="Override high-severity threshold (default: config or 4).")
    p.add_argument("--medium-hours", type=float, default=None,
                   help="Override medium-severity threshold (default: config or 12).")
    p.add_argument("--board-escalation-log", default=None,
                   help="Test-only: path to a JSON file of coordination-board posts standing in for the live board scan.")
    p.add_argument("--no-email", action="store_true",
                   help="Test-only: skip the email-send.sh subprocess and pretend it succeeded.")
    p.add_argument("--no-board", action="store_true",
                   help="Test-only: skip the board-post.sh cooldown breadcrumb and pretend it succeeded.")
    args = p.parse_args()
    result = run(args)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
