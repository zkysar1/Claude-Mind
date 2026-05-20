#!/usr/bin/env python3
"""Layer-C detective: scan Claude Code transcripts for the
"/aspirations rejected" pattern and the ScheduleWakeup misuse that caused it.

Companion to `core/scripts/schedule-wakeup-gate.py` (Layer A) and
`.claude/rules/schedule-wakeup-correctness.md` (Layer B). The gate is the
primary defense; this audit catches drift if the gate is bypassed
(timeout, fail-open path, hook misconfiguration).

The bad-slash predicate is imported from `_swakeup_predicate.py` — single
source of truth shared with the gate. Do NOT inline the check.

Run modes:
  --since-hours <N>      # only count events from the last N hours (default 24)
  --json                 # machine-readable JSON output
  --transcripts-dir <p>  # override default Claude Code projects dir
  --exit-on-hits         # exit 1 if any hits in window (cron regression shape)

Wrapper goal usage (the "files an Investigate goal" part of Layer C lives
HERE, not in this script):
    if ! py -3 core/scripts/aspirations-rejection-audit.py --exit-on-hits; then
        bash core/scripts/aspirations-add-goal.sh ... --description "..."
    fi

What it counts (per agent transcript):
  - bad_schedule_wakeups: ScheduleWakeup tool_use events whose `prompt`
    is flagged by `is_bad_slash_prefix` (same predicate as the gate).
  - rejection_messages: any user-content string containing the canonical
    rejection text "can only be invoked by Claude".

SID -> agent mapping uses the project root's `.active-agent-<SID>` files.

Cross-platform note: the default transcripts dir is computed from project
root by replacing `:` and `/` with `-`. Verified on Windows (Git Bash). On
POSIX the convention may differ — pass `--transcripts-dir` explicitly if
the default doesn't resolve.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _swakeup_predicate import is_bad_slash_prefix  # noqa: E402

REJECTION_NEEDLE = "can only be invoked by Claude"


def _default_transcripts_dir(project_root: Path) -> Path:
    """Claude Code transcripts dir for a given project root.

    Convention: ~/.claude/projects/<dashified-path>, where the dashified path
    replaces both `:` and `/` (and `\\`) with `-`. Example on Windows:
        C:/path/to/my-repo -> C--path-to-my-repo
    """
    s = str(project_root.resolve()).replace("\\", "/")
    dashified = s.replace(":", "-").replace("/", "-")
    return Path(os.path.expanduser("~/.claude/projects")) / dashified


def _parse_args():
    default_root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since-hours", type=int, default=24)
    p.add_argument("--json", action="store_true")
    p.add_argument("--project-root", type=Path, default=default_root)
    p.add_argument("--transcripts-dir", type=Path, default=None)
    p.add_argument("--exit-on-hits", action="store_true")
    args = p.parse_args()
    if args.transcripts_dir is None:
        args.transcripts_dir = _default_transcripts_dir(args.project_root)
    return args


def _load_agent_map(project_root: Path) -> dict:
    """Map SID -> agent name from .active-agent-* files at project root."""
    out = {}
    try:
        for f in project_root.glob(".active-agent-*"):
            sid = f.name.replace(".active-agent-", "")
            try:
                out[sid] = f.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    except OSError:
        pass
    return out


def _parse_ts(s):
    """Parse ISO8601 timestamp string -> UTC datetime. None on failure."""
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _scan_transcript(path: Path, cutoff: datetime) -> dict:
    """Scan a single transcript JSONL file. Returns counts + samples."""
    bad_sw = []
    rejections = []
    try:
        with open(path, "rb") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                ts = _parse_ts(e.get("timestamp", ""))
                if ts is None or ts < cutoff:
                    continue
                msg = e.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if (item.get("type") == "tool_use"
                                and item.get("name") == "ScheduleWakeup"):
                            prompt = (item.get("input") or {}).get("prompt")
                            if is_bad_slash_prefix(prompt):
                                bad_sw.append({
                                    "timestamp": e.get("timestamp", ""),
                                    "prompt": str(prompt)[:120],
                                })
                if isinstance(content, str) and REJECTION_NEEDLE in content:
                    rejections.append(e.get("timestamp", ""))
    except OSError:
        pass
    return {"bad_schedule_wakeups": bad_sw, "rejection_messages": rejections}


def _build_report(transcripts_dir: Path, project_root: Path,
                  since_hours: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    sid_to_agent = _load_agent_map(project_root)
    per_agent = {}
    try:
        files = sorted(transcripts_dir.glob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        sid = path.stem
        result = _scan_transcript(path, cutoff)
        if not (result["bad_schedule_wakeups"] or result["rejection_messages"]):
            continue
        agent = sid_to_agent.get(sid, "(unknown)")
        bucket = per_agent.setdefault(agent, {
            "sids": [],
            "bad_schedule_wakeups": [],
            "rejection_messages": [],
        })
        bucket["sids"].append(sid)
        bucket["bad_schedule_wakeups"].extend(result["bad_schedule_wakeups"])
        bucket["rejection_messages"].extend(result["rejection_messages"])
    totals = {
        "agents_with_hits": len(per_agent),
        "total_bad_schedule_wakeups": sum(
            len(v["bad_schedule_wakeups"]) for v in per_agent.values()),
        "total_rejections": sum(
            len(v["rejection_messages"]) for v in per_agent.values()),
        "since_hours": since_hours,
        "cutoff_utc": cutoff.isoformat(),
        "transcripts_dir": str(transcripts_dir),
        "transcripts_dir_exists": transcripts_dir.exists(),
    }
    return {"totals": totals, "per_agent": per_agent}


def _print_human(report: dict) -> None:
    t = report["totals"]
    print(
        f"aspirations-rejection-audit window={t['since_hours']}h "
        f"agents_with_hits={t['agents_with_hits']} "
        f"bad_schedule_wakeups={t['total_bad_schedule_wakeups']} "
        f"rejections={t['total_rejections']}"
    )
    if not t["transcripts_dir_exists"]:
        print(f"(transcripts dir missing: {t['transcripts_dir']})")
        return
    if not report["per_agent"]:
        print("(clean - no hits in window)")
        return
    for agent, data in sorted(report["per_agent"].items()):
        print(f"\n=== {agent} ===")
        print(f"  sids: {', '.join(s[:8] for s in data['sids'])}")
        print(f"  bad_schedule_wakeups: {len(data['bad_schedule_wakeups'])}")
        for sw in data["bad_schedule_wakeups"][:5]:
            print(f"    {sw['timestamp'][:19]} | prompt={sw['prompt']!r}")
        if len(data["bad_schedule_wakeups"]) > 5:
            print(f"    ... ({len(data['bad_schedule_wakeups']) - 5} more)")
        print(f"  rejection_messages: {len(data['rejection_messages'])}")
        for r in data["rejection_messages"][:5]:
            print(f"    {r[:19]}")
        if len(data["rejection_messages"]) > 5:
            print(f"    ... ({len(data['rejection_messages']) - 5} more)")


def main():
    args = _parse_args()
    report = _build_report(args.transcripts_dir, args.project_root,
                           args.since_hours)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    if args.exit_on_hits:
        any_hits = (report["totals"]["total_bad_schedule_wakeups"]
                    + report["totals"]["total_rejections"]) > 0
        sys.exit(1 if any_hits else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
