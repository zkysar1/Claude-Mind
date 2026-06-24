#!/usr/bin/env python3
"""wm-contamination-check.py - SessionStart cross-agent WM contamination detector.

REMEDIAL companion to the PREVENTIVE sid-collision-check.sh defense. A binding
desync during /stop (or a post-autocompact SID rotation) can let a
SessionStart:compact event restore ONE agent's loop_state into ANOTHER agent's
working-memory.yaml -- the observed failure mode was an entire foreign
loop_state (dozens of another agent's completed-goal IDs) landing in the bound
agent's WM and PERSISTING even after the binding itself was corrected. The SID
collision gate defends the binding-write boundary; it does NOT scrub a WM that
was already cross-written. This detector closes that residual gap: at every
SessionStart, it reads the bound agent's WM, attributes the goal IDs it claims
to have completed this session, and QUARANTINES the WM (moving it aside +
writing a fresh template) when those goals overwhelmingly belong to a DIFFERENT
agent.

DESIGN: threshold + ownership, NOT a naive per-goal rule
--------------------------------------------------------
A per-goal "any foreign goal -> quarantine" rule is WRONG here: every agent's
WM legitimately carries RECURRING shared-queue goals whose `completed_by`
records the LAST completer, who is routinely a different agent (e.g. a recurring
world goal completed_by another agent appears in this agent's WM all the time --
verified: a live recurring world goal carried completed_by a non-bound agent).
That is normal shared-queue cadence, not contamination. So the detector:

  1. Excludes RECURRING goals from the ownership tally entirely (their
     completed_by is not an ownership signal -- it is a who-ran-it-last signal).
  2. Requires a DOMINANT foreign block: a single other agent must account for
     the large majority of the attributable (non-recurring, owner-known) goals,
     AND the bound agent must be near-absent from them. A few foreign goals from
     legitimate cross-agent collaboration never dominate.
  3. Applies a board claim-history GUARD (the critical false-positive defense):
     if the bound agent has RECENT coordination-board posts (claim/complete/
     release/handoff/...) for most of the suspected-foreign goals, it really did
     work on them (legit collaboration: one agent claims, another finishes in a
     handoff) -> NOT contamination.

All three conditions must hold to quarantine. The bias is deliberately toward
FALSE-NEGATIVE over false-positive: a missed contamination self-corrects next
session; a false quarantine destroys a legitimate WM.

FAIL-OPEN: this runs on the SessionStart critical path. Any error -> report
status + exit 0. It NEVER blocks session start and NEVER raises.

SELF-CONTAINED: reads JSONL/YAML directly (the daemon may not be up at
SessionStart). The only intra-repo import is the pure, side-effect-free
session-binding resolver.

Usage:
  py -3 wm-contamination-check.py --sid <SID> [--apply] [--json]
  py -3 wm-contamination-check.py --agent <name> [--apply] [--json]   # tests
Options:
  --sid <SID>           Resolve the bound agent from this session id.
  --agent <name>        Explicit bound agent (overrides --sid; for tests).
  --project-root <dir>  Repo root (default: this file's ../..; for tests).
  --world-dir <dir>     World path override (default: read from the agent's
                        local-paths.conf; for tests).
  --apply               Actually quarantine on detection (default: report only).
  --json                Emit a machine-readable JSON result on stdout.
                        Without --json: silent on clean, loud block on detect.

Tunables (env, all have conservative defaults):
  WM_CONTAM_MIN_FOREIGN     absolute floor on the foreign block       (default 5)
  WM_CONTAM_MIN_ATTRIB      minimum attributable goals to judge       (default 8)
  WM_CONTAM_DOMINANCE       foreign fraction of attributable required (default 0.75)
  WM_CONTAM_BOARD_DAYS      board claim-history lookback window, days (default 7)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# ARRAY_SLOTS / MAP_SLOTS mirror wm.py's structural constants (the authoritative
# source). Kept in sync by hand -- they change rarely. slot_types itself is NOT
# copied: it is read from core/config/memory-pipeline.yaml below, exactly as
# wm.py::_default_wm_data does. test_wm_contamination_check validates the fresh
# template is parseable + empty, so drift here fails loud.
_ARRAY_SLOTS = {
    "knowledge_debt", "known_blockers", "micro_hypotheses",
    "recent_violations", "sensory_buffer", "conclusions",
}
_MAP_SLOTS = {
    "active_context": {"summary": None, "experience_refs": [], "retrieval_manifest": None},
    "archived_context": {"summary": None, "experience_refs": []},
}
# Fallback only -- mirrors wm.py DEFAULT_SLOT_TYPES for the rare config-unreadable
# case. The live path reads memory-pipeline.yaml (single source of truth).
_DEFAULT_SLOT_TYPES = [
    "active_constraints", "active_context", "active_hypothesis", "active_strategy",
    "archived_context", "cross_domain_transfer", "domain_data",
    "ephemeral_observation", "knowledge_debt", "known_blockers",
    "micro_hypotheses", "pending_resolutions", "recent_violations",
    "sensory_buffer", "session_goal", "conclusions",
]

# Board post types that prove the bound agent was legitimately involved with a
# goal (the false-positive guard). Any of these, authored by the bound agent and
# tagging the goal within the lookback window, exonerates that goal.
_INVOLVEMENT_TYPES = {
    "claim", "complete", "release", "handoff", "blocked", "escalation",
    "review-request", "execution-feedback",
}


def _now() -> datetime:
    return datetime.now()


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _resolve_agent(sid: str, agent: str, project_root: Path) -> str:
    if agent:
        return agent.strip()
    if not sid:
        return ""
    try:
        from _session_binding import resolve_agent_name
        return resolve_agent_name(sid, project_root) or ""
    except Exception:
        return ""


def _read_world_dir(agent: str, project_root: Path, override: str) -> Path | None:
    if override:
        return Path(override)
    conf = project_root / "agents" / agent / "local-paths.conf"
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key.strip() == "WORLD_PATH":
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                return Path(val)
    except Exception:
        return None
    return None


def _slot_types(project_root: Path) -> list:
    """Read slot_types from memory-pipeline.yaml (single source of truth);
    fall back to the wm.py-mirrored default only if config is unreadable."""
    cfg = _load_yaml(project_root / "core" / "config" / "memory-pipeline.yaml")
    if isinstance(cfg, dict):
        st = cfg.get("working_memory", {}).get("slot_types")
        if isinstance(st, list) and st:
            return st
    return list(_DEFAULT_SLOT_TYPES)


def _fresh_wm(project_root: Path) -> dict:
    """Canonical empty-WM dict -- mirror of wm.py::_default_wm_data, with
    session_start stamped now so the loop treats it as a fresh session."""
    slots, slot_meta = {}, {}
    for st in _slot_types(project_root):
        if st in _ARRAY_SLOTS:
            slots[st] = []
        elif st in _MAP_SLOTS:
            slots[st] = dict(_MAP_SLOTS[st])
        else:
            slots[st] = None
        slot_meta[st] = {"updated_at": None, "accessed_at": None, "update_count": 0}
    return {
        "encoding_queue": [],
        "session_id": None,
        "session_start": _now().strftime("%Y-%m-%dT%H:%M:%S"),
        "goals_completed_this_session": [],
        "aspiration_touched_last": "",
        "last_goal_category": "",
        "slots": slots,
        "slot_meta": slot_meta,
    }


def _collect_candidate_goal_ids(wm: dict) -> list:
    """Pull every goal ID the WM claims to have completed this session, from the
    two authoritative lists plus the per-goal routine-streak map. Aspiration IDs
    (asp-*) and the `touched` list are intentionally excluded."""
    ids: list = []
    seen = set()

    def _add(gid):
        if isinstance(gid, str) and gid.startswith("g-") and gid not in seen:
            seen.add(gid)
            ids.append(gid)

    for entry in (wm.get("goals_completed_this_session") or []):
        if isinstance(entry, dict):
            _add(entry.get("goal_id"))
        elif isinstance(entry, str):
            _add(entry)

    slots = wm.get("slots") or {}
    loop_state = slots.get("loop_state") or {}
    if isinstance(loop_state, dict):
        for gid in (loop_state.get("counted_goals_this_session") or []):
            _add(gid)
        rs = loop_state.get("routine_streaks") or {}
        if isinstance(rs, dict):
            for gid in rs.keys():
                _add(gid)
    return ids


def _build_ownership_index(world_dir: Path | None, agent_dir: Path) -> dict:
    """Map goal_id -> {completed_by, claimed_by, recurring} from every reachable
    aspirations store. Archive entries (terminal) override live ones."""
    index: dict = {}
    files = []
    if world_dir is not None:
        files.append(world_dir / "aspirations.jsonl")
        files.append(world_dir / "aspirations-archive.jsonl")
    files.append(agent_dir / "aspirations.jsonl")
    files.append(agent_dir / "aspirations-archive.jsonl")

    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except Exception:
                        continue
                    for g in asp.get("goals", []):
                        gid = g.get("id")
                        if not gid:
                            continue
                        index[gid] = {
                            "completed_by": g.get("completed_by"),
                            "claimed_by": g.get("claimed_by"),
                            "recurring": bool(g.get("recurring", False)),
                        }
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return index


def _board_involved_goals(world_dir: Path | None, agent: str, goal_ids: set,
                          since_days: int) -> set:
    """Subset of goal_ids the bound agent has a recent coordination-board post
    for (claim/complete/release/handoff/...). The false-positive guard."""
    if world_dir is None or not goal_ids:
        return set()
    board = world_dir / "board" / "coordination.jsonl"
    cutoff = _now() - timedelta(days=since_days)
    involved: set = set()
    try:
        with open(board, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("author") != agent:
                    continue
                if msg.get("type") not in _INVOLVEMENT_TYPES:
                    continue
                ts = msg.get("timestamp", "")
                try:
                    when = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                    if when < cutoff:
                        continue
                except Exception:
                    pass  # undated/odd -> do not exclude on age (conservative)
                tags = msg.get("tags") or []
                text = msg.get("text") or ""
                for gid in goal_ids:
                    if gid in involved:
                        continue
                    if gid in tags or gid in text:
                        involved.add(gid)
    except FileNotFoundError:
        return set()
    except Exception:
        return set()
    return involved


def analyze(wm: dict, world_dir: Path | None, agent_dir: Path, agent: str,
            project_root: Path, *, min_foreign: int, min_attrib: int,
            dominance: float, board_days: int) -> dict:
    """Pure detection core (no filesystem mutation). Returns a result dict."""
    candidates = _collect_candidate_goal_ids(wm)
    index = _build_ownership_index(world_dir, agent_dir)

    own = []
    foreign: dict = {}          # agent -> [goal_id]
    recurring_skipped = []
    unresolved = []
    unattributed = []

    for gid in candidates:
        rec = index.get(gid)
        if rec is None:
            unresolved.append(gid)
            continue
        if rec["recurring"]:
            recurring_skipped.append(gid)
            continue
        owner = rec["completed_by"] or rec["claimed_by"]
        if not owner:
            unattributed.append(gid)
            continue
        if owner == agent:
            own.append(gid)
        else:
            foreign.setdefault(owner, []).append(gid)

    own_count = len(own)
    dominant_source = None
    dominant_goals: list = []
    if foreign:
        dominant_source = max(foreign, key=lambda a: len(foreign[a]))
        dominant_goals = foreign[dominant_source]
    foreign_count = len(dominant_goals)
    attributable = own_count + foreign_count

    # Board guard runs only when the cheap thresholds already implicate a block
    # (the board scan is the expensive read; skip it in the common clean case).
    board_implicated = (
        foreign_count >= min_foreign
        and attributable >= min_attrib
        and foreign_count >= dominance * attributable
    )
    board_involved: set = set()
    if board_implicated:
        board_involved = _board_involved_goals(
            world_dir, agent, set(dominant_goals), board_days)

    board_clears = len(board_involved) >= math.ceil(foreign_count / 2) if foreign_count else False
    is_contaminated = bool(board_implicated and not board_clears)

    return {
        "agent": agent,
        "candidate_count": len(candidates),
        "own_count": own_count,
        "foreign_counts": {a: len(v) for a, v in foreign.items()},
        "dominant_source": dominant_source,
        "dominant_goal_count": foreign_count,
        "dominant_goal_ids": dominant_goals[:50],
        "attributable": attributable,
        "recurring_skipped": len(recurring_skipped),
        "unresolved": len(unresolved),
        "unattributed": len(unattributed),
        "board_involved_count": len(board_involved),
        "board_implicated": board_implicated,
        "board_clears": board_clears,
        "is_contaminated": is_contaminated,
    }


def quarantine(wm_path: Path, project_root: Path) -> tuple[bool, str]:
    """Move the contaminated WM aside and write a fresh template in its place.
    Atomic (os.replace). Returns (ok, quarantine_path_or_error)."""
    try:
        ts = _now().strftime("%Y%m%dT%H%M%S")
        wm_dir = wm_path.parent
        q_path = wm_dir / f"working-memory-quarantined-{ts}.yaml"
        # Avoid clobbering a same-second prior quarantine.
        suffix = 0
        while q_path.exists():
            suffix += 1
            q_path = wm_dir / f"working-memory-quarantined-{ts}-{suffix}.yaml"
        os.replace(str(wm_path), str(q_path))

        import yaml
        fresh = _fresh_wm(project_root)
        tmp = wm_dir / f".working-memory-fresh-{ts}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(fresh, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(str(tmp), str(wm_path))
        return True, str(q_path)
    except Exception as e:
        return False, f"quarantine-failed: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="SessionStart cross-agent WM contamination detector")
    ap.add_argument("--sid", default="")
    ap.add_argument("--agent", default="")
    ap.add_argument("--project-root", default="")
    ap.add_argument("--world-dir", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    project_root = Path(args.project_root) if args.project_root else SCRIPT_DIR.parent.parent

    def _emit(result: dict) -> int:
        if args.json:
            print(json.dumps(result))
        elif result.get("is_contaminated"):
            src = result.get("dominant_source")
            n = result.get("dominant_goal_count")
            ids = ", ".join(result.get("dominant_goal_ids", [])[:12])
            print("=" * 64)
            print("WM CONTAMINATION DETECTED + QUARANTINED")
            print(f"  bound agent : {result.get('agent')}")
            print(f"  source agent: {src} ({n} of its goals found in this WM)")
            print(f"  own goals   : {result.get('own_count')}")
            print(f"  goal ids    : {ids}{' ...' if n and n > 12 else ''}")
            if result.get("action") == "quarantined":
                print(f"  quarantined : {result.get('quarantine_path')}")
                print("  a fresh working-memory.yaml was written; the loop will re-init it.")
            elif result.get("action") == "would-quarantine":
                print("  (report-only: re-run with --apply to quarantine)")
            else:
                print(f"  WARNING: quarantine FAILED: {result.get('quarantine_error')}")
            print("=" * 64)
        return 0

    try:
        agent = _resolve_agent(args.sid, args.agent, project_root)
        if not agent:
            return _emit({"status": "no-bound-agent", "is_contaminated": False})

        agent_dir = project_root / "agents" / agent
        wm_path = agent_dir / "session" / "working-memory.yaml"
        if not wm_path.exists():
            return _emit({"status": "no-wm", "agent": agent, "is_contaminated": False})

        wm = _load_yaml(wm_path)
        if not isinstance(wm, dict):
            return _emit({"status": "wm-unreadable", "agent": agent, "is_contaminated": False})

        world_dir = _read_world_dir(agent, project_root, args.world_dir)

        result = analyze(
            wm, world_dir, agent_dir, agent, project_root,
            min_foreign=int(os.environ.get("WM_CONTAM_MIN_FOREIGN", "5")),
            min_attrib=int(os.environ.get("WM_CONTAM_MIN_ATTRIB", "8")),
            dominance=float(os.environ.get("WM_CONTAM_DOMINANCE", "0.75")),
            board_days=int(os.environ.get("WM_CONTAM_BOARD_DAYS", "7")),
        )
        result["status"] = "checked"

        if result["is_contaminated"]:
            if args.apply:
                ok, info = quarantine(wm_path, project_root)
                result["action"] = "quarantined" if ok else "quarantine-error"
                if ok:
                    result["quarantine_path"] = info
                else:
                    result["quarantine_error"] = info
            else:
                result["action"] = "would-quarantine"
        else:
            result["action"] = "none"

        return _emit(result)
    except Exception as e:
        # Absolute fail-open: never block session start.
        return _emit({"status": "error", "error": str(e), "is_contaminated": False})


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
