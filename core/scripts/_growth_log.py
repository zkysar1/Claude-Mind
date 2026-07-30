"""tree_growth_log SSOT — shared by CLI tree.py and daemon tree_write.py
(g-115-3210).

`tree_growth_log` in `world/knowledge/tree/_tree.yaml` is the tree's
structural-op history. `/fresh-eyes-tree` Phase 2.3 pastes its tail into every
tree-review briefing as the evidence of what changed structurally.

WHY THIS MODULE EXISTS — the measured finding, so nobody re-derives it:

    The log held 8 rows, ALL `op: DECOMPOSE`, ALL dated 2026-04-04, for 3.7
    months. The obvious reading is "a refactor orphaned the append" (the same
    shape as g-115-1943, where daemonization silently dropped the SIBLING
    l1-pick-log for ~6 weeks). That reading is WRONG here, and four
    independent signals say so:

      1. grep across core/, mind_api/, .claude/ finds exactly TWO writers —
         l1-domain-add.py and l1-domain-rename.py — and both write only
         L1_ADD / L1_RENAME.
      2. tree.py, tree_write.py, _l1_pick.py and mind_api/src/ contained ZERO
         references to the log.
      3. The 8 frozen rows are DECOMPOSE, which neither writer emits — so they
         cannot have come from the script path at all.
      4. Their `reason` strings ("backlog-cleanup: 459 lines", "manual
         decompose: 338 lines") are hand-authored prose, not generated text.

    So writes never STOPPED. There was never a script writer for DECOMPOSE.
    The 8 rows are the residue of one 2026-04-04 session that followed the
    honor-system instruction in `.claude/skills/tree/SKILL.md` ("Append to
    tree_growth_log ..." — restated at 9 separate sites, enforced at none).
    Every session since simply did not. That is the drift-invited class
    (fresh-eyes-code 3.7): an LLM-discretionary step drifts, a bash-gated one
    does not. This module is the bash gate.

WHY THE DETECTION IS RECOGNITION, NOT INFERENCE. There is no `--decompose`
primitive to hook. But `/tree decompose` writes through ONE `--batch` call
whose ops are, by construction, `set <K> node_type interior` + N `add-child`
on that same `<K>` (tree/SKILL.md steps 5-8). A batch that flips a node to
interior while giving it children IS a decompose — that is the operation's own
signature, not a heuristic about it. Step 9 of that same prose block said
"Append to tree_growth_log: {op: DECOMPOSE, node, children, date, reason}";
this module is that step, moved from prose into the writer.

NOT BACKFILLED, DELIBERATELY. The 2026-04-04 → fix-date window stays visibly
unrecorded so later reviews can see it is a gap rather than a quiet period
(g-115-3210 item 4). Rows written here carry a `reason` that names the writer,
so script-recorded rows are always distinguishable from the 8 hand-written
ones.

SSOT SHAPE mirrors `_l1_pick.py` / `_competence.py` / `_team_state.py`: pure
functions, everything passed as args, NO `_paths` import (daemon-import-safe
per `.claude/rules/path-resolution.md`). Both write paths call the SAME
function, which is the structural reason this log cannot go silent on one path
while working on the other — the exact two-implementation split that hid the
l1-pick-log outage.

FAIL-OPEN CONTRACT: a logging error MUST NOT block the tree write that just
succeeded. Every entry point swallows to a stderr WARN and returns 0.

MERGE NOTE: `coordination_merge._tree_growth_log_union` dedups rows by the
identity tuple `(op, node, date)`. Two decomposes of the same node on the same
day therefore merge to one row. That is accepted, not overlooked — a node
decomposed twice in one day is pathological, and order-preserving union across
boxes matters more than that edge.
"""
import sys

__all__ = [
    "decompose_rows",
    "reparent_row",
    "prune_rows",
    "append_rows",
    "record_batch",
    "record_reparent",
]


def decompose_rows(mutation_ops, today):
    """Rows for every DECOMPOSE recognizable in a batch's mutation ops.

    A parent qualifies when the SAME batch both (a) sets its `node_type` to
    `interior` and (b) adds at least one child to it. Returns [] when neither
    half is present — an ordinary `--add-child` batch logs nothing, which is
    the deliberate answer to "does ordinary child-add belong in this log?"
    (g-115-3210 item 3: it does not; logging every add would bury the
    structural signal the consumer reads this log FOR).
    """
    interior = {
        o.get("key") for o in mutation_ops
        if o.get("op") == "set" and o.get("field") == "node_type"
        and o.get("value") == "interior" and o.get("key")
    }
    if not interior:
        return []
    kids = {}
    for o in mutation_ops:
        if o.get("op") != "add-child":
            continue
        parent = o.get("key")
        if parent not in interior:
            continue
        child_key = (o.get("child") or {}).get("key")
        if child_key:
            kids.setdefault(parent, []).append(child_key)
    return [
        {
            "op": "DECOMPOSE",
            "node": parent,
            "children": list(children),
            "date": today,
            "reason": "batch decompose: %d children" % len(children),
        }
        for parent, children in sorted(kids.items())
    ]


def reparent_row(node, new_parent, today):
    """Row for a single REPARENT. `--reparent` is a one-op chokepoint."""
    if not node or not new_parent:
        return []
    return [{
        "op": "REPARENT",
        "node": node,
        "children": [],
        "date": today,
        "reason": "reparent -> %s" % new_parent,
    }]


def prune_rows(mutation_ops, today):
    """Rows for every PRUNE (remove-child) in a batch's mutation ops.

    Emitted per removed child, not per parent: a removal is the unit of loss,
    and the orphan gate already refuses a remove whose subtree is non-empty,
    so each row stands for exactly one leaf leaving the tree.
    """
    rows = []
    for o in mutation_ops:
        if o.get("op") != "remove-child":
            continue
        child_key = o.get("child_key")
        parent = o.get("key")
        if not child_key:
            continue
        rows.append({
            "op": "PRUNE",
            "node": child_key,
            "children": [],
            "date": today,
            "reason": "removed from %s" % (parent or "<unknown>"),
        })
    return rows


def append_rows(tree, rows):
    """Append rows to `tree['tree_growth_log']` in place. Returns count."""
    if not rows:
        return 0
    log = tree.get("tree_growth_log") or []
    log.extend(rows)
    tree["tree_growth_log"] = log
    return len(rows)


def _warn(exc, what):
    print("WARN[_growth_log]: %s append failed (%s: %s) — tree write "
          "unaffected" % (what, type(exc).__name__, exc), file=sys.stderr)


def record_batch(tree, mutation_ops, today):
    """Fail-open entry point for the batch path (DECOMPOSE + PRUNE).

    Call INSIDE the write lock, BEFORE the tree is serialized — it mutates
    `tree`. Both `cmd_batch` (CLI) and the daemon's batch branch call exactly
    this, so the two paths cannot diverge.
    """
    try:
        rows = decompose_rows(mutation_ops, today) + prune_rows(
            mutation_ops, today)
        return append_rows(tree, rows)
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        _warn(e, "batch")
        return 0


def record_reparent(tree, node, new_parent, today):
    """Fail-open entry point for the reparent path. Mutates `tree`."""
    try:
        return append_rows(tree, reparent_row(node, new_parent, today))
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        _warn(e, "reparent")
        return 0
