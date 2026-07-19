#!/usr/bin/env python3
"""Operator status aggregator — one command, composite view.

Replaces the 6-probe re-entry ritual (state + mode + persona + team-state +
handoffs + pipeline + blockers + context zone) with a single pretty-printed
box or --json blob.

Usage:
  bash core/scripts/status.sh              # pretty box
  bash core/scripts/status.sh --json       # machine-readable JSON
  bash core/scripts/status.sh --field <k>  # single field (plain), e.g. handoffs_inbound

Sources (all reuse existing APIs):
  session.py state/mode/persona  — per-agent session signals
  world/team-state.yaml          — direct YAML read
  _rt.rt_call /v1/pipeline/read  — pipeline hypothesis counts (daemon)
  world/aspirations.jsonl        — handoff scan (pattern from boot/SKILL.md L304-346)
  <agent>/session/context-budget.json — context zone snapshot

Design notes:
  - Fails-open on missing pieces: prints "unknown"/"not initialized" rather
    than erroring. Operator convenience tool, not a gate.
  - Context-budget is READ from its JSON snapshot, not recomputed — the
    status-line hook owns that write.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml  # type: ignore

from _paths import AGENT_DIR, AGENT_NAME, CORE_ROOT, WORLD_DIR

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hours_since(ts_str: str | None) -> float | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_str))
    except Exception:
        return None
    delta = (datetime.now() - dt).total_seconds() / 3600.0
    # Cross-machine clock/TZ skew (0): a partner on a machine whose
    # local wall-clock is ahead writes a future-dated liveness stamp, and
    # datetime.now() is machine-local (naive), so delta goes negative. Clamp
    # to 0 — a future-dated stamp means "just active", never "active in the
    # future". Skew-safe; mirrors goal-selector.hours_since's negative guard.
    return max(0.0, delta)


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "?"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 24:
        h = int(hours)
        m = int((hours - h) * 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d = int(hours // 24)
    h = int(hours - d * 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _run_py(script_path: Path, *args: str) -> tuple[int, str]:
    """Invoke a core/scripts python script with the current interpreter.

    We use sys.executable (not 'python3') so we bypass any Windows Store stub
    that might intercept 'python3'. We also avoid bash entirely here — the .sh
    wrappers exist for humans/hook callers, but for in-process aggregation the
    direct call is simpler and more reliable cross-platform.
    """
    try:
        r = subprocess.run(
            [sys.executable, str(script_path), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, cwd=str(Path(__file__).parent),
        )
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return 1, ""


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_session() -> dict:
    """state, mode, persona via direct subprocess to session.py.

    We invoke session.py with sys.executable rather than shelling out to the
    .sh wrappers because on Git-Bash/Windows the child bash process rewrites
    drive-letter paths and fails to locate the script. The .sh wrappers
    remain the public interface for humans and hook callers.
    """
    session_py = CORE_ROOT / "scripts" / "session.py"
    _, state = _run_py(session_py, "state", "get")
    _, mode = _run_py(session_py, "mode", "get")
    _, persona = _run_py(session_py, "persona", "get")
    return {
        "state": state or "UNKNOWN",
        "mode": mode or "unknown",
        "persona": persona or "unknown",
    }


def collect_team_state() -> dict:
    """Read team-state.yaml. Guarantees a dict — malformed yaml collapses to {}.

    Downstream code relies on the dict contract. Do not change the return
    type without updating build_status(); the isinstance guards there were
    intentionally removed when this contract tightened.
    """
    path = WORLD_DIR / "team-state.yaml"
    data = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    #  sharding: overlay per-agent row files (rows win newest-wins).
    # Preserves the dict contract — compose failure returns the core dict.
    try:
        from _team_state import compose_state
        data = compose_state(data, WORLD_DIR)
    except Exception:
        pass
    return data


def collect_handoffs_inbound() -> dict:
    """Count goals where handoff_to == AGENT_NAME AND status in (pending, in-progress).

    Returns ids (top 5, for one-liner rendering) AND top (first 3 with titles,
    for per-item rendering by boot/SKILL.md). This is the ONE scan path — if a
    caller wants a different summary shape, add it here rather than re-scanning
    aspirations.jsonl. See boot/SKILL.md Step 1.7 (Pending handoffs) which
    consumes `--field handoffs_inbound`.
    """
    empty = {"count": 0, "oldest_age_hours": None, "ids": [], "top": []}
    if not AGENT_NAME:
        return empty
    path = WORLD_DIR / "aspirations.jsonl"
    if not path.exists():
        return empty

    pending = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            asp = json.loads(s)
        except Exception:
            continue
        for g in asp.get("goals", []) or []:
            if g.get("handoff_to") == AGENT_NAME and g.get("status") in ("pending", "in-progress"):
                pending.append({
                    "id": g.get("id"),
                    "title": (g.get("title") or "")[:60],
                    "age_h": _hours_since(g.get("handoff_created_at")),
                })

    ages = [p["age_h"] for p in pending if p["age_h"] is not None]
    return {
        "count": len(pending),
        "oldest_age_hours": max(ages) if ages else None,
        "ids": [p["id"] for p in pending[:5]],
        "top": [{"id": p["id"], "title": p["title"]} for p in pending[:3]],
    }


def collect_pipeline_counts() -> dict:
    """Pipeline counts via daemon.

    Query contract copied from pipeline-read.sh: GET /v1/pipeline/read?counts=1.
    The pipeline.py read CLI was deleted in the 2026-05-14 cutover.
    """
    try:
        raw = _rt.rt_call("GET", "/v1/pipeline/read", query="counts=1")
    except _rt.RtError:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def collect_context_zone() -> dict:
    """Read <agent>/session/context-budget.json. Set by PreCompact-ish hook."""
    if AGENT_DIR is None:
        return {}
    path = Path(AGENT_DIR) / "session" / "context-budget.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

def build_status() -> dict:
    session = collect_session()
    team = collect_team_state()           # guaranteed dict (see collector contract)
    handoffs = collect_handoffs_inbound()
    pipeline = collect_pipeline_counts()
    ctx = collect_context_zone()

    agent_status = team.get("agent_status") or {}
    active_blockers = team.get("active_blockers") or []
    critical_blockers = team.get("critical_blockers") or []
    strategic = team.get("strategic_focus") or {}

    return {
        "agent": AGENT_NAME or "NO_AGENT",
        "session": session,
        "team": {
            "last_updated": team.get("last_updated"),
            "last_updated_by": team.get("last_updated_by"),
            "agent_status": agent_status,
            "strategic_focus": strategic.get("primary"),
            "active_blockers_count": len(active_blockers),
            "critical_blockers_count": len(critical_blockers),
        },
        "handoffs_inbound": handoffs,
        "pipeline": {
            "active": pipeline.get("active", 0),
            "discovered": pipeline.get("discovered", 0),
        },
        "context": {
            "zone": ctx.get("zone"),
            "used_pct": ctx.get("used_pct"),
            "pct_to_autocompact": ctx.get("pct_to_autocompact"),
        },
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_pretty(s: dict) -> str:
    lines: list[str] = []
    lines.append("═══ STATUS ════════════════════════════════════")
    lines.append(f"Agent:    {s['agent']}")

    sess = s["session"]
    lines.append(
        f"State:    {sess['state']} | Mode: {sess['mode']} | Persona: {sess['persona']}"
    )

    team = s["team"]
    if team["last_updated"] or team["agent_status"]:
        lines.append("Team:")
        for name, rec in (team["agent_status"] or {}).items():
            la = rec.get("last_active") if isinstance(rec, dict) else None
            focus = rec.get("current_focus") if isinstance(rec, dict) else None
            live = rec.get("live_phase") if isinstance(rec, dict) else None
            age = _hours_since(la)
            live_str = f"  [{live}]" if live else ""
            focus_str = f"  (focus: {focus})" if focus else ""
            lines.append(f"  {name:<6}  last_active {_fmt_age(age):<8}ago{live_str}{focus_str}")
        if team["last_updated"]:
            age = _hours_since(team["last_updated"])
            by = team["last_updated_by"] or "?"
            lines.append(
                f"  team-state file last_updated {_fmt_age(age)} by {by}"
            )
    else:
        lines.append("Team:     not initialized")

    h = s["handoffs_inbound"]
    if h["count"]:
        oldest = _fmt_age(h["oldest_age_hours"])
        lines.append(f"Handoffs inbound:  {h['count']} pending (oldest {oldest})")
    else:
        lines.append("Handoffs inbound:  0")

    p = s["pipeline"]
    lines.append(
        f"Pipeline:          {p['active']} active, {p['discovered']} discovered"
    )

    ab = team["active_blockers_count"]
    cb = team["critical_blockers_count"]
    lines.append(f"Blockers:          {ab} active, {cb} critical")

    sf = team["strategic_focus"] or "none set"
    lines.append(f"Strategic focus:   {sf}")

    ctx = s["context"]
    if ctx["zone"]:
        lines.append(
            f"Context zone:      {ctx['zone']} ({ctx['used_pct']}% used, {ctx['pct_to_autocompact']}% to autocompact)"
        )
    else:
        lines.append("Context zone:      unknown (no status-line snapshot yet)")

    lines.append("═══════════════════════════════════════════════")
    return "\n".join(lines)


def render_field(s: dict, field: str) -> str:
    # Flatten paths via dotted notation (handoffs_inbound.count, team.last_updated, etc.)
    cur: object = s
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    if isinstance(cur, (dict, list)):
        return json.dumps(cur)
    return "" if cur is None else str(cur)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Composite operator status")
    ap.add_argument("--json", action="store_true", help="Emit JSON blob")
    ap.add_argument("--field", help="Emit a single field (dotted path) as plain text")
    args = ap.parse_args()

    if not AGENT_NAME and not args.field:
        # Graceful NO_AGENT output so status.sh is callable pre-/start.
        print("═══ STATUS ════════════════════════════════════")
        print("Agent:    NO_AGENT (run /start <name> to bind)")
        print("═══════════════════════════════════════════════")
        return 0

    s = build_status()

    if args.field:
        print(render_field(s, args.field))
        return 0
    if args.json:
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0
    print(render_pretty(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
