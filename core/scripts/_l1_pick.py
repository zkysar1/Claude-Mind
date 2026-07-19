"""L1 pick-log SSOT (S9 telemetry) — shared by CLI tree.py and daemon
tree_write.py (g-115-1943).

Captures the implicit-taxonomy decision every tree write makes: which L1
(depth==1 domain) the written node lands under. Append-only JSONL at
<meta>/l1-pick-log.jsonl. Consumed by l1-skew-check.py,
l1-emergence-detector.py, and /fresh-eyes-tree (S9 pick-rate evidence).

Extracted from core/scripts/tree.py when g-115-1943 found the daemon
tree-write endpoints (mind_api/src/world/tree_write.py) had deferred this
telemetry at daemonization — the log went silent 2026-05-28 while ~6 weeks
of tree writes flowed through the daemon. Same SSOT shape as
_competence.py / _team_state.py: pure functions, paths as args, no _paths
import (daemon-import-safe per .claude/rules/path-resolution.md).

FAIL-OPEN CONTRACT: a logging error MUST NOT block the tree write that just
succeeded. Every entry point swallows exceptions to a stderr WARN.
"""
import json
import sys
from datetime import datetime
from pathlib import Path


def get_l1_for_node(nodes, key):
    """Walk parent chain to find the L1 (depth==1) ancestor.

    Returns the L1 key. For a node that IS L1, returns itself. Returns None
    if the chain is malformed or breaks before reaching depth==1.
    """
    visited = set()
    current = key
    while current is not None and current not in visited:
        visited.add(current)
        node = nodes.get(current)
        if not node:
            return None
        depth = node.get("depth")
        if depth == 1:
            return current
        if depth == 0:
            return None
        current = node.get("parent")
    return None


def append_l1_pick_log(meta_dir, target_node, l1, decision_type,
                       source=None, reason=None, agent=None, session_id=None):
    """Append one L1 pick log entry to <meta_dir>/l1-pick-log.jsonl. Fail-open."""
    try:
        log_path = Path(meta_dir) / "l1-pick-log.jsonl"
        entry = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": agent or None,
            "session_id": session_id or None,
            "target_node": target_node,
            "l1": l1,
            "decision_type": decision_type,
            "source": source,
            "reason": reason,
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(log_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[l1-pick-log] WARN: " + str(e), file=sys.stderr)


def log_l1_pick(nodes, meta_dir, key, decision_type,
                source=None, reason=None, agent=None, session_id=None):
    """Resolve L1 for a just-written node from the given nodes map and append
    a pick-log entry. Fail-open — never raises.

    The caller supplies the nodes map it already holds (daemon: the post-write
    in-lock tree; CLI wrapper: a fresh read) — this function does no I/O
    beyond the append.
    """
    try:
        l1 = get_l1_for_node(nodes, key)
        if l1 is None:
            l1 = "_orphan"
        append_l1_pick_log(meta_dir, key, l1, decision_type,
                           source=source, reason=reason,
                           agent=agent, session_id=session_id)
    except Exception as e:
        print("[l1-pick-log] WARN: " + str(e), file=sys.stderr)
