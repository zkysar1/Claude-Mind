"""Agents holding an UNPROCESSED loop-stall warning row ().

WHY THIS EXISTS AS A MODULE rather than a heredoc inside stop-hook-analyze.sh:
the predicate needs a regression test, and AGENTS_PARENT_DIR is an unconditional
sync-critical constant in _paths.sh (line 141) that a test cannot override — so a
shell-embedded scanner can only be exercised by planting a fake agent dir in the
LIVE agents/ tree. That is exactly the fixture-pollution class the roster tripwire
in test_capability_route_gate.py detects, so the test would create the defect it
is meant to guard against. A module takes `agents_root` as a parameter and is
testable against tmp_path with no live-tree writes.

THE GAP IT CLOSES. stall-goal-filer.py skips a rate-limited entry WITHOUT setting
`goal_filed`, so the row stays unprocessed. `stop-hook-analyze.sh --and-file` used
to invoke the filer only for agents named by a WROTE_AGENT marker — agents that
got a NEW warning on THIS run — so if the agent never stalls again the row is
never revisited. reclaim-routed-work.md rule 7: a reclaim predicate must not be
narrower than the gate that creates the population.

This widens REACH only. The filer's per-agent 24h rate limit is untouched; a
still-rate-limited agent is re-invoked and skips again, which is idempotent.
"""
from __future__ import annotations

import glob
import json
import os
import sys


def backlog_agents(agents_root):
    """Sorted agent names holding >=1 warning row without a truthy `goal_filed`.

    Fail-open by contract: an unreadable file or a malformed line is skipped
    rather than raised. This feeds a best-effort sweep — a scan that dies on one
    corrupt row would strand every other agent's backlog, which is strictly worse
    than missing the corrupt one.
    """
    found = []
    pattern = os.path.join(agents_root, "*", "session", "loop-stall-warnings.jsonl")
    for path in sorted(glob.glob(pattern)):
        agent = path.split(os.sep)[-3]
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if not row.get("goal_filed"):
                        found.append(agent)
                        break
        except OSError:
            continue
    return found


def main(argv):
    if len(argv) != 2:
        print("usage: stall_backlog_agents.py <agents_root>", file=sys.stderr)
        return 2
    for agent in backlog_agents(argv[1]):
        print(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
