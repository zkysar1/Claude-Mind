#!/usr/bin/env python3
"""Stage-3 measurement: audit deadman-switch ARM COMPLIANCE per agent.

Companion to the arm-fix in `core/scripts/iteration-close.sh` /
`core/scripts/recurring-close.sh` (deadman-ON branch) and
`core/config/rationale/deadman-switch.md`. Scans each agent's Claude Code
transcript and classifies every deadman arm by RESPONSE FORM:

  batched  : the arm's own response (same message.id) ALSO contains
             Skill(aspirations) — the ideal atomic arm + immediate re-entry.
  followed : the arm's response has no Skill, but a LATER response emits
             Skill(aspirations) within FOLLOW_WINDOW_S — the SPLIT form
             (ScheduleWakeup ends the turn, its tool_result feeds back, a new
             response emits the Skill). SAFE: the 600s net is armed AND the
             loop re-enters; NEITHER response is a text-death.
  orphan   : NO Skill(aspirations) within FOLLOW_WINDOW_S after the arm — the
             only concerning form (the net is still armed, so a real death
             would still resurrect, but the loop did not visibly re-enter
             in-window — worth investigating).

Safety note: the deadman's protection is the ARMED 600s wakeup, present in all
three forms. `batched` vs `followed` is a QUALITY distinction (atomic vs split),
not a safety one. Only `orphan` and `NOT-ARMING` (flagged agent, no arm in
window) are flagged as non-compliant by --exit-on-noncompliance.

Why this exists: the rollout must MEASURE arm behavior, not ship-and-assume. A
single good sample (one batched pair) is NOT evidence the arm fires correctly
in aggregate — only a transcript-wide tally is. This is the automated version
of that tally, per the Stage-3 plan.

Transcript-resolution helpers (_default_transcripts_dir, _load_agent_map,
_parse_ts) are imported from `aspirations-rejection-audit.py` — single source
of truth shared with the sibling Layer-C detective. Do NOT re-inline them.

Run modes:
  --since-hours <N>           # window (default 24)
  --json                      # machine-readable JSON
  --transcripts-dir <p>       # override default Claude Code projects dir
  --exit-on-noncompliance     # exit 1 if any FLAGGED agent is ORPHANS or
                              #   NOT-ARMING in window (recurring/cron shape)
"""

import argparse
import importlib.util as _ilu
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Single source of truth: reuse the sibling detective's transcript helpers.
# importlib-load (the filename is hyphenated, so a plain import is impossible).
# The sibling has no import-time side effects beyond defs + a safe predicate
# import, and its main() is __name__-guarded, so exec_module is side-effect-free.
_src = SCRIPT_DIR / "aspirations-rejection-audit.py"
_spec = _ilu.spec_from_file_location("_arja_helpers", _src)
_arja = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_arja)
_default_transcripts_dir = _arja._default_transcripts_dir
_load_agent_map = _arja._load_agent_map
_parse_ts = _arja._parse_ts

SENTINEL = "<<autonomous-loop-dynamic>>"
DEADMAN_DELAY = 600
# A Skill(aspirations) in a LATER response this many seconds after the arm
# still counts as the loop re-entering (the split form). The threshold sits in
# the no-man's-land between two regimes: observed prompt re-entry gaps (split
# form: 11-188s in charlie's 24h sample) and the 600s deadman resurrection
# latency. 300s is comfortably above the largest observed re-entry gap (188s)
# and comfortably below 600s — so a HEALTHY split re-entry is classified
# `followed`, while a genuine text-death (whose only following Skill is the
# +600s resurrection, or none at all) still classifies `orphan`. Do NOT lower
# toward the observed-gap ceiling: a single outlier (the 188s arm) was
# false-flagged as orphan at 180s though the loop had plainly re-entered.
FOLLOW_WINDOW_S = 300


def _parse_args():
    default_root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since-hours", type=int, default=24)
    p.add_argument("--json", action="store_true")
    p.add_argument("--project-root", type=Path, default=default_root)
    p.add_argument("--transcripts-dir", type=Path, default=None)
    p.add_argument("--exit-on-noncompliance", action="store_true")
    args = p.parse_args()
    if args.transcripts_dir is None:
        args.transcripts_dir = _default_transcripts_dir(args.project_root)
    return args


def _flagged_agents(project_root: Path) -> dict:
    """agent name -> bool (deadman ACTIVE for this agent).

    Default-ON since Stage 5 (2026-06-23): every agent is deadman-active
    UNLESS it carries an opt-out `deadman-disabled` flag in its session dir.
    (Pre-Stage-5 this checked the inverse opt-IN `deadman-enabled` flag.)
    """
    out = {}
    agents_parent = project_root / "agents"
    if not agents_parent.is_dir():
        return out
    try:
        for agent_dir in sorted(agents_parent.iterdir()):
            if not agent_dir.is_dir():
                continue
            session_dir = agent_dir / "session"
            if not session_dir.is_dir():
                continue
            out[agent_dir.name] = not (session_dir / "deadman-disabled").is_file()
    except OSError:
        pass
    return out


def _session_to_agent(project_root: Path) -> dict:
    """SID -> agent, combining the sibling's binding-based map with the
    per-agent running/latest-session-id pointers (which reliably attribute the
    CURRENTLY-active transcript even when a binding.yaml is absent)."""
    sid_map = dict(_load_agent_map(project_root))
    agents_parent = project_root / "agents"
    if agents_parent.is_dir():
        try:
            for agent_dir in agents_parent.iterdir():
                if not agent_dir.is_dir():
                    continue
                sdir = agent_dir / "session"
                for ptr in ("running-session-id", "latest-session-id"):
                    f = sdir / ptr
                    if f.is_file():
                        try:
                            sid = f.read_text(encoding="utf-8").strip()
                        except OSError:
                            continue
                        if sid and sid not in sid_map:
                            sid_map[sid] = agent_dir.name
        except OSError:
            pass
    return sid_map


def _collect_events(path: Path, cutoff: datetime):
    """One pass: return (arm_events, skill_msgids, skill_events).

    arm_events   : list of {ts_dt, ts_str, msg_id} for ScheduleWakeup(600,sentinel)
    skill_msgids : set of message.id that contain a Skill(aspirations) tool_use
    skill_events : list of (ts_dt, msg_id) for Skill(aspirations) tool_uses
    """
    arm_events = []
    skill_msgids = set()
    skill_events = []
    try:
        with open(path, "rb") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                ts_dt = _parse_ts(e.get("timestamp", ""))
                if ts_dt is None or ts_dt < cutoff:
                    continue
                msg = e.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    name = item.get("name")
                    inp = item.get("input") or {}
                    if name == "ScheduleWakeup":
                        ds = inp.get("delaySeconds")
                        pr = inp.get("prompt")
                        if (ds == DEADMAN_DELAY or ds == str(DEADMAN_DELAY)) and pr == SENTINEL:
                            arm_events.append({"ts_dt": ts_dt,
                                               "ts_str": e.get("timestamp", ""),
                                               "msg_id": mid})
                    elif name == "Skill":
                        # exact "aspirations" (the loop re-entry) — NOT
                        # "aspirations-spark", which precedes the pair on deep
                        # recurring closes.
                        if str(inp.get("skill") or inp.get("command") or "") == "aspirations":
                            if mid is not None:
                                skill_msgids.add(mid)
                            skill_events.append((ts_dt, mid))
    except OSError:
        pass
    skill_events.sort(key=lambda x: x[0])
    return arm_events, skill_msgids, skill_events


def _classify_arms(arm_events, skill_msgids, skill_events) -> list:
    """Classify each arm: batched | followed | orphan.

    A non-batched arm is `followed` iff a later Skill(aspirations) re-enters
    its iteration within FOLLOW_WINDOW_S. Each Skill is assigned to the MOST
    RECENT non-batched arm preceding it — so one Skill marks at most one arm
    followed. Without that single-assignment rule, two arms clustered within
    the window both claim the same Skill, silently UNDERCOUNTING orphans (an
    arm whose own re-entry is missing borrows the next arm's Skill). The
    latest-preceding rule matches loop shape: a Skill re-enters the iteration
    that most recently armed. (deadman review, 2026-06-24)
    """
    arms = sorted(arm_events, key=lambda a: a["ts_dt"])
    klass = {}
    nonbatched = []
    for idx, a in enumerate(arms):
        mid = a["msg_id"]
        if mid is not None and mid in skill_msgids:
            klass[idx] = "batched"
        else:
            nonbatched.append(idx)
    followed = set()
    for ts_s, _m in skill_events:
        best = None
        for idx in nonbatched:
            a = arms[idx]
            if a["ts_dt"] <= ts_s and (ts_s - a["ts_dt"]).total_seconds() <= FOLLOW_WINDOW_S:
                if best is None or a["ts_dt"] > arms[best]["ts_dt"]:
                    best = idx
        if best is not None:
            followed.add(best)
    out = []
    for idx, a in enumerate(arms):
        k = klass[idx] if idx in klass else ("followed" if idx in followed else "orphan")
        out.append({"ts": a["ts_str"], "klass": k})
    return out


def _verdict(flagged: bool, total: int, orphan: int) -> str:
    if not flagged:
        return "off"
    if total == 0:
        return "NOT-ARMING"   # flagged but no arm in window (no close yet, or dropped)
    if orphan > 0:
        return "ORPHANS"      # net armed but loop did not visibly re-enter (investigate)
    return "ARMED-OK"         # arming + loop re-enters every time (batched and/or split)


def _build_report(transcripts_dir: Path, project_root: Path, since_hours: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    sid_to_agent = _session_to_agent(project_root)
    flagged = _flagged_agents(project_root)
    per_agent = {}
    for agent in flagged:
        per_agent[agent] = {"flagged": flagged[agent], "sids": [], "arms_total": 0,
                            "batched": 0, "followed": 0, "orphan": 0,
                            "last_arm_utc": None}
    try:
        files = sorted(transcripts_dir.glob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        agent = sid_to_agent.get(path.stem)
        if agent is None:
            continue
        arm_events, skill_msgids, skill_events = _collect_events(path, cutoff)
        if not arm_events:
            continue
        arms = _classify_arms(arm_events, skill_msgids, skill_events)
        bucket = per_agent.setdefault(agent, {
            "flagged": flagged.get(agent, False), "sids": [], "arms_total": 0,
            "batched": 0, "followed": 0, "orphan": 0, "last_arm_utc": None})
        bucket["sids"].append(path.stem)
        for a in arms:
            bucket["arms_total"] += 1
            bucket[a["klass"]] += 1
            if bucket["last_arm_utc"] is None or a["ts"] > bucket["last_arm_utc"]:
                bucket["last_arm_utc"] = a["ts"]
    for agent, b in per_agent.items():
        b["batched_rate"] = (round(b["batched"] / b["arms_total"], 3)
                             if b["arms_total"] else None)
        b["verdict"] = _verdict(b["flagged"], b["arms_total"], b["orphan"])
    bad = [a for a, b in per_agent.items() if b["verdict"] in ("ORPHANS", "NOT-ARMING")]
    totals = {
        "since_hours": since_hours,
        "cutoff_utc": cutoff.isoformat(),
        "transcripts_dir": str(transcripts_dir),
        "transcripts_dir_exists": transcripts_dir.exists(),
        "flagged_agents": sorted(a for a, v in flagged.items() if v),
        "noncompliant_agents": sorted(bad),
    }
    return {"totals": totals, "per_agent": per_agent}


def _print_human(report: dict) -> None:
    t = report["totals"]
    print(f"deadman-arm-audit window={t['since_hours']}h "
          f"flagged={t['flagged_agents']} noncompliant={t['noncompliant_agents']}")
    if not t["transcripts_dir_exists"]:
        print(f"(transcripts dir missing: {t['transcripts_dir']})")
        return
    for agent, b in sorted(report["per_agent"].items()):
        rate = "n/a" if b["batched_rate"] is None else f"{b['batched_rate']*100:.0f}%"
        last = (b["last_arm_utc"] or "")[:19]
        print(f"  {agent:8} flag={'ON ' if b['flagged'] else 'off'} "
              f"verdict={b['verdict']:11} arms={b['arms_total']} "
              f"batched={b['batched']} followed={b['followed']} orphan={b['orphan']} "
              f"batched_rate={rate} last_arm={last}")


def main():
    args = _parse_args()
    report = _build_report(args.transcripts_dir, args.project_root, args.since_hours)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    if args.exit_on_noncompliance:
        sys.exit(1 if report["totals"]["noncompliant_agents"] else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
