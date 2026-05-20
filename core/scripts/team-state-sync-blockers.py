#!/usr/bin/env python3
"""Purge stale entries from team-state.yaml critical_blockers.

team-state.critical_blockers is a snapshot that accumulates entries at
blocker-creation time but has no automatic cleanup on goal completion.
Result: goals that completed weeks ago still appear as "critical blockers"
and both agents read the stale list on prime, inheriting the lie.

This script reads each critical_blockers[].goal_id, looks up the goal's
status across every candidate aspiration source (see _candidate_sources for
the authoritative list), and removes any entry whose status is one of
{completed, archived, skipped, expired}. Pending / in-progress / blocked
entries stay — they are legitimately still blocked.

Invoked by /prime Phase 5.45 (immediately before Phase 5.5 team-state-read
so downstream readers see a fresh snapshot) and by aspirations-state-update
Step 3.5 (so writes also re-sweep). Idempotent.

Single source of truth for "what counts as terminal for purge purposes":
TERMINAL_STATUSES + the asp_archived promotion in load_goal_statuses.
DO NOT add a fallback that silently leaves unknown-status entries in place —
that would recreate the zombie-blocker class this script exists to kill.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR
from _fileops import locked_modify_yaml

TERMINAL_STATUSES = {"completed", "archived", "skipped", "expired"}
TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"


def _candidate_sources():
    """Return every JSONL file that might contain a goal for status lookup.

    Critical: MUST include aspirations-archive.jsonl, otherwise an archived
    aspiration's goals vanish from the status map and their critical_blockers
    entries never get purged (zombie blockers survive archival). MUST also
    include agent-local queues because agents can file per-agent goals that
    appear in team-state.critical_blockers.
    """
    sources = [
        WORLD_DIR / "aspirations.jsonl",
        WORLD_DIR / "aspirations-archive.jsonl",
    ]
    agent = os.environ.get("MIND_AGENT", "").strip()
    if agent:
        from _paths import agent_dir as _agent_dir
        sources.extend([
            _agent_dir(agent) / "aspirations.jsonl",
            _agent_dir(agent) / "aspirations-archive.jsonl",
        ])
    return sources


def load_goal_statuses():
    """Return {goal_id: status} map from all candidate aspiration files."""
    statuses = {}
    for path in _candidate_sources():
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                asp_archived = asp.get("archived") is True or asp.get("status") == "archived"
                for g in asp.get("goals", []) or []:
                    gid = g.get("id")
                    if not gid:
                        continue
                    status = g.get("status") or "unknown"
                    if asp_archived and status not in TERMINAL_STATUSES:
                        status = "archived"
                    statuses[gid] = status
    return statuses


def purge(state, statuses):
    """Mutate state in place. Return (kept, removed) lists for reporting."""
    blockers = state.get("critical_blockers") or []
    kept, removed = [], []
    for entry in blockers:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        gid = entry.get("goal_id")
        status = statuses.get(gid)
        if status in TERMINAL_STATUSES:
            removed.append((gid, status))
        else:
            kept.append(entry)
    state["critical_blockers"] = kept
    return kept, removed


def main():
    statuses = load_goal_statuses()

    removed_all = []

    def _modifier(state):
        _, removed = purge(state, statuses)
        removed_all.extend(removed)
        return state

    if not TEAM_STATE_PATH.exists():
        print("team-state-sync-blockers: team-state.yaml missing, nothing to sync", file=sys.stderr)
        return 0

    locked_modify_yaml(TEAM_STATE_PATH, _modifier, initial={})

    if removed_all:
        for gid, status in removed_all:
            print(f"purged {gid} (status={status})")
    else:
        print("critical_blockers: all entries still blocked — no purge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
