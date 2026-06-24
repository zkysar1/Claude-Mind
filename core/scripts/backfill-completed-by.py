#!/usr/bin/env python3
"""One-time idempotent backfill of completed_by on historical completed world goals.

Goal g-115-1567. Background: only ~11% (180/1621) of completed world goals carried
completed_by — the g-115-1562 write-path fix stamps it on the status->completed
transition GOING FORWARD only and does not repair historical records. The
cross_queue graduation count (g-115-1560) reads status==completed AND
completed_by==self, so pre-fix completions were retroactively uncounted toward
curriculum graduation (delta in particular).

Reconstruction source: per-agent journal.jsonl (goal_id + completion outcome,
dir-attributed) and experience.jsonl (goal_id/source_goal + explicit agent field).
Both are private to the completing agent and goal-id-keyed. A goal is stamped ONLY
when EXACTLY ONE agent's records claim completing it (a unique single-agent match);
ambiguous (>1 agent) and undeterminable (0 sources) goals are LEFT UNSTAMPED —
never guess (matches the goal's explicit "only where determinable" requirement).

Reliability: validated against the already-stamped cohort — of the goals that both
had a native completed_by AND appeared in the reconstruction map, agreement was
100% (44/44, 0 disagreements). The changelog.jsonl was rejected as a source: it is
file-granular (no goal_id), summary-empty, and 55% attributed to "system"
(the daemon), so it cannot isolate which goal an agent's aspirations.jsonl edit
completed.

Idempotent: stamps ONLY goals whose completed_by is currently falsy; re-running is
a no-op. Contention-safe + cache-consistent: writes through
_fileops.locked_modify_jsonl, which holds the same <path>.lock the daemon's
file_locks uses (no lost-update race) and bumps mtime so the daemon's mtime-keyed
jsonl_cache auto-reloads. One call = one history snapshot + one atomic write.

Usage:
  py -3 backfill-completed-by.py <WORLD_DIR> <AGENTS_ROOT>            # dry-run (default)
  py -3 backfill-completed-by.py <WORLD_DIR> <AGENTS_ROOT> --apply   # apply the backfill
"""
import sys
import os
import json
import collections
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import _fileops  # noqa: E402

AGENTS = ["alpha", "bravo", "charlie", "delta", "echo", "zeta"]
COMPLETION_OUTCOMES = {"deep", "routine"}


def build_recon(agents_root):
    """goal_id -> {agent: [source-tags]} from journals + experience traces."""
    recon = collections.defaultdict(lambda: collections.defaultdict(list))

    def add(gid, agent, source):
        if gid and agent:
            recon[gid][agent].append(source)

    for agent in AGENTS:
        adir = os.path.join(agents_root, agent)
        jp = os.path.join(adir, "journal.jsonl")
        if os.path.exists(jp):
            with open(jp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("goal_id") and e.get("outcome") in COMPLETION_OUTCOMES:
                        add(e["goal_id"], agent, "journal")
        ep = os.path.join(adir, "experience.jsonl")
        if os.path.exists(ep):
            with open(ep, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    gid = e.get("goal_id") or e.get("source_goal")
                    if gid:
                        add(gid, e.get("agent") or agent, "experience:" + agent)
    return recon


def unique_attributions(recon):
    """goal_id -> agent, only where exactly one agent claims it."""
    out = {}
    for gid, agents in recon.items():
        if len(agents) == 1:
            out[gid] = next(iter(agents))
    return out


def main():
    if len(sys.argv) < 3:
        print("usage: backfill-completed-by.py <WORLD_DIR> <AGENTS_ROOT> [--apply]",
              file=sys.stderr)
        return 2
    world_dir = sys.argv[1]
    agents_root = sys.argv[2]
    apply = "--apply" in sys.argv[3:]

    asp_path = Path(world_dir) / "aspirations.jsonl"
    recon = build_recon(agents_root)
    attrib = unique_attributions(recon)

    # Compute the plan against current on-disk state (pre-lock preview).
    plan = {}          # gid -> agent (missing + uniquely attributable)
    stamped = 0
    completed = 0
    malformed = []     # goals with a non-agent completed_by value
    with open(asp_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                asp = json.loads(line)
            except Exception:
                continue
            for g in asp.get("goals", []) or []:
                if g.get("status") != "completed":
                    continue
                completed += 1
                cb = g.get("completed_by")
                if cb:
                    stamped += 1
                    if cb not in AGENTS and cb not in ("world", "omni", "user"):
                        malformed.append((g.get("id"), cb))
                    continue
                gid = g.get("id")
                if gid in attrib:
                    plan[gid] = attrib[gid]

    by_agent = collections.Counter(plan.values())
    print("=== backfill-completed-by (g-115-1567) ===")
    print(f"mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"world completed goals: {completed}")
    print(f"already stamped: {stamped}")
    print(f"missing + uniquely attributable (will stamp): {len(plan)}")
    print(f"by agent: {dict(by_agent)}")
    if malformed:
        print(f"NOTE malformed completed_by values (NOT touched, separate fix): {malformed}")

    if not apply:
        print("dry-run: no write performed. Re-run with --apply to stamp.")
        return 0

    # Apply: single locked RMW. modifier_fn re-checks each goal's CURRENT
    # completed_by inside the lock so any concurrent daemon stamp that landed
    # between plan-build and write is respected (idempotent + race-safe).
    stamped_count = [0]

    def modifier(items):
        for asp in items:
            for g in asp.get("goals", []) or []:
                if g.get("status") != "completed":
                    continue
                if g.get("completed_by"):
                    continue
                gid = g.get("id")
                if gid in attrib:
                    g["completed_by"] = attrib[gid]
                    g["completed_by_backfilled"] = True
                    stamped_count[0] += 1
        return items

    _fileops.locked_modify_jsonl(asp_path, modifier)
    print(f"APPLIED: stamped {stamped_count[0]} goals with reconstructed completed_by "
          f"(+ completed_by_backfilled=true provenance marker)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
