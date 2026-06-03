#!/usr/bin/env python3
"""Convert streak-break signals into Investigate goals — and auto-resolve
canaries whose source goal has since recovered cadence.

Reads <agent>/session/streak-breaks.jsonl entries written by
aspirations.py cmd_complete_by when a recurring goal's currentStreak
resets due to a missed interval (elapsed > 2 × interval_hours).

For each unprocessed entry, files an Investigate goal on the parent
aspiration asking what disrupted the cadence. Marks the entry
processed so we don't refile across iterations.

Origin: LifingPolls plan item 1 (2026-05-08).

Dedup rules:
  1. Skip if a similar Investigate already exists in the last 48h
     (title regex match on goal_id).
  2. Skip entries already marked processed=True.

Auto-resolve sweep (added 2026-05-18, g-115-919):
  After the filing pass, scan open `investigate:streak-break:*` canaries
  across world+agent queues. For each, look up the source recurring goal's
  current lastAchievedAt. If the source has fired AFTER the canary was
  filed AND within streak_mult × interval_hours of the filing time, mark
  the canary completed with a note that the gap was transient (e.g.,
  session inactivity that happened to span the expected fire window).

  Rationale: streak_mult is read from
  core/config/aspirations.yaml recurring.streak_mult — single source of
  truth shared with the canary-filing writer at
  mind_api/src/endpoints/aspirations_write.py cmd_complete_by. The same
  multiplier that fires the canary also defines the "recovered cadence"
  envelope — if next fire is inside the window, the original miss was
  marginal session-gap noise, not real drift. Forward-looking only:
  newly-filed canaries cannot self-resolve in the same run because
  lastAchievedAt was already set BEFORE the canary's filed_at. Sync
  enforcement: the config-knob lift (g-115-929, 2026-05-18) replaces the
  prior module-constant mirror — both sites now read the same key, so
  drift between filing-trigger and auto-resolve-window is structurally
  impossible.

Usage:
    py -3 core/scripts/streak-break-reflector.py [--agent NAME] [--dry-run]

--agent defaults to $MIND_AGENT (current session binding).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

DEFAULT_PRIORITY = "MEDIUM"
DEFAULT_CATEGORY = "framework-maintenance"
DEDUP_HOURS = 48


def _load_session_gap_threshold() -> float:
    """Load recurring.session_gap_threshold_hours from aspirations.yaml.

    Used by `_auto_resolve_recovered_canaries()` to decide whether a
    too-late recovery (outside the streak_mult window) is actually
    session-inactivity (agent wasn't running) rather than real cadence
    drift. The execution-diary is probed for activity gaps >= this
    threshold during the canary-filing-to-recovery span; a gap big
    enough to span the threshold flips the verdict from "leave open" to
    "auto-resolve with session-gap note".

    Fail-open: any read/parse error falls back to 2.0 hours (matches
    default in aspirations.yaml). Bounded by
    `modifiable.recurring.session_gap_threshold_hours: {min: 0.5,
    max: 999.0, default: 2.0}`. Set very high to disable.

    Overlay caveat (rb-335 pattern-match, intentional): reads raw
    aspirations.yaml; does NOT merge `meta/config-overrides.yaml`.
    Matches the sibling `_load_streak_mult` behavior. Overlay-aware
    reads for `recurring.*` knobs would need to touch both readers
    plus mind_api/src/endpoints/aspirations_write.py
    _load_streak_mult_config — a separate cross-cutting change.
    """
    cfg_path = PROJECT_ROOT / "core" / "config" / "aspirations.yaml"
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        v = (cfg.get("recurring") or {}).get("session_gap_threshold_hours")
        if v is None:
            return 2.0
        return float(v)
    except Exception:
        return 2.0


def _has_session_gap(start_dt: datetime, end_dt: datetime,
                     agent_dir: Path, gap_threshold_h: float) -> bool:
    """True iff execution-diary shows a gap >= gap_threshold_h in [start_dt, end_dt].

    Reads agent_dir/session/execution-diary.jsonl and inspects the
    timestamps that fall inside the window. Returns True when:
      - no diary entries at all in the window (entire span was inactive), OR
      - the gap between start_dt and the first in-window entry >= threshold, OR
      - the gap between the last in-window entry and end_dt >= threshold, OR
      - any consecutive pair of in-window entries is >= threshold apart.

    Fail-open on infrastructure errors: a missing diary or unreadable
    file returns False so the caller keeps the existing "leave open"
    semantics for the canary. A broken probe must never silently
    auto-resolve real drift. Note that zero PARSABLE entries inside a
    long window still returns True via the "no entries" rule above —
    that's an inactivity signal, not an infrastructure error.

    Closes the bravo 2026-05-20 catch-up cluster: 6 streak-break canaries
    from a 30h session gap stayed open under the streak_mult window
    because the recovery was always > 2 * interval. With this helper, the
    diary's 30h gap correctly classifies the misses as session-inactivity
    and the canaries auto-resolve on the next routine close.
    """
    diary = agent_dir / "session" / "execution-diary.jsonl"
    try:
        if not diary.exists():
            return False
        text = diary.read_text(encoding="utf-8")
    except OSError:
        return False
    timestamps: list[datetime] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(str(rec.get("timestamp", "")))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if start_dt <= ts <= end_dt:
            timestamps.append(ts)
    span_hours = (end_dt - start_dt).total_seconds() / 3600.0
    if not timestamps:
        # No activity in the window at all. Only counts as a gap if the
        # window itself is >= threshold (otherwise even a fresh canary
        # filed seconds before recovery would trip this).
        return span_hours >= gap_threshold_h
    timestamps.sort()
    if (timestamps[0] - start_dt).total_seconds() / 3600.0 >= gap_threshold_h:
        return True
    if (end_dt - timestamps[-1]).total_seconds() / 3600.0 >= gap_threshold_h:
        return True
    for i in range(1, len(timestamps)):
        if (timestamps[i] - timestamps[i - 1]).total_seconds() / 3600.0 >= gap_threshold_h:
            return True
    return False


def _load_streak_mult() -> float:
    """Load recurring.streak_mult from core/config/aspirations.yaml.

    Single source of truth shared with the writer site at
    mind_api/src/endpoints/aspirations_write.py cmd_complete_by — the
    same multiplier that fires the canary defines the "recovered cadence"
    envelope this script uses to close transient session-gap canaries
    (g-115-929, 2026-05-18). Drift between the two readers is now
    structurally impossible.

    Fail-open: any read/parse error falls back to 2.0 (the historical
    literal). Bounded by `modifiable.recurring.streak_mult: {min: 1.5,
    max: 5.0}` in aspirations.yaml.
    """
    cfg_path = PROJECT_ROOT / "core" / "config" / "aspirations.yaml"
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        v = (cfg.get("recurring") or {}).get("streak_mult")
        if v is None:
            return 2.0
        return float(v)
    except Exception:
        return 2.0


def _read_signals(log_path: Path) -> list[dict]:
    """Read all signal entries (processed + unprocessed)."""
    if not log_path.exists():
        return []
    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_signals(log_path: Path, entries: list[dict]) -> None:
    """Atomically replace the log with the updated entries."""
    tmp = log_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, log_path)


def _aspiration_for_goal(goal_id: str) -> tuple[str, str] | None:
    """Find the aspiration ID and queue source containing a goal.

    Reads world/aspirations.jsonl and <agent>/aspirations.jsonl. Returns
    a (asp_id, source) tuple where source is "world" or "agent", or None
    if the goal is not found in either queue. Used when the signal record
    lacks aspiration_id and source (legacy entries).

    Returning source is required so the caller can route the daemon
    write to the correct queue. _rt.aspirations_add_goal defaults to
    source="world"; without an explicit source, agent-queue aspirations
    look up as aspiration_not_found in world (g-115-961, g-115-980).
    Uses the same (path, source) candidate pattern as
    _auto_resolve_recovered_canaries below.
    """
    candidates: list[tuple[Path, str]] = []
    if WORLD_DIR is not None:
        candidates.append((WORLD_DIR / "aspirations.jsonl", "world"))
    if AGENT_DIR is not None:
        candidates.append((AGENT_DIR / "aspirations.jsonl", "agent"))
    for path, source in candidates:
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for g in asp.get("goals", []):
                    if g.get("id") == goal_id:
                        return asp.get("id"), source
    return None


def _recent_investigate_exists(goal_id: str, since_hours: int = DEDUP_HOURS) -> bool:
    """Check world + agent queues for a recent streak-break Investigate."""
    cutoff = datetime.now() - timedelta(hours=since_hours)
    # Title order can be either "streak-break — <goal_id>" or
    # "<goal_id> streak-break"; match if BOTH substrings are present.
    streak_re = re.compile(r"streak[- ]?break", re.IGNORECASE)
    goal_re = re.compile(re.escape(goal_id), re.IGNORECASE)
    candidates = []
    if WORLD_DIR is not None:
        candidates.append(WORLD_DIR / "aspirations.jsonl")
    if AGENT_DIR is not None:
        candidates.append(AGENT_DIR / "aspirations.jsonl")
    for path in candidates:
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for g in asp.get("goals", []):
                    title = g.get("title", "") or ""
                    if not (streak_re.search(title) and goal_re.search(title)):
                        continue
                    # cmd_add_goal writes `created_at`; older goals may have
                    # `created_date` or `created` — check all three so dedup
                    # works regardless of writer vintage.
                    created = (g.get("created_at") or g.get("created_date")
                               or g.get("created"))
                    if not created:
                        return True  # title match without date — assume recent
                    try:
                        c_dt = datetime.fromisoformat(str(created))
                    except (ValueError, TypeError):
                        continue
                    if c_dt > cutoff:
                        return True
    return False


def _file_investigate(entry: dict, asp_id: str, source: str,
                      dry_run: bool) -> tuple[bool, str]:
    """File an Investigate goal on the parent aspiration.

    source must be "world" or "agent" — required because
    _rt.aspirations_add_goal defaults to source="world", which silently
    misroutes agent-queue aspirations as aspiration_not_found (g-115-980).
    """
    goal_id = entry.get("goal_id") or "unknown"
    expected = entry.get("expected_interval_hours", 0)
    actual = entry.get("actual_elapsed_hours", 0)
    ratio = entry.get("lateness_ratio")

    title = f"Investigate: {goal_id} streak-break — {actual}h vs {expected}h expected"
    description = (
        f"Recurring goal {goal_id} missed its cadence: actual {actual}h "
        f"elapsed since last fire, expected ~{expected}h"
        f"{f' (lateness ratio {ratio}x)' if ratio else ''}.\n\n"
        f"What disrupted the cadence? Possible causes to check:\n"
        f"  - Loop blocked or sleeping past expected fire time\n"
        f"  - Goal claimed and released without completion\n"
        f"  - Selector deprioritized for N iterations (recurring saturation, "
        f"work-class skew, or claim-elsewhere)\n"
        f"  - Infrastructure dependency unavailable when goal would have fired\n"
        f"  - Agent session ended before goal's natural fire window\n\n"
        f"Output: short root-cause note. If pattern recurs across multiple "
        f"streak-breaks for the same goal, file a follow-up Idea proposing "
        f"interval adjustment or fire_when guard."
    )

    payload = {
        "title": title,
        "description": description,
        "priority": DEFAULT_PRIORITY,
        "category": DEFAULT_CATEGORY,
        "participants": ["agent"],
        "verification": {
            "outcomes": ["Root-cause note recorded in goal closure"],
            "checks": [],
            "preconditions": [],
        },
        "origin_signal": f"investigate:streak-break:{goal_id}",
    }

    if dry_run:
        print(f"[dry-run] would file Investigate on {asp_id}: {title}")
        return True, "dry-run"

    try:
        record = _rt.aspirations_add_goal(asp_id, payload, source=source)
        return True, record.get("id") or "filed"
    except _rt.RtError as e:
        return False, (e.body or str(e)).strip()[:200]


def _auto_resolve_recovered_canaries(dry_run: bool) -> tuple[int, list[str]]:
    """Auto-close open `investigate:streak-break:*` canaries whose source
    recurring goal has fired AFTER the canary was filed.

    Two recovery paths resolve the canary:
      1. **Cadence path** (original): recovery happened within
         `streak_mult * interval_hours` of the canary filing — the streak
         break was transient, real cadence resumed.
      2. **Session-gap path** (added 2026-05-20): recovery happened LATER
         than `streak_mult * interval_hours` (would normally be classified
         as real drift), BUT the agent's `execution-diary.jsonl` shows a
         gap >= `recurring.session_gap_threshold_hours` between canary
         filing and recovery — the cadence miss was structural (agent
         wasn't running), not a bug. The bravo 2026-05-20 catch-up cluster
         (6 Investigates from one 30h session gap) is the canonical
         incident this path closes. Set `session_gap_threshold_hours: 999`
         in aspirations.yaml to disable this path.

    Returns (resolved_count, resolved_goal_ids). Fail-open on every parse
    error; a malformed record never blocks the sweep.

    Forward-looking design — see module docstring "Auto-resolve sweep".
    """
    streak_mult = _load_streak_mult()
    session_gap_threshold_h = _load_session_gap_threshold()
    canary_re = re.compile(r"^investigate:streak-break:(g-\d+-\d+)$")
    candidates: list[tuple[Path, str]] = []
    if WORLD_DIR is not None:
        candidates.append((WORLD_DIR / "aspirations.jsonl", "world"))
    if AGENT_DIR is not None:
        candidates.append((AGENT_DIR / "aspirations.jsonl", "agent"))

    # Pass 1: build {source_goal_id: (lastAchievedAt, interval_hours)} for
    # every recurring goal across both queues. The source and its canary
    # share an aspiration, but the source itself can be world OR agent —
    # scan both.
    source_state: dict[str, tuple[str, float]] = {}
    for path, _src in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                asp = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in asp.get("goals", []) or []:
                if not isinstance(g, dict):
                    continue
                if not g.get("recurring") or not g.get("lastAchievedAt"):
                    continue
                gid = g.get("id")
                if not gid:
                    continue
                interval = g.get("interval_hours")
                if interval is None:
                    rd = g.get("remind_days")
                    interval = (rd * 24) if rd else 24
                try:
                    source_state[gid] = (str(g["lastAchievedAt"]),
                                         float(interval))
                except (TypeError, ValueError):
                    continue

    # Pass 2: walk open canaries; check recovery criterion; resolve.
    resolved: list[str] = []
    for path, source in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                asp = json.loads(line)
            except json.JSONDecodeError:
                continue
            for g in asp.get("goals", []) or []:
                if not isinstance(g, dict):
                    continue
                if g.get("status") not in ("pending", "in-progress"):
                    continue
                origin = g.get("origin_signal") or ""
                m = canary_re.match(origin)
                if not m:
                    continue
                source_goal_id = m.group(1)
                if source_goal_id not in source_state:
                    continue
                last_achieved_str, interval_h = source_state[source_goal_id]
                canary_created = (g.get("created_at") or g.get("created_date")
                                  or g.get("created"))
                if not canary_created:
                    continue
                try:
                    last_achieved = datetime.fromisoformat(
                        str(last_achieved_str))
                    canary_filed = datetime.fromisoformat(str(canary_created))
                except (ValueError, TypeError):
                    continue
                if last_achieved <= canary_filed:
                    # Source hasn't fired since canary was filed — leave open.
                    continue
                delta_h = (last_achieved - canary_filed).total_seconds() / 3600.0
                if delta_h > streak_mult * interval_h:
                    # Recovered too late by cadence standard. Check the
                    # session-gap escape hatch: if the agent's
                    # execution-diary shows >= session_gap_threshold_h of
                    # inactivity between canary filing and recovery, this
                    # was session-inactivity (agent wasn't running), not
                    # real drift. (Bravo 2026-05-20 catch-up cluster.)
                    if AGENT_DIR is None or not _has_session_gap(
                            canary_filed, last_achieved, AGENT_DIR,
                            session_gap_threshold_h):
                        continue  # real drift, leave open
                    note = (f"session-inactivity gap ({delta_h:.1f}h) - agent "
                            f"was not running continuously between canary "
                            f"and recovery; cadence resumed on session "
                            f"restart (threshold "
                            f"{session_gap_threshold_h}h, interval "
                            f"{interval_h}h)")
                else:
                    note = (f"transient session gap - cadence recovered "
                            f"{delta_h:.1f}h after canary filing "
                            f"(within {streak_mult}x interval={interval_h}h)")
                gid = g["id"]
                if dry_run:
                    print(f"[streak-break-reflector] [dry-run] would "
                          f"auto-resolve canary {gid} for {source_goal_id} "
                          f"(cadence recovered: {note})")
                    resolved.append(gid)
                    continue
                try:
                    _rt.aspirations_complete_by(
                        goal_id=gid, source=source, key_finding=note)
                except _rt.RtError as e:
                    print(f"[streak-break-reflector] WARN: auto-resolve "
                          f"for {gid} failed: "
                          f"{(e.body or str(e)).strip()[:160]}",
                          file=sys.stderr)
                    continue
                resolved.append(gid)
                print(f"[streak-break-reflector] auto-resolved canary "
                      f"{gid} for {source_goal_id} ({note})")
    return len(resolved), resolved


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent", default=os.environ.get("MIND_AGENT"),
                   help="Agent name (default $MIND_AGENT)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would happen, don't write")
    args = p.parse_args()

    if not args.agent:
        print("ERROR: MIND_AGENT not set and --agent not provided",
              file=sys.stderr)
        return 1

    if AGENT_DIR is None:
        print("ERROR: AGENT_DIR unresolved", file=sys.stderr)
        return 1

    log_path = AGENT_DIR / "session" / "streak-breaks.jsonl"
    filed_count = 0
    skipped_count = 0

    # Pass 1: file Investigate canaries from new signals (existing behavior).
    # An absent or empty log skips this pass — the auto-resolve sweep below
    # still runs to clean up canaries filed in prior sessions.
    entries: list[dict] = []
    unprocessed: list[dict] = []
    if log_path.exists():
        entries = _read_signals(log_path)
        unprocessed = [e for e in entries if not e.get("processed")]

    for entry in unprocessed:
        goal_id = entry.get("goal_id")
        if not goal_id:
            entry["processed"] = True
            continue

        if _recent_investigate_exists(goal_id):
            entry["processed"] = True
            entry["dedup_skipped"] = True
            skipped_count += 1
            continue

        # Resolve aspiration_id AND source. Signal entry may carry both
        # directly (preferred — no lookup cost); fall back to scanning
        # both queues when either is missing. source is required to
        # route the daemon write — _rt.aspirations_add_goal defaults to
        # "world" and silently misroutes agent-queue aspirations otherwise
        # (, ).
        asp_id = entry.get("aspiration_id")
        source = entry.get("source")
        if not (asp_id and source):
            found = _aspiration_for_goal(goal_id)
            if found is not None:
                found_asp, found_source = found
                if not asp_id:
                    asp_id = found_asp
                if not source:
                    source = found_source
        if not asp_id:
            print(f"[streak-break-reflector] WARN: aspiration not found for "
                  f"{goal_id}, skipping", file=sys.stderr)
            entry["processed"] = True
            entry["filing_error"] = "aspiration_not_found"
            continue

        ok, msg = _file_investigate(entry, asp_id, source, args.dry_run)
        if ok:
            entry["processed"] = True
            entry["filed_at"] = datetime.now().isoformat(timespec="seconds")
            filed_count += 1
            print(f"[streak-break-reflector] filed Investigate for {goal_id} "
                  f"on {asp_id}")
        else:
            print(f"[streak-break-reflector] WARN: filing for {goal_id} "
                  f"failed: {msg}", file=sys.stderr)
            entry["processed"] = True
            entry["filing_error"] = msg[:200]

    if entries and not args.dry_run:
        _write_signals(log_path, entries)

    # Pass 2: auto-resolve previously-filed canaries whose source goal has
    # since recovered cadence (). Always runs, independent of
    # whether new signals were processed in pass 1.
    resolved_count, _resolved_ids = _auto_resolve_recovered_canaries(
        args.dry_run)

    print(f"[streak-break-reflector] {filed_count} filed, "
          f"{skipped_count} dedup-skipped, "
          f"{resolved_count} auto-resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
