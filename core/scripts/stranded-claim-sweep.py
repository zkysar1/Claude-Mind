#!/usr/bin/env python3
"""Stranded Claim Sweep — release in-progress claims orphaned by autocompact.

Canonical incident (bravo session-35, g-115-23, 2026-05-21): autocompact
fires AFTER aspirations-claim.sh succeeds but BEFORE Phase 4 execution
starts. The post-compaction session inherits a stranded in-progress claim
(status=in-progress, claimed_by=self, team-state.in_flight set) with no
execution-diary entry for that goal in the current session — the next
iteration's selector sees the goal as "owned" and skips it; the goal sits
frozen until /felt-sense-checkin Phase 2 happens to catch it on its
75-goal cadence.

Sweep logic per g-115-1044:

  for each goal where claimed_by == MIND_AGENT AND status == "in-progress":
      if execution-diary has an entry for this goal_id AFTER claimed_at:
          NOT stranded (work is in progress)
      elif (now - claimed_at) < stale_threshold_minutes (default 5):
          NOT stranded (fresh claim, race-condition window)
      else:
          STRANDED

  with --apply:
      - POST /v1/aspirations/release           # strips claimed_by + claimed_at
      - POST /v1/aspirations/update-goal       # field=status, value="pending"
      - team-state.py clear-in-flight          # if in_flight matches goal_id

Output (JSON to stdout): {"scanned": K, "scanned_no_claim": J,
"stranded": [...], "released": N, "kept": M, "dry_run": bool,
"agent": "<name>", "now": "<iso>"}.

Second shape (g-115-1691): the claimed_by==MIND_AGENT query above is
structurally blind to agent-source goals that went in-progress WITHOUT a
claim. Agent-source goals skip aspirations-claim.sh (loop digest Phase 4
claims only IF source==world), and that wrapper is the sole writer of
claimed_by — so a stranded agent-source in-progress goal carries
claimed_by=unset and never matches the query. The sweep ALSO scans the
agent-source active aggregate for status==in-progress goals with no
claimed_by, using last_modified as the stale-age basis (no claimed_at
exists — claimed_by/claimed_at are written together by the claim wrapper).
For a genuinely stranded goal (no writes after it went in-progress)
last_modified == the in-progress-transition moment; the diary check carries
the primary detection weight regardless. A no-claim stranded goal has
nothing to release (no claim) and no team-state in_flight to clear
(in_flight is written at claim time): the operative action is the
status->pending flip that returns it to the selectable pool. World goals
always claim, so the no-claim shape is agent-source-only.

Exit codes:
  0 — sweep ran (dry-run or apply). Output is JSON.
  1 — fatal error (missing MIND_AGENT, daemon unreachable, etc.). Diagnostic
      on stderr.

Invoked by:
  - .claude/skills/aspirations/SKILL.md Phase -0.5c (after compact-restore-slots.sh)
  - User / debugging via the .sh wrapper

Implementation note: framework Python scripts on Windows cannot subprocess
to bash wrappers (rb-225/rb-247 — every bash invocation form fails or
hangs). The canonical Python -> daemon client is `_rt.py`; team-state has
no daemon endpoint so we invoke its CLI via `sys.executable` (Python on
Python, never through bash).

Cross-references:
  - g-115-1044 — originating Idea goal
  - rb-428 — sentinel-lifecycle pattern (related)
  - .claude/rules/stop-hook-compliance.md — claim/in_flight invariants
  - core/scripts/_rt.py — canonical Python -> daemon client
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _rt  # canonical Python -> daemon client  # noqa: E402
from _paths import agent_dir  # type: ignore  # noqa: E402

DEFAULT_STALE_MINUTES = 5


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _parse_iso(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _agent_name() -> str:
    name = (os.environ.get("MIND_AGENT") or "").strip()
    if not name:
        raise SystemExit(
            "MIND_AGENT not set — sweep cannot run without an agent binding"
        )
    return name


def _query_claimed_goals(agent: str) -> List[Dict[str, Any]]:
    """Daemon: GET /v1/aspirations/query — list in-progress goals claimed by agent."""
    try:
        raw = _rt.rt_call(
            "GET",
            "/v1/aspirations/query",
            query={
                "goal_status": "in-progress",
                "goal_field_name": "claimed_by",
                "goal_field_value": agent,
            },
        )
    except _rt.RtError as e:
        raise SystemExit(f"aspirations query failed: {e}") from e
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError as e:
        raise SystemExit(f"aspirations query returned non-JSON: {raw!r}") from e
    return data if isinstance(data, list) else []


def _read_goal_claimed_at(asp_id: str, goal_id: str, source: str) -> Optional[str]:
    """Pull claimed_at from the live aspiration record.

    The query endpoint omits claimed_at (intentional — query is identity
    info only). We need the timestamp to compute age, so we hit the active
    aggregate read.
    """
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError:
        return None
    # raw can be aggregate JSON ({"aspirations": [...]}) or raw list.
    try:
        decoded = _rt.tolerant_decode_aggregate("active", raw)
    except Exception:
        return None
    asps = decoded.get("aspirations", []) if isinstance(decoded, dict) else decoded
    for asp in asps or []:
        if asp.get("id") != asp_id:
            continue
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                ca = g.get("claimed_at")
                return ca if isinstance(ca, str) and ca else None
    return None


def _diary_has_entry_after(agent: str, goal_id: str, since_iso: str) -> bool:
    """True iff execution-diary.jsonl has an entry for goal_id with ts >= since_iso."""
    diary_path = agent_dir(agent) / "session" / "execution-diary.jsonl"
    if not diary_path.exists():
        return False
    since_dt = _parse_iso(since_iso)
    if since_dt is None:
        return False
    try:
        with diary_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("goal_id") != goal_id:
                    continue
                ts = _parse_iso(entry.get("timestamp", ""))
                if ts is None:
                    continue
                if ts >= since_dt:
                    return True
    except OSError:
        return False
    return False


def _read_team_in_flight(agent: str) -> Optional[Dict[str, Any]]:
    """Daemon: GET /v1/team-state/read — return agent's in_flight block or None."""
    try:
        raw = _rt.rt_call(
            "GET",
            "/v1/team-state/read",
            query={"field": f"agent_status.{agent}.in_flight", "format": "json"},
        )
    except _rt.RtError:
        return None
    raw = (raw or "").strip()
    if not raw or raw == "null":
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _release_goal(goal_id: str, source: str) -> Dict[str, Any]:
    """Release claim (strip claimed_by/claimed_at) + flip status pending.

    Two separate daemon writes — both must succeed for the release to be
    complete. Partial-failure is possible (release succeeds, status update
    fails). Caller surfaces the breaking step in the JSON output.
    """
    try:
        _rt.rt_call(
            "POST",
            "/v1/aspirations/release",
            query={"id": goal_id, "source": source},
        )
    except _rt.RtError as e:
        return {
            "ok": False,
            "step": "aspirations-release",
            "error": str(e)[:400],
        }
    try:
        _rt.rt_call(
            "POST",
            "/v1/aspirations/update-goal",
            query={"id": goal_id, "field": "status", "source": source},
            body=json.dumps("pending"),
        )
    except _rt.RtError as e:
        return {
            "ok": False,
            "step": "aspirations-update-goal-status",
            "error": str(e)[:400],
        }
    return {"ok": True, "step": "released+pending"}


def _clear_team_in_flight(agent: str, goal_id: str) -> Dict[str, Any]:
    """Clear team-state in_flight ONLY if the recorded goal_id matches.

    team-state has no daemon endpoint (no /v1/team-state/clear-in-flight).
    Invoke the CLI via sys.executable (Python on Python, never through bash
    — see rb-225/rb-247 for the Windows bash subprocess hang).
    """
    current = _read_team_in_flight(agent)
    if not current or current.get("goal_id") != goal_id:
        return {"cleared": False, "reason": "in_flight did not match goal_id"}
    try:
        res = subprocess.run(
            [
                sys.executable,
                str(CORE_ROOT / "scripts" / "team-state.py"),
                "clear-in-flight",
                "--agent", agent,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"cleared": False, "reason": "team-state.py clear-in-flight timeout"}
    if res.returncode != 0:
        return {
            "cleared": False,
            "reason": (res.stderr or "").strip()[:400] or "non-zero rc",
        }
    return {"cleared": True, "reason": "matched and cleared"}


def _query_inprogress_no_claim(agent: str) -> List[Dict[str, Any]]:
    """1: agent-source in-progress goals with NO claimed_by.

    The claimed-by query path (_query_claimed_goals) cannot see these:
    agent-source goals skip aspirations-claim.sh (the only claimed_by writer),
    so they sit status=in-progress with claimed_by unset and never match the
    claimed_by==agent filter. Read the agent-source active aggregate directly
    and surface in-progress goals with no claim, carrying last_modified as the
    stale-age basis (no claimed_at exists). Fail-open: any read/decode error
    yields an empty list (the sweep degrades to claimed-only, never crashes).
    """
    try:
        raw = _rt.aspirations_read(source="agent", active=True)
    except _rt.RtError:
        return []
    try:
        decoded = _rt.tolerant_decode_aggregate("active", raw)
    except Exception:
        return []
    asps = decoded.get("aspirations", []) if isinstance(decoded, dict) else decoded
    out: List[Dict[str, Any]] = []
    for asp in asps or []:
        asp_id = asp.get("id", "")
        for g in asp.get("goals", []) or []:
            if g.get("status") != "in-progress":
                continue
            if g.get("claimed_by"):  # has a claim — the claimed path handles it
                continue
            out.append({
                "goal_id": g.get("id", ""),
                "asp_id": asp_id,
                "source": "agent",
                "title": g.get("title", ""),
                "last_modified": g.get("last_modified"),
            })
    return out


def _flip_pending_no_claim(goal_id: str, source: str) -> Dict[str, Any]:
    """Flip a stranded no-claim in-progress goal back to pending (1).

    No claim to release (claimed_by was never set) and no team-state in_flight
    to clear (in_flight is written at claim time). The single operative action
    is the status->pending flip that returns the goal to the selectable pool.
    """
    try:
        _rt.rt_call(
            "POST",
            "/v1/aspirations/update-goal",
            query={"id": goal_id, "field": "status", "source": source},
            body=json.dumps("pending"),
        )
    except _rt.RtError as e:
        return {
            "ok": False,
            "step": "aspirations-update-goal-status",
            "error": str(e)[:400],
        }
    return {"ok": True, "step": "flipped-pending"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Release stranded in-progress claims (post-autocompact recovery)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually release stranded claims. Without this flag, the sweep "
             "is dry-run (reports findings but mutates nothing).",
    )
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=DEFAULT_STALE_MINUTES,
        help=f"Minimum age (minutes) of claimed_at before a claim with no "
             f"diary activity is considered stranded. Default: "
             f"{DEFAULT_STALE_MINUTES} (race-condition guard — claims younger "
             f"than this may legitimately be mid-Phase-4 setup).",
    )
    args = parser.parse_args()

    agent = _agent_name()
    now = dt.datetime.now().replace(microsecond=0)
    stale_threshold = dt.timedelta(minutes=args.stale_minutes)

    claimed = _query_claimed_goals(agent)
    summary: Dict[str, Any] = {
        "agent": agent,
        "now": now.isoformat(),
        "stale_minutes": args.stale_minutes,
        "dry_run": not args.apply,
        "scanned": len(claimed),
        "stranded": [],
        "kept": 0,
        "released": 0,
    }

    for entry in claimed:
        goal_id = entry.get("goal_id", "")
        asp_id = entry.get("asp_id", "")
        source = entry.get("source", "world")
        title = entry.get("title", "")
        if not goal_id or not asp_id:
            continue

        claimed_at_iso = _read_goal_claimed_at(asp_id, goal_id, source)

        if not claimed_at_iso:
            summary["stranded"].append({
                "goal_id": goal_id,
                "asp_id": asp_id,
                "source": source,
                "title": title,
                "verdict": "kept",
                "reason": "claimed_at missing/unreadable — release manually if persistent",
            })
            summary["kept"] += 1
            continue

        claimed_at = _parse_iso(claimed_at_iso)
        if claimed_at is None:
            summary["stranded"].append({
                "goal_id": goal_id,
                "asp_id": asp_id,
                "source": source,
                "title": title,
                "verdict": "kept",
                "reason": f"claimed_at unparseable ({claimed_at_iso!r})",
            })
            summary["kept"] += 1
            continue

        has_recent_diary = _diary_has_entry_after(agent, goal_id, claimed_at_iso)
        age = now - claimed_at

        if has_recent_diary:
            summary["kept"] += 1
            continue  # work is happening — not stranded

        if age < stale_threshold:
            summary["kept"] += 1
            continue  # too fresh — race window

        record: Dict[str, Any] = {
            "goal_id": goal_id,
            "asp_id": asp_id,
            "source": source,
            "title": title,
            "claimed_at": claimed_at_iso,
            "age_minutes": round(age.total_seconds() / 60.0, 2),
            "verdict": "stranded",
            "reason": "no diary entry after claimed_at AND age >= stale threshold",
        }

        if args.apply:
            rel = _release_goal(goal_id, source)
            record["release_result"] = rel
            if rel.get("ok"):
                clear = _clear_team_in_flight(agent, goal_id)
                record["team_state_clear"] = clear
                summary["released"] += 1
                record["verdict"] = "released"
            else:
                summary["kept"] += 1
                record["verdict"] = "release-failed"

        summary["stranded"].append(record)

    # 1: second shape — agent-source in-progress goals with NO
    # claimed_by (structurally invisible to the claimed_by==agent query above).
    no_claim = _query_inprogress_no_claim(agent)
    summary["scanned_no_claim"] = len(no_claim)
    for entry in no_claim:
        goal_id = entry.get("goal_id", "")
        asp_id = entry.get("asp_id", "")
        source = entry.get("source", "agent")
        title = entry.get("title", "")
        lm_iso = entry.get("last_modified")
        if not goal_id or not asp_id:
            continue

        if not lm_iso or not isinstance(lm_iso, str):
            summary["stranded"].append({
                "goal_id": goal_id, "asp_id": asp_id, "source": source,
                "title": title, "shape": "no-claim", "verdict": "kept",
                "reason": "last_modified missing/unreadable — cannot age; "
                          "release manually if persistent",
            })
            summary["kept"] += 1
            continue

        lm = _parse_iso(lm_iso)
        if lm is None:
            summary["stranded"].append({
                "goal_id": goal_id, "asp_id": asp_id, "source": source,
                "title": title, "shape": "no-claim", "verdict": "kept",
                "reason": f"last_modified unparseable ({lm_iso!r})",
            })
            summary["kept"] += 1
            continue

        has_recent_diary = _diary_has_entry_after(agent, goal_id, lm_iso)
        age = now - lm

        if has_recent_diary:
            summary["kept"] += 1
            continue  # work is happening — not stranded

        if age < stale_threshold:
            summary["kept"] += 1
            continue  # too fresh — race / mid-transition window

        record = {
            "goal_id": goal_id, "asp_id": asp_id, "source": source,
            "title": title, "shape": "no-claim",
            "last_modified": lm_iso,
            "age_minutes": round(age.total_seconds() / 60.0, 2),
            "verdict": "stranded",
            "reason": "in-progress with no claimed_by, no diary entry after "
                      "last_modified AND age >= stale threshold",
        }

        if args.apply:
            res = _flip_pending_no_claim(goal_id, source)
            record["flip_result"] = res
            if res.get("ok"):
                summary["released"] += 1
                record["verdict"] = "released"
            else:
                summary["kept"] += 1
                record["verdict"] = "release-failed"

        summary["stranded"].append(record)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
