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
HERE, not in this script). Goal fields go in the JSON body via stdin, not
as CLI flags — `--title`/`--description`/etc. are rejected with exit 2 (see
`aspirations-add-goal.sh --help`):
    if ! py -3 core/scripts/aspirations-rejection-audit.py --exit-on-hits; then
        printf '%s' '{"title":"Investigate: ...","priority":"MEDIUM",
            "participants":["agent"],"description":"..."}' \
            | bash core/scripts/aspirations-add-goal.sh asp-115
    fi

What it counts (per agent transcript):
  - bad_schedule_wakeups: ScheduleWakeup tool_use events whose `prompt`
    is flagged by `is_bad_slash_prefix` (same predicate as the gate).
  - rejection_messages: any user-content string containing the canonical
    rejection text "can only be invoked by Claude".
  - deadman_arms: count of ScheduleWakeup tool_use events whose `prompt` IS
    the `<<autonomous-loop-dynamic>>` sentinel — the GOOD self-resurrection arm
    (opposite of is_bad_slash_prefix).
  - resurrection_gaps / resurrection_risk (g-115-2771 / rb-4345): the
    silent-death signature, detected by arm CADENCE. On a healthy loop the
    deadman net re-arms at least every 600s (the wakeup re-fires at its own
    delay whenever the session idles, and each iteration re-arms at close). A
    gap between consecutive deadman arms exceeding _SILENT_GAP_SEC (over an hour
    — longer than any single legitimate iteration, including a 32-min test
    suite) means the net went un-re-armed far too long. `resurrection_risk` is
    raised only for a HIGH-CONFIDENCE gap — one that ALSO contains a structured
    API-error event (`isApiErrorMessage`, the flag Claude Code sets on genuine
    transport errors) — the exact 2026-07-19 cc-04 shape (last arm 23:38,
    storm-killed resurrection attempts through 00:04, then silence, next arm
    07:32 after zombie-recovery, 3 API errors in the gap). A structured error in
    the gap discriminates a real storm-death from a legitimate /stop idle period
    (no errors); NOTE a storm-death is high-CHURN (~577 lines over the 7.9h
    incident gap from failed retries), so event density is NOT a discriminator.
    Because detection keys on arm CADENCE + a STRUCTURED error flag (never
    error-string matching), a transcript that merely DISCUSSES API errors does
    not trip it. Complements trailing-text-detector.py (non-storm text-deaths);
    the durable fix is the re-arm-first rule in return-protocol.md /
    schedule-wakeup-correctness.md.
  - health-ledger cross-reference (g-115-2782): a high-confidence gap is
    DOWNGRADED (high_confidence -> False, dropping resurrection_risk) when the
    agent's health-ledger — agents/<agent>/health/<date>.jsonl, an INDEPENDENT
    per-iteration liveness signal appended by iteration-close.sh — has entries
    INSIDE the gap window. Iteration-close entries across the "silent" window
    prove the loop was alive and iterating, so the arm-cadence gap was a
    transcript-completeness artifact, not a real death (a third independent
    signal atop arm-cadence + structured API error). The real 2026-07-19 storm
    death has ZERO ledger entries in its gap (dead loop -> no iteration-close),
    so it is preserved; the downgrade count is `total_health_ledger_filtered`.

SID -> agent mapping uses the project root's `.active-agent-<SID>` files.

Cross-platform note: the default transcripts dir is computed from project
root by replacing `:` and `/` with `-`. Verified on Windows (Git Bash). On
POSIX the convention may differ — pass `--transcripts-dir` explicitly if
the default doesn't resolve.
"""

import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _swakeup_predicate import is_bad_slash_prefix  # noqa: E402

REJECTION_NEEDLE = "can only be invoked by Claude"

# Deadman-net health ( / rb-4345). The GOOD self-resurrection arm is a
# ScheduleWakeup whose prompt IS this sentinel. The silent-death signature is
# detected by arm CADENCE: a long gap between consecutive deadman arms with the
# session idle across it (net fired, resurrected turn never re-armed),
# corroborated by a structured API-error event inside the gap. Thresholds:
DEADMAN_SENTINEL = "<<autonomous-loop-dynamic>>"
# A death is multi-HOUR (bounded by resurrection/recovery latency — the
# 2026-07-19 incident sat dead ~7.9h); a legitimate single iteration re-arms at
# its close, well under an hour even for a 32-min test suite. So a > 1h gap
# between consecutive deadman arms is the outer signature. A storm-death is NOT
# idle: failed API retries + hook fires churn out HUNDREDS of transcript lines
# during the dead window (measured 577 lines / 73 per hour across the 7.9h
# incident gap), so event density does NOT discriminate death from busy work —
# density was tried and WRONGLY excluded the real incident. The clean
# discriminator is a STRUCTURED API-error event inside the gap (high_confidence),
# which separates a storm-death from a legitimate /stop idle period (no errors).
_SILENT_GAP_SEC = 3600           # 1h — above any single legit iteration


def _find_silent_death_gaps(arm_ts, event_ts, api_err_ts):
    """Consecutive deadman-arm gaps longer than _SILENT_GAP_SEC — candidate
    silent deaths (the net went un-re-armed for over an hour). A gap that ALSO
    contains a structured API-error event is flagged high_confidence: the
    storm-death shape (net fired, resurrected turn re-killed by the storm and
    never re-armed), distinct from a legitimate /stop idle period (no errors).
    events_in_gap is retained for CONTEXT only — it does NOT gate, because a
    storm-death is high-churn, not idle. All three args are sorted-ascending
    datetime lists; counting via bisect keeps it O(arms * log events).

    Known residual: a genuinely > 1h iteration that hit a transient API error
    but SURVIVED (re-armed at its eventual close) reads as a candidate. That
    intersection is rare, and the Layer-C wrapper files an Investigate goal for
    a human/agent to confirm against agent-state / recovery-notice history — the
    detective surfaces candidates, it does not adjudicate."""
    gaps = []
    for a, b in zip(arm_ts, arm_ts[1:]):
        gap_sec = (b - a).total_seconds()
        if gap_sec <= _SILENT_GAP_SEC:
            continue
        inside = bisect.bisect_left(event_ts, b) - bisect.bisect_right(event_ts, a)
        errs = bisect.bisect_left(api_err_ts, b) - bisect.bisect_right(api_err_ts, a)
        gaps.append({
            "after_arm": a.isoformat(),
            "next_arm": b.isoformat(),
            "gap_hours": round(gap_sec / 3600.0, 2),
            "events_in_gap": inside,
            "api_errors_in_gap": errs,
            "high_confidence": errs > 0,
        })
    return gaps


def _health_ledger_ts(project_root, agent, cutoff):
    """Sorted UTC health-ledger entry timestamps for `agent`, at/after `cutoff`.

    The health-ledger (agents/<agent>/health/<date>.jsonl) is appended ONCE PER
    ITERATION by iteration-close.sh — a liveness signal from an INDEPENDENT code
    path than the deadman arm. Entries INSIDE a flagged resurrection gap prove the
    loop was iterating (alive) across the "silent" window, so the arm-cadence gap
    is a transcript-completeness artifact (arms in another transcript / a
    deadman-disabled agent that still iterates), NOT a real silent death — the
    g-115-2782 cross-reference filter consumes this. The real 2026-07-19 storm
    death has ZERO ledger entries in its gap (loop dead -> no iteration-close ->
    no append), so the filter preserves it (verified on-box).

    The naive `ts` is UTC wall time (CLAUDE.md TZ convention — a naive value is
    treated as UTC directly, never via astimezone-local, so the comparison is
    correct on any box TZ). Returns [] for an unknown/absent agent (conservative:
    no filtering, the risk flag stands)."""
    if not agent or agent == "(unknown)":
        return []
    health_dir = project_root / "agents" / agent / "health"
    if not health_dir.is_dir():
        return []
    out = []
    try:
        files = sorted(health_dir.glob("*.jsonl"))
    except OSError:
        return []
    for hp in files:
        try:
            with open(hp, "rb") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    s = e.get("ts", "")
                    if not isinstance(s, str) or not s:
                        continue
                    try:
                        dt = datetime.fromisoformat(
                            s[:-1] + "+00:00" if s.endswith("Z") else s)
                    except (ValueError, TypeError):
                        continue
                    dt = (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None
                          else dt.astimezone(timezone.utc))
                    if dt >= cutoff:
                        out.append(dt)
        except OSError:
            continue
    out.sort()
    return out


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
    """Map SID -> agent name from session bindings.

    Phase 2.6: prefers agents/<name>/sessions/<SID>/binding.yaml (the SID is
    the directory name; the agent is the parent dir's parent). Falls back to
    the legacy .active-agent-<SID> file at project root for migration-era
    sessions. Without Phase 2.6, new-session transcript hits are attributed
    to "(unknown)".
    """
    out = {}
    agents_parent = project_root / "agents"
    if agents_parent.is_dir():
        try:
            for agent_dir in agents_parent.iterdir():
                if not agent_dir.is_dir():
                    continue
                sessions_dir = agent_dir / "sessions"
                if not sessions_dir.is_dir():
                    continue
                try:
                    for session_dir in sessions_dir.iterdir():
                        if not session_dir.is_dir():
                            continue
                        if (session_dir / "binding.yaml").is_file():
                            out[session_dir.name] = agent_dir.name
                except OSError:
                    pass
        except OSError:
            pass
    # Legacy fallback (do not overwrite Phase 2.6 entries):
    try:
        for f in project_root.glob(".active-agent-*"):
            sid = f.name.replace(".active-agent-", "")
            if sid in out:
                continue
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
    arm_ts = []        # deadman-sentinel arm timestamps (datetime)
    api_err_ts = []    # structured API-error event timestamps (datetime)
    api_errors = []    # {timestamp, status} for reporting
    event_ts = []      # every in-window line timestamp (idle-density base)
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
                event_ts.append(ts)
                # Structured API-error flag (). Claude Code sets
                # isApiErrorMessage on genuine transport errors — NOT on content
                # that merely mentions them — so a transcript discussing errors
                # is not a false positive.
                if e.get("isApiErrorMessage") is True:
                    api_err_ts.append(ts)
                    api_errors.append({
                        "timestamp": e.get("timestamp", ""),
                        "status": e.get("apiErrorStatus", ""),
                    })
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
                            elif (isinstance(prompt, str)
                                  and DEADMAN_SENTINEL in prompt):
                                arm_ts.append(ts)
                if isinstance(content, str) and REJECTION_NEEDLE in content:
                    rejections.append(e.get("timestamp", ""))
    except OSError:
        pass
    gaps = _find_silent_death_gaps(sorted(arm_ts), sorted(event_ts),
                                   sorted(api_err_ts))
    return {
        "bad_schedule_wakeups": bad_sw,
        "rejection_messages": rejections,
        "deadman_arms": len(arm_ts),
        "api_errors": api_errors,
        "resurrection_gaps": gaps,
        "resurrection_risk": any(g["high_confidence"] for g in gaps),
    }


def _build_report(transcripts_dir: Path, project_root: Path,
                  since_hours: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    sid_to_agent = _load_agent_map(project_root)
    per_agent = {}
    health_filtered = 0        # : high-conf gaps downgraded by ledger
    try:
        files = sorted(transcripts_dir.glob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        sid = path.stem
        result = _scan_transcript(path, cutoff)
        agent = sid_to_agent.get(sid, "(unknown)")
        # Health-ledger cross-reference filter (). A high-confidence
        # gap is downgraded when the agent's health-ledger — an INDEPENDENT
        # per-iteration liveness signal (appended by iteration-close.sh, a
        # different code path than the deadman arm) — has entries INSIDE the gap
        # window. That proves the loop was iterating across the "silent" window,
        # so the arm-cadence gap is a transcript-completeness artifact, not a
        # real death. The real 2026-07-19 storm death has ZERO ledger entries in
        # its gap (dead loop -> no iteration-close), so it is preserved. Runs
        # only when a high-confidence gap exists (the ledger read is skipped on
        # the clean common path).
        if any(g["high_confidence"] for g in result["resurrection_gaps"]):
            health_ts = _health_ledger_ts(project_root, agent, cutoff)
            for g in result["resurrection_gaps"]:
                if not g["high_confidence"]:
                    continue
                a = _parse_ts(g["after_arm"])
                b = _parse_ts(g["next_arm"])
                live = 0
                if a is not None and b is not None:
                    live = (bisect.bisect_left(health_ts, b)
                            - bisect.bisect_right(health_ts, a))
                g["health_ledger_entries_in_gap"] = live
                g["health_ledger_liveness_in_gap"] = live > 0
                if live > 0:
                    g["high_confidence"] = False
                    g["filtered_reason"] = (
                        f"health-ledger liveness: agent iterated {live}x inside "
                        "the gap (alive, not a silent death) [g-115-2782]")
                    health_filtered += 1
            # Recompute the transcript's risk after the ledger filter.
            result["resurrection_risk"] = any(
                g["high_confidence"] for g in result["resurrection_gaps"])
        if not (result["bad_schedule_wakeups"] or result["rejection_messages"]
                or result["resurrection_risk"]):
            continue
        bucket = per_agent.setdefault(agent, {
            "sids": [],
            "bad_schedule_wakeups": [],
            "rejection_messages": [],
            "deadman_arms": 0,
            "api_errors": [],
            "resurrection_gaps": [],
            "resurrection_risk_sids": [],
        })
        bucket["sids"].append(sid)
        bucket["bad_schedule_wakeups"].extend(result["bad_schedule_wakeups"])
        bucket["rejection_messages"].extend(result["rejection_messages"])
        bucket["deadman_arms"] += result["deadman_arms"]
        bucket["api_errors"].extend(result["api_errors"])
        bucket["resurrection_gaps"].extend(result["resurrection_gaps"])
        if result["resurrection_risk"]:
            bucket["resurrection_risk_sids"].append(sid)
    totals = {
        "agents_with_hits": len(per_agent),
        "total_bad_schedule_wakeups": sum(
            len(v["bad_schedule_wakeups"]) for v in per_agent.values()),
        "total_rejections": sum(
            len(v["rejection_messages"]) for v in per_agent.values()),
        "total_resurrection_gaps": sum(
            len(v["resurrection_gaps"]) for v in per_agent.values()),
        "agents_with_resurrection_risk": sum(
            1 for v in per_agent.values() if v["resurrection_risk_sids"]),
        "total_resurrection_risk_sids": sum(
            len(v["resurrection_risk_sids"]) for v in per_agent.values()),
        "total_health_ledger_filtered": health_filtered,
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
        f"rejections={t['total_rejections']} "
        f"resurrection_risk={t['total_resurrection_risk_sids']} "
        f"health_ledger_filtered={t.get('total_health_ledger_filtered', 0)}"
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
        if data["resurrection_risk_sids"]:
            hc = [g for g in data["resurrection_gaps"] if g.get("high_confidence")]
            print(f"  ⚠ resurrection_risk: {len(data['resurrection_risk_sids'])} "
                  f"transcript(s) — long deadman-arm gap with API error(s) inside "
                  f"(silent-death shape) [g-115-2771 / rb-4345]")
            for g in hc[:3]:
                print(f"    {g['after_arm'][:19]} -> {g['next_arm'][:19]} | "
                      f"gap={g['gap_hours']}h events={g['events_in_gap']} "
                      f"api_errors={g['api_errors_in_gap']}")


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
                    + report["totals"]["total_rejections"]
                    + report["totals"]["total_resurrection_risk_sids"]) > 0
        sys.exit(1 if any_hits else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
