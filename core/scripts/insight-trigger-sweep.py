#!/usr/bin/env python3
"""core/scripts/insight-trigger-sweep.py — Convert findings-channel
insight_triggers into goals.

Background
----------
Agents post insight_triggers to world/board/findings.jsonl with tags like
`requires_action_by:alpha`, `action_type:extend-filter`, `severity:constrains`
when they notice cross-agent action items. The goal-selector reads goal
QUEUES, not BOARD posts, so these descriptive tags sit on the board with
no consumer. Canonical case: bravo's msg-20260514-143816-bravo-1073 posted
at 14:38 routed an action to alpha; 2h later no goal existed in any queue.

This sweeper closes the gap:
  1. Reads world/board/findings.jsonl for the last 24h.
  2. Filters to posts older than 1h (GRACE_HOURS) so authors who file
     their own goal-via-script first aren't pre-empted.
  3. Drops posts without `requires_action_by:<x>` AND `action_type:<y>` tags.
  4. Dedups against BOTH origin_signal formats in world + per-agent
     aspirations.jsonl: `insight_trigger:<msg_id>` (this sweeper) and
     `board_post:<msg_id>` (existing insight-trigger-gate.py). See
     core/config/conventions/board.md "Forward-Routing" for the asymmetry.
  5. Files an Apply goal in the world queue under asp-115 with
     `intended_agent=<x>` and `origin_signal=insight_trigger:<msg_id>`.
  6. Caps filings at MAX_GOALS_PER_RUN to bound any spam blast radius.

Modes
-----
  (default)            Read, dedup, file new goals.
  --dry-run            Same probe path; no aspirations writes.
  --json               Emit machine-readable output (used by /prime Step 5.5b).

Callers
-------
  - Recurring goal g-115-754 (every 1h, via insight-trigger-sweep.sh).
  - /prime Phase 2 Step 5.5b (with --dry-run --json) for visibility surface.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Reuse _paths.py resolution (WORLD_DIR honors local-paths.conf + env overrides).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
import _rt  # noqa: E402  — canonical Python -> daemon client (post-cutover; see _rt.py)
from _paths import WORLD_DIR, agents_root as _agents_root  # noqa: E402

FINDINGS = WORLD_DIR / "board" / "findings.jsonl"
WORLD_ASPS = WORLD_DIR / "aspirations.jsonl"

# Tunables — edit in place if the cadence/window/spam-cap needs adjustment.
# Single source of truth; no config-file indirection unless cross-agent
# variance becomes a real requirement.
GRACE_HOURS = 1.0
WINDOW_HOURS = 24.0
MAX_GOALS_PER_RUN = 10

SEVERITY_PRIORITY = {
    "invalidates": "HIGH",
    "constrains": "MEDIUM",
    "enables": "MEDIUM",
    "informs": "LOW",
}

REQ_ACTION_RE = re.compile(r"^requires_action_by:(.+)$")
ACTION_TYPE_RE = re.compile(r"^action_type:(.+)$")
SEVERITY_RE = re.compile(r"^severity:(.+)$")


# ---------------------------------------------------------------------------
# Loading + filtering
# ---------------------------------------------------------------------------


def _parse_ts(ts_str):
    """Parse the timestamp formats observed in findings.jsonl.

    Board entries write `2026-05-14T14:38:16` (local, no TZ suffix). We
    interpret as local naive — same convention as the rest of the framework
    (CLAUDE.md "ISO 8601 dates everywhere. Timestamps: ALWAYS local system
    time").
    """
    return datetime.fromisoformat(ts_str.rstrip("Z"))


def load_triggers():
    """Read findings.jsonl, yield candidate insight_triggers.

    A candidate satisfies all of:
      - message has `requires_action_by:<agent>` tag
      - message has `action_type:<verb>` tag
      - message timestamp is within WINDOW_HOURS and older than GRACE_HOURS
    """
    if not FINDINGS.is_file():
        return []
    now = datetime.now()
    win_cutoff = now - timedelta(hours=WINDOW_HOURS)
    grace_cutoff = now - timedelta(hours=GRACE_HOURS)
    out = []
    for line in FINDINGS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = _parse_ts(msg["timestamp"])
        except (KeyError, ValueError):
            continue
        if ts < win_cutoff or ts > grace_cutoff:
            continue
        tags = msg.get("tags") or []
        target = None
        action = None
        severity = "informs"
        for t in tags:
            m = REQ_ACTION_RE.match(t)
            if m:
                target = m.group(1).strip()
                continue
            m = ACTION_TYPE_RE.match(t)
            if m:
                action = m.group(1).strip()
                continue
            m = SEVERITY_RE.match(t)
            if m:
                severity = m.group(1).strip()
        if not target or not action:
            continue
        out.append({
            "msg_id": msg.get("id"),
            "author": msg.get("author"),
            "target": target,
            "action": action,
            "severity": severity,
            "text": msg.get("text", ""),
            "tags": tags,
            "timestamp": msg.get("timestamp"),
            "age_h": round((now - ts).total_seconds() / 3600, 1),
        })
    return out


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


_CONVERTED_ID_RE = re.compile(r'"(?:insight_trigger|board_post):([^"]+)"')


def load_converted_ids():
    """Single-pass scan of world + per-agent aspirations.jsonl. Returns the
    set of msg_ids already converted via EITHER origin_signal format:
      - insight_trigger:<msg_id> — this sweeper's convention
      - board_post:<msg_id> — existing insight-trigger-gate.py convention
    Dual recognition keeps the two routing mechanisms from filing duplicates
    if/when insight-trigger-gate.py gets fully wired into precheck.

    Returns a set, called ONCE per sweep run. Per-trigger dedup is then an
    O(1) set membership check in main() — single source of truth for the
    "what's already converted" view."""
    paths = [WORLD_ASPS]
    for d in sorted(_agents_root().iterdir()):
        if d.is_dir() and (d / "local-paths.conf").is_file():
            paths.append(d / "aspirations.jsonl")
    ids = set()
    for path in paths:
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                for m in _CONVERTED_ID_RE.finditer(line):
                    ids.add(m.group(1))
    return ids


# ---------------------------------------------------------------------------
# Goal filing
# ---------------------------------------------------------------------------


def _build_goal_payload(trigger):
    """Build the JSON payload passed via stdin to aspirations.py add-goal."""
    priority = SEVERITY_PRIORITY.get(trigger["severity"], "MEDIUM")
    title = f"Apply: {trigger['action']} (from {trigger['author']} insight_trigger {trigger['msg_id']})"
    desc_parts = [
        f"Insight trigger from {trigger['author']} @ {trigger['timestamp']}.",
        "",
        trigger["text"],
        "",
        f"Tags: {', '.join(trigger['tags'])}",
        f"Board pointer: world/board/findings.jsonl ({trigger['msg_id']})",
        "",
        "Filed automatically by core/scripts/insight-trigger-sweep.py — closes",
        "the routing gap where findings-channel action items did not reach",
        "the goal queue (canonical incident: msg-20260514-143816-bravo-1073).",
    ]
    return {
        "title": title,
        "description": "\n".join(desc_parts),
        "priority": priority,
        "participants": ["agent"],
        "intended_agent": trigger["target"],
        "origin_signal": f"insight_trigger:{trigger['msg_id']}",
        "tags": ["insight-trigger-conversion", f"from:{trigger['author']}", f"to:{trigger['target']}"],
    }


def file_goal(trigger, *, dry_run=False):
    payload = _build_goal_payload(trigger)
    if dry_run:
        return {"would_file": True, "payload": payload}
    # --override-all: bypass every gate with one audited justification. This
    # conversion path has:
    #   - already dedup'd by msg_id substring (covers duplication-gate concern)
    #   - source board post IS the investigation artifact (covers
    #     scaffolded-exploration-gate concern)
    #   - origin_signal is correctly formed (covers origin-signal-gate)
    #   - no stale-read concern — we read findings.jsonl fresh each run
    # Override is logged to world/override-bypass-ledger.jsonl per the
    # override-bypass-ledger convention (blast radius bounded — one goal per
    # msg, msg dedup'd).
    #
    # Route via _rt.aspirations_add_goal — the canonical Python -> daemon
    # client identified by  (zeta investigation, 2026-05-17) and
    # adopted by the sibling insight-trigger-gate.py:386. The prior
    # subprocess.run(['bash', 'aspirations-add-goal.sh', ...]) shape resolved
    # 'bash' to the WSL stub on Windows (rc=127 / WinError 193) and inherited
    # CRLF under autocrlf=true. _rt skips bash entirely: pure urllib POST to
    # the local daemon. `overrides={"All": justification}` maps to the
    # X-Mind-Override-All header that --override-all used to set
    # (see _rt.py:117-128).
    justification = (
        f"insight-trigger conversion (sweep): {trigger['msg_id']} "
        f"from {trigger['author']} (msg-id dedup'd; source post is "
        "the investigation artifact)"
    )
    try:
        resp = _rt.aspirations_add_goal(
            "asp-115", payload, source="world",
            overrides={"All": justification},
        )
        return {
            "would_file": False,
            "rc": 0,
            "stdout": json.dumps(resp)[:500],
            "stderr": "",
        }
    except _rt.RtError as e:
        err_text = (e.body or str(e)).strip()
        return {
            "would_file": False,
            "rc": getattr(e, "status", None) or 1,
            "stdout": "",
            "stderr": err_text[:500],
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Convert findings-channel insight_triggers to goals.")
    ap.add_argument("--dry-run", action="store_true", help="Probe-only mode; no writes.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    args = ap.parse_args()

    dry_run = args.dry_run

    triggers = load_triggers()
    converted_ids = load_converted_ids()
    pending = []
    skipped = []
    for t in triggers:
        if t["msg_id"] in converted_ids:
            skipped.append({"msg_id": t["msg_id"], "reason": "already_converted"})
            continue
        pending.append(t)

    filed = []
    overflow = []
    for t in pending:
        if len(filed) >= MAX_GOALS_PER_RUN:
            overflow.append(t)
            continue
        result = file_goal(t, dry_run=dry_run)
        filed.append({"trigger": t, "result": result})

    if dry_run:
        filed_count = len(filed)
    else:
        filed_count = sum(1 for f in filed if f["result"].get("rc") == 0)

    summary = {
        "mode": "dry-run" if dry_run else "sweep",
        "window_hours": WINDOW_HOURS,
        "grace_hours": GRACE_HOURS,
        "scanned": len(triggers),
        "skipped_already_converted": len(skipped),
        "filed": filed_count,
        "attempted": len(filed),
        "overflow": len(overflow),
        "pending": [
            {
                "msg_id": t["msg_id"],
                "author": t["author"],
                "target": t["target"],
                "action": t["action"],
                "severity": t["severity"],
                "age_h": t["age_h"],
            }
            for t in pending
        ],
        "filed_details": filed,
        "overflow_details": [{"msg_id": t["msg_id"], "target": t["target"]} for t in overflow],
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        # Human-readable
        print(f"[insight-trigger-sweep] mode={summary['mode']} scanned={summary['scanned']} "
              f"skipped={summary['skipped_already_converted']} filed={summary['filed']} "
              f"overflow={summary['overflow']}")
        for f in filed:
            t = f["trigger"]
            r = f["result"]
            status = "DRY-RUN-WOULD-FILE" if dry_run else ("OK" if r.get("rc") == 0 else f"FAIL rc={r.get('rc')}")
            print(f"  {status}: {t['msg_id']} {t['author']}->{t['target']} action={t['action']} severity={t['severity']}")
            if not dry_run and r.get("rc") != 0:
                print(f"    stderr: {r.get('stderr')[:200]}")
        if overflow:
            print(f"  WARN: {len(overflow)} additional triggers exceeded MAX_GOALS_PER_RUN={MAX_GOALS_PER_RUN}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
