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

TERMINAL_STATUSES = ("completed", "archived", "skipped", "expired", "resolved")


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
