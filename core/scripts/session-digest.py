#!/usr/bin/env python3
"""session-digest.py --  (, FW-9).

Unified "what changed since last session" orientation digest. ONE read that
aggregates the six surfaces a returning agent session would otherwise check one
command at a time: handoff, own goal mix, recent commits, board (coordination +
findings), team-state (partner liveness), and the agent's own recent journal.
The point (g-317-07 desc) is to orient in one command instead of six -- intended
to surface at /prime or /start, or be run directly.

READ-ONLY + FAIL-OPEN by design. Writes nothing. Every section is independently
guarded: a missing, locked, or malformed source degrades THAT section to a stub
rather than failing the whole digest, and the script always exits 0 (only an
argparse error is non-zero). Source files are read directly (not via the daemon
wrappers) so the digest stays fast and has ZERO daemon-liveness coupling -- the
rb-1235 orphaned-from-daemon failure class does not apply to a read-only
aggregator, and direct reads are the established pattern for this script family
(team-contribution-report.py, g-318-02). It mutates no state and holds no lock,
so the "JSONL via scripts" write-protection rule does not apply.

Usage:
  py -3 core/scripts/session-digest.py [--agent NAME] [--format text|json]
       [--since-hours N] [--max-commits N] [--max-items N] [--no-commits]
       [--world-dir P] [--agent-dir P] [--now ISO8601]

--world-dir / --agent-dir override path resolution (tests); production resolves
via core/scripts/_paths.py (honours MIND_WORLD / MIND_AGENT). --now pins the
board lookback upper bound (tests; default = system local time). --no-commits
skips the git section (hermetic tests).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    import yaml  # PyYAML is available across core/scripts
except Exception:  # pragma: no cover - only on a broken env
    yaml = None

_OPEN_STATUSES = {"pending", "in-progress"}


# ----------------------------------------------------------------------------
# Path + IO helpers (mirrors team-contribution-report.py, )
# ----------------------------------------------------------------------------
def _resolve_dirs(agent, world_dir, agent_dir):
    """Return (world_path, agent_path). Explicit flags win; else _paths.py
    (honours MIND_WORLD / MIND_AGENT). Fail-soft: a _paths import failure
    leaves the unresolved side None, and every section degrades to a stub."""
    w = Path(world_dir) if world_dir else None
    a = Path(agent_dir) if agent_dir else None
    if w and a:
        return w, a
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
        import _paths  # type: ignore

        if w is None:
            w = Path(_paths.WORLD_DIR)
        if a is None and agent:
            a = Path(_paths.agent_dir(agent))
    except Exception as e:  # pragma: no cover - exercised only on broken envs
        print(f"WARN: path resolution via _paths failed: {e}", file=sys.stderr)
    return w, a


def _load_jsonl(path):
    """Yield parsed objects from a JSONL file. Tolerant: missing file -> nothing;
    a malformed line is skipped (telemetry/queue logs can carry a half-written
    tail line)."""
    if not path or not Path(path).exists():
        return
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (ValueError, TypeError):
            continue


def _load_yaml(path):
    if not yaml or not path or not Path(path).exists():
        return None
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_ts(value):
    """Parse an ISO8601-ish timestamp -> datetime or None. Tolerates trailing
    'Z', space-separated date/time, and fractional seconds."""
    if not isinstance(value, str) or not value:
        return None
    v = value.strip().replace("Z", "").replace(" ", "T").split(".")[0][:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _now(now_arg):
    if now_arg:
        dt = _parse_ts(now_arg)
        if dt:
            return dt
    return datetime.now()


# ----------------------------------------------------------------------------
# Sections (each pure; the build_digest guard wraps every call in try/except)
# ----------------------------------------------------------------------------
def section_handoff(agent_path):
    """Cross-session handoff summary. Absent handoff.yaml is the common case
    (a fresh session or a clean stop) -> present:false, not an error."""
    if not agent_path:
        return {"present": False}
    data = _load_yaml(Path(agent_path) / "session" / "handoff.yaml")
    if not isinstance(data, dict) or not data:
        return {"present": False}
    out = {"present": True}
    for k in ("session_id", "session", "updated_at", "created_at",
              "next_action", "first_action", "summary", "current_focus"):
        if data.get(k) not in (None, ""):
            out[k] = data[k]
    kb = data.get("known_blockers_active")
    if isinstance(kb, list):
        out["known_blockers_active"] = len(kb)
    elif kb not in (None, ""):
        out["known_blockers_active"] = kb
    return out


def section_own_goals(agent, world_path, agent_path):
    """Open goals the agent owns: everything still open in the agent's private
    queue, plus world goals whose claimed_by == agent. Grouped by status;
    in-progress goals are listed (they are the active work)."""
    in_progress, pending_count, by_asp = [], 0, {}

    def consume(asps, world_scope):
        nonlocal pending_count
        for asp in asps or []:
            if not isinstance(asp, dict) or asp.get("archived"):
                continue
            for g in asp.get("goals", []) or []:
                if not isinstance(g, dict):
                    continue
                if g.get("status") not in _OPEN_STATUSES:
                    continue
                # World goals only count as "own" when this agent claimed them.
                if world_scope and g.get("claimed_by") != agent:
                    continue
                if g.get("status") == "in-progress":
                    in_progress.append({"id": g.get("id"),
                                        "title": (g.get("title") or "")[:70],
                                        "asp": asp.get("id")})
                else:
                    pending_count += 1
                by_asp[asp.get("id")] = by_asp.get(asp.get("id"), 0) + 1

    if agent_path:
        consume(list(_load_jsonl(Path(agent_path) / "aspirations.jsonl")), False)
    if world_path:
        consume(list(_load_jsonl(Path(world_path) / "aspirations.jsonl")), True)
    return {"in_progress": in_progress, "pending_count": pending_count,
            "by_aspiration": by_asp}


def section_recent_commits(max_commits, no_commits):
    if no_commits:
        return {"skipped": True}
    try:
        r = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", f"-{max_commits}",
             "--oneline", "--no-decorate"],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"error": (r.stderr or "git log failed").strip()[:120]}
        return {"commits": [l for l in r.stdout.splitlines() if l.strip()]}
    except Exception as e:
        return {"error": str(e)[:120]}


def section_board(world_path, now, since_hours, max_items):
    cutoff = now - timedelta(hours=since_hours)
    out = {}
    for ch in ("coordination", "findings"):
        items = []
        if world_path:
            for o in _load_jsonl(Path(world_path) / "board" / f"{ch}.jsonl"):
                dt = _parse_ts(o.get("timestamp"))
                if dt is None or dt < cutoff:
                    continue
                items.append({"ts": o.get("timestamp"), "author": o.get("author"),
                              "type": o.get("type"),
                              "text": (o.get("text") or "")[:120],
                              "tags": o.get("tags")})
        out[ch] = items[-max_items:]
    return out


def section_team_state(world_path, agent, now):
    data = _load_yaml(Path(world_path) / "team-state.yaml") if world_path else None
    core_present = isinstance(data, dict)
    if not core_present:
        data = {}
    #  sharding: overlay per-agent row files (rows win newest-wins).
    try:
        from _team_state import compose_state
        data = compose_state(data, Path(world_path)) if world_path else data
    except Exception:
        pass
    if not core_present and not (data.get("agent_status") or {}):
        return {"present": False}
    status = data.get("agent_status") or {}
    agents = {}
    for name, s in status.items():
        if not isinstance(s, dict):
            continue
        dt = _parse_ts(s.get("last_active"))
        infl = s.get("in_flight")
        agents[name] = {
            "last_active": s.get("last_active"),
            "age_min": round((now - dt).total_seconds() / 60) if dt else None,
            "current_focus": s.get("current_focus"),
            "in_flight": ({"goal_id": infl.get("goal_id"), "phase": infl.get("phase"),
                           "title": (infl.get("title") or "")[:50]}
                          if isinstance(infl, dict) else None),
            "is_self": name == agent,
        }
    return {"present": True, "agents": agents}


def section_journal(agent_path, max_items):
    if not agent_path:
        return []
    entries = list(_load_jsonl(Path(agent_path) / "journal.jsonl"))
    return [{"date": e.get("date"), "type": e.get("type"),
             "summary": (e.get("summary") or "")[:120]}
            for e in entries[-max_items:]]


# ----------------------------------------------------------------------------
# Assembly + rendering
# ----------------------------------------------------------------------------
def build_digest(agent, world_path, agent_path, now, since_hours, max_commits,
                 max_items, no_commits):
    def guard(fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # pragma: no cover - defensive fail-open
            return {"error": str(e)[:120]}

    return {
        "agent": agent,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "window_hours": since_hours,
        "sections": {
            "handoff": guard(section_handoff, agent_path),
            "own_goals": guard(section_own_goals, agent, world_path, agent_path),
            "recent_commits": guard(section_recent_commits, max_commits, no_commits),
            "board": guard(section_board, world_path, now, since_hours, max_items),
            "team_state": guard(section_team_state, world_path, agent, now),
            "journal": guard(section_journal, agent_path, max_items),
        },
    }


def _age(age_min):
    if age_min is None:
        return "?"
    if age_min < 90:
        return f"{age_min}m"
    return f"{age_min // 60}h{age_min % 60:02d}m"


def render_text(d):
    L = []
    L.append(f"=== session-digest: {d['agent']} @ {d['generated_at']} "
             f"(window {d['window_hours']}h) ===")
    s = d["sections"]

    h = s["handoff"]
    if h.get("present"):
        bits = []
        if h.get("current_focus"):
            bits.append(f"focus={h['current_focus']}")
        if h.get("next_action") or h.get("first_action"):
            bits.append(f"next={h.get('next_action') or h.get('first_action')}")
        if "known_blockers_active" in h:
            bits.append(f"blockers={h['known_blockers_active']}")
        L.append("handoff: " + ("; ".join(str(b) for b in bits) if bits else "present"))
    else:
        L.append("handoff: (none)")

    og = s["own_goals"]
    if "error" in og:
        L.append(f"own goals: (error: {og['error']})")
    else:
        L.append(f"own goals: {len(og['in_progress'])} in-progress, "
                 f"{og['pending_count']} pending")
        for g in og["in_progress"]:
            L.append(f"  IP {g['id']} [{g['asp']}] {g['title']}")

    rc = s["recent_commits"]
    if rc.get("skipped"):
        pass
    elif "error" in rc:
        L.append(f"recent commits: (error: {rc['error']})")
    else:
        L.append(f"recent commits ({len(rc['commits'])}):")
        for c in rc["commits"]:
            L.append(f"  {c}")

    b = s["board"]
    if "error" in b:
        L.append(f"board: (error: {b['error']})")
    else:
        total = sum(len(b.get(ch, [])) for ch in ("coordination", "findings"))
        L.append(f"board ({d['window_hours']}h, {total}):")
        for ch in ("coordination", "findings"):
            for p in b.get(ch, []):
                L.append(f"  [{ch}] {p.get('author')} {p.get('type')}: {p.get('text')}")

    ts = s["team_state"]
    if ts.get("present"):
        L.append("team-state:")
        for name, a in ts["agents"].items():
            star = "*" if a.get("is_self") else ""
            inf = a.get("in_flight")
            inf_s = (f" | in_flight {inf['goal_id']} phase={inf['phase']}"
                     if inf else "")
            focus = a.get("current_focus") or ""
            L.append(f"  {name}{star} {_age(a.get('age_min'))} ago"
                     f"{' | ' + focus if focus else ''}{inf_s}")
    else:
        L.append("team-state: (none)")

    j = s["journal"]
    if isinstance(j, dict) and "error" in j:
        L.append(f"journal: (error: {j['error']})")
    else:
        L.append(f"journal (last {len(j)}):")
        for e in j:
            L.append(f"  {e.get('date')} {e.get('type')}: {e.get('summary')}")

    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Unified session-orientation digest (read-only).")
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT"))
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--since-hours", type=int, default=24)
    ap.add_argument("--max-commits", type=int, default=10)
    ap.add_argument("--max-items", type=int, default=8)
    ap.add_argument("--no-commits", action="store_true")
    ap.add_argument("--world-dir")
    ap.add_argument("--agent-dir")
    ap.add_argument("--now")
    args = ap.parse_args(argv)

    agent = args.agent or "unknown"
    world_path, agent_path = _resolve_dirs(args.agent, args.world_dir, args.agent_dir)
    now = _now(args.now)

    digest = build_digest(agent, world_path, agent_path, now, args.since_hours,
                          args.max_commits, args.max_items, args.no_commits)

    if args.format == "json":
        print(json.dumps(digest, indent=2))
    else:
        print(render_text(digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
