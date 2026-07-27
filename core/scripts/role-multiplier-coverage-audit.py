#!/usr/bin/env python3
"""Audit agent_role_multipliers coverage of the ACTIVE agent roster.

Every ACTIVE agent (a non-retired row in world/team-state.yaml agent_status)
MUST have an entry in meta/goal-selection-strategy.yaml `agent_role_multipliers`.
A missing entry makes goal-selector.py's compute_role_affinity return 0.0 for
EVERY one of that agent's goals — a silent scorer bug that surfaces only as an
anomalous self-abstention pattern.

Canonical incident (g-115-2858, discovered g-115-2884): foxtrot was ABSENT from
agent_role_multipliers for ~2 weeks after the charlie+delta->foxtrot merge
(2026-07-07), so role_affinity=0.0 for all its goals — the 28/29 self-abstention
the g-115-2831 audit flagged. No check caught it until someone noticed the
pattern by hand. This audit is that check.

Two findings:
  MISSING (blocking, exit 2): an ACTIVE agent with no agent_role_multipliers
    entry — the silent role_affinity=0.0 bug.
  STALE (advisory, non-blocking): an agent_role_multipliers entry for an agent
    that is NOT in the active roster (a retired agent's leftover, e.g. charlie /
    delta after the merge). Harmless to scoring but worth pruning.

Output: JSON to stdout with --json; human summary otherwise.

Exit codes:
  0  OK      -- every active agent has a multiplier entry
  2  MISSING -- one or more active agents lack an entry (the bug class)
  1  ERROR   -- bad paths / unreadable config

Wired into /verify-learning Step 2 (framework-config invariants). Also runnable
standalone as a recurring audit. Origin: g-115-2884.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _paths  # noqa: E402  — SSOT for WORLD_DIR / META_DIR resolution
import _team_state  # noqa: E402  — active-roster source (load_rows + _is_retired)

try:
    import yaml  # noqa: E402
except ImportError:  # pragma: no cover
    yaml = None


def active_agents(world_dir: Path) -> set[str]:
    """Non-retired agents from the team-state shard roster.

    load_rows() returns per-agent shard rows; _is_retired() applies the
    tombstone + self-healing-revival logic (a heartbeat newer than retired_at
    re-enters the roster). Mirrors compose_agent_status's retired-drop so this
    audit's "active" set matches what team-state-read.sh --field agent_status
    reports.
    """
    rows = _team_state.load_rows(world_dir)
    return {name for name, entry in rows.items()
            if not _team_state._is_retired(entry)}


def multiplier_keys(meta_dir: Path) -> set[str]:
    """Keys of agent_role_multipliers in meta/goal-selection-strategy.yaml."""
    if yaml is None:
        raise RuntimeError("PyYAML not available")
    strat = meta_dir / "goal-selection-strategy.yaml"
    if not strat.is_file():
        raise FileNotFoundError(f"missing {strat}")
    data = yaml.safe_load(strat.read_text(encoding="utf-8")) or {}
    arm = data.get("agent_role_multipliers") or {}
    if not isinstance(arm, dict):
        raise ValueError("agent_role_multipliers is not a mapping")
    return set(arm.keys())


def audit(world_dir: Path, meta_dir: Path) -> dict:
    # Empty-roster semantics are INTENTIONAL: with zero active agents (bare/CI
    # repo, or an unreadable team-state) `missing` is trivially empty and ok=True
    # (vacuous). This is deliberate — the audit answers "is every ACTIVE agent
    # covered?", and a broken/absent team-state is caught loudly by the many
    # OTHER verify-learning checks that read it, so this never becomes a lone
    # false-assurance. Guarding empty-roster as a hard fail would false-fail on
    # bare repos where team-state legitimately does not exist yet.
    active = active_agents(world_dir)
    mult = multiplier_keys(meta_dir)
    missing = sorted(active - mult)   # active agent with no entry -> role_affinity 0.0 bug
    stale = sorted(mult - active)     # entry for a non-active (retired) agent -> advisory
    return {
        "active_agents": sorted(active),
        "multiplier_keys": sorted(mult),
        "missing": missing,
        "stale": stale,
        "ok": len(missing) == 0,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    args = ap.parse_args(argv)

    try:
        result = audit(_paths.WORLD_DIR, _paths.META_DIR)
    except Exception as e:  # noqa: BLE001 — any resolution/parse failure is ERROR
        if args.json:
            print(json.dumps({"error": str(e), "ok": False}))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result))
    else:
        if result["missing"]:
            print(f"MISSING ({len(result['missing'])}): active agents with NO "
                  f"agent_role_multipliers entry (role_affinity=0.0 silent bug): "
                  f"{result['missing']}", file=sys.stderr)
        else:
            print(f"OK: all {len(result['active_agents'])} active agents have "
                  f"agent_role_multipliers entries "
                  f"({', '.join(result['active_agents'])})")
        if result["stale"]:
            print(f"ADVISORY: agent_role_multipliers entries for non-active "
                  f"(retired?) agents — prune when convenient: {result['stale']}")

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
