#!/usr/bin/env python3
"""Shared dependency-graph primitives for the `blocked_by` edge set.

PURE — no I/O, no daemon calls, no path resolution. Callers do their own
reading and hand in `{goal_id: goal_dict}`; this module only normalizes the
polymorphic edge field and walks the resulting graph. That split is what makes
it importable from both a precheck sweep and a test without dragging in
`_paths`/`_rt` (the daemon-import-unsafety guard-547 names).

SSOT for `norm_blocked_by`. Two call sites today:
  - `dependency-cycle-check.py`            (cycle detection, precheck 0.5b.16)
  - `blocked-signal-resolution-check.py`   (resolution sweep, precheck 0.5b.12)
guard-547's amendment prefers "extract to a shared module imported by both"
over hand-mirroring, and its measured harm — a duplicated copy drifting four
fixes behind and producing a ~40% live divergence — is exactly what a second
copy of this normalizer would invite. It is deliberately NOT re-derived here:
the body below is the one from `blocked-signal-resolution-check.py`, moved.
"""

import re

# --- edge normalization ----------------------------------------------------


def norm_blocked_by(v):
    """Normalize the polymorphic `blocked_by` field to list[str].

    THE defect this survives: the field is a bare STRING on some goals
    (g-115-3053, g-335-144 on 2026-07-26) and a LIST on others. A checker that
    iterates it directly turns 'g-335-260' into 7 single-character phantom ids,
    none of which resolve — so the goal reads "not resolved" forever and is
    silently excluded from every verdict. Non-str list members are dropped
    rather than coerced (an unexpected shape must not become a confident wrong
    id).
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str) and x.strip()]
    return []


# --- graph construction ----------------------------------------------------

# "superseded" and "decomposed" are TERMINAL closes in the goal vocabulary
# (aspirations.py TERMINAL_GOAL_STATUSES) and MUST appear here: build_graph
# below skips terminal goals, so a status missing from this tuple stays in the
# adjacency map and keeps BLOCKING its dependents forever. Measured 2026-08-27
# (): "superseded" was absent while the write path already accepted
# it, so closing a duplicate as superseded would have wedged its dependents --
# strictly worse than the skipped-close it was meant to replace.
TERMINAL_STATUSES = ("completed", "archived", "skipped", "expired", "resolved",
                     "superseded", "decomposed")


def build_graph(goal_index, terminal_statuses=TERMINAL_STATUSES):
    """Build the blocked_by adjacency map over NON-TERMINAL goals.

    `goal_index` is {goal_id: goal_dict}. Returns (edges, dangling) where
    `edges` is {goal_id: [target_id, ...]} restricted to non-terminal SOURCES,
    and `dangling` is [(source_id, target_id), ...] for targets absent from
    `goal_index` entirely.

    WHY SOURCES ARE FILTERED BUT TARGETS ARE NOT. A terminal goal cannot be
    waiting on anything, so its edges are history and including them would
    manufacture phantom cycles out of finished work. A terminal TARGET, by
    contrast, must stay in the graph: it is the thing that BREAKS a chain, and
    dropping it would silently convert a resolved dependency into a dangling
    one.

    WHY `dangling` IS RETURNED RATHER THAN IGNORED (guard-1890). An id absent
    from the index is NOT automatically a typo — if the caller resolved only
    live queues, a COMPLETED-then-ARCHIVED dependency lands here too, and
    reporting it as a broken reference is the precise false positive that froze
    g-005-17 for 37 days. Callers MUST fold the archive into `goal_index`
    before trusting this list; the sweep that owns this module does, and says
    so in its output when the archive read degraded.
    """
    edges = {}
    dangling = []
    for gid, goal in goal_index.items():
        if (goal.get("status") or "") in terminal_statuses:
            continue
        targets = norm_blocked_by(goal.get("blocked_by"))
        if not targets:
            continue
        edges[gid] = targets
        for t in targets:
            if t not in goal_index:
                dangling.append((gid, t))
    return edges, dangling


# --- cycle detection -------------------------------------------------------


def find_cycles(edges):
    """Return every simple cycle in `edges` as a list of node lists.

    Iterative DFS with an explicit colour map — WHITE/GREY/BLACK — so a GREY
    hit is a back-edge and therefore a cycle. Iterative rather than recursive
    on purpose: the edge set is small today (36 edges fleet-wide, measured
    2026-08-09) but recursion depth is a function of chain length, and a sweep
    that raises RecursionError on a pathological queue would fail exactly when
    the graph is most degenerate.

    Each returned cycle is the node list in traversal order WITHOUT the
    repeated closing node (`['a','b']` for a<->b, `['a']` for a self-loop), so
    `len(cycle)` is the cycle's true length. SELF-LOOPS COUNT: `X blocked_by X`
    is a degenerate one-node cycle and is the cheapest form of this bug to
    write by hand, so it must never be filtered out as trivial.

    Cycles are de-duplicated by their canonical rotation, because one cycle
    reached from two different entry nodes is ONE deadlock, not two — an
    inflated count would misreport severity to whoever reads the verdict.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {}
    cycles = []
    seen_keys = set()

    def _record(path):
        # Canonical rotation: smallest node first, so the same ring found from
        # any entry point collapses to one key.
        i = path.index(min(path))
        rot = tuple(path[i:] + path[:i])
        if rot not in seen_keys:
            seen_keys.add(rot)
            cycles.append(list(rot))

    for root in sorted(edges):
        if colour.get(root, WHITE) != WHITE:
            continue
        # stack frames: (node, iterator over its targets)
        stack = [(root, iter(edges.get(root, ())))]
        path = [root]
        colour[root] = GREY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                c = colour.get(nxt, WHITE)
                if c == GREY:
                    _record(path[path.index(nxt):])
                elif c == WHITE:
                    colour[nxt] = GREY
                    path.append(nxt)
                    stack.append((nxt, iter(edges.get(nxt, ()))))
                    advanced = True
                    break
                # BLACK: fully explored, cannot be on the current path
            if not advanced:
                colour[node] = BLACK
                stack.pop()
                path.pop()
    return cycles


# --- supersession-aware dependency resolution ------------------------------

# A goal id: g-NNN-NN, widened to 2-4 digits on both halves (CLAUDE.md ID
# Formats —  hit  on 2026-05-19).
_GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,4}\b")

# The migration fallback's marker. Rows closed BEFORE `superseded_by` existed
# (the ..80 shape) carry their supersession only as prose in
# outcome_note, so the marker is the one machine-findable handle on them.
# Matched case-insensitively at a WORD boundary: "superseded by "
# and "SUPERSEDES the duplicate chain" are both real phrasings in the store.
_SUPERSEDED_NOTE_RE = re.compile(r"supersed(?:ed|es|ing)\b", re.IGNORECASE)

# Statuses whose goal may carry a supersession pointer. `skipped` is here
# because it is what the store actually used before `superseded` was
# reachable, and those rows are the entire migration population.
SUPERSEDABLE_STATUSES = ("skipped", "superseded")


def supersession_target(goal):
    """Return the goal-id this goal was superseded BY, or None.

    Two sources, in precedence order:
      1. the explicit `superseded_by` field (authoritative, written at close)
      2. a goal-id in `outcome_note` alongside a supersession marker — the
         MIGRATION FALLBACK for rows closed before the field existed

    The fallback is deliberately narrow. It requires BOTH a supersession word
    AND a goal id, and it takes the FIRST id in the note. A note that merely
    MENTIONS another goal is not a supersession, and a bare `*supersed*`
    substring with no id resolves to nothing rather than to a guess — an
    over-eager fallback here would silently satisfy a dependency that is not
    actually met, which is strictly worse than the freeze this fixes.
    """
    if not isinstance(goal, dict):
        return None
    explicit = goal.get("superseded_by")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if (goal.get("status") or "") not in SUPERSEDABLE_STATUSES:
        return None
    note = goal.get("outcome_note") or ""
    if not isinstance(note, str) or not _SUPERSEDED_NOTE_RE.search(note):
        return None
    m = _GOAL_ID_RE.search(note)
    return m.group(0) if m else None


def resolve_dependency(target_id, goal_index, terminal_statuses=TERMINAL_STATUSES):
    """Resolve whether a dependency on `target_id` is SATISFIED.

    Returns (verdict, resolved_id, chain) where verdict is one of:
      "satisfied"   — the target, or the goal that superseded it, is completed
      "open"        — the target is live (or closed in a way that does not
                      satisfy a dependency, e.g. expired)
      "unknown"     — the target is not in `goal_index` at all
      "cycle"       — the superseded_by chain loops (a data defect)

    THIS IS THE WHOLE POINT OF THE GOAL. The echo incident (2026-08-26): a
    duplicate chain was closed `skipped` with supersession notes, a re-probe
    read `status == "skipped"` as NOT-done, and re-deferred two goals that had
    just been unblocked — zero vinheim goals selectable across 1,400 ranked
    for ~4h. A status-equality check cannot see that the work IS done under a
    different id; following the pointer is what makes it visible.

    `completed` is the ONLY status that satisfies. A superseding goal that is
    itself skipped/expired does NOT satisfy the dependency — supersession
    moves the obligation, it does not discharge it. Chains are followed so a
    duplicate-of-a-duplicate still resolves to whoever finally did the work.
    """
    seen = []
    current = target_id
    while True:
        if current in seen:
            return "cycle", current, seen
        seen.append(current)
        goal = goal_index.get(current)
        if goal is None:
            # Absent from the index. Per build_graph's guard-1890 note this is
            # NOT automatically a typo — an archived completion lands here too
            # — so the honest verdict is "unknown", never "open".
            return "unknown", current, seen
        status = goal.get("status") or ""
        if status == "completed":
            return "satisfied", current, seen
        nxt = supersession_target(goal)
        if not nxt:
            return "open", current, seen
        current = nxt
