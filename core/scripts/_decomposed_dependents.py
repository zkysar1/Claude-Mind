"""Keep a decomposed goal's live dependents waiting on its children ().

WHY THIS EXISTS
---------------
`decomposed` is a terminal status, and a terminal status write releases every
goal waiting on it in two ways at once. The terminal cleanup
(`_clear_stale_blockers` in the CLI, `_clear_stale_blockers_inline` in the
daemon) strips the goal's id from every `blocked_by` in the store. And
goal-selector.py counts a decomposed goal as DONE (`done_ids` = completed |
decomposed), so even an edge that survived would read as satisfied. A goal
waiting on the parent therefore becomes selectable the moment the parent is
decomposed, before any child has run.

/decompose Step 6.2 ("re-point the parent's dependents FIRST") prevents this,
but it is honor-system. Measured 2026-09-25 (alpha fresh-eyes, findings
msg-20260925-231943-alpha-3133): g-376-51 was decomposed into g-376-51-a..d,
Step 6.2 was skipped, and the write stripped g-376-51 from g-376-53's
blocked_by ([g-376-51, g-376-52] -> [g-376-52]). The client-executes goal
would have become selectable once g-376-52 completed, before any port existed.

So the status write itself now performs Step 6.2, in the same locked write
that would otherwise strip the edge. Both writers call this module; the daemon
is the live path and the CLI is its twin (guard-547 / guard-2323), so the
logic lives here once instead of in two copies that can drift.

WHICH CHILDREN
--------------
A child is a goal in the SAME store whose `parent_goal` is the parent. A goal
with no `parent_goal` also counts when its `origin_signal` is EXACTLY
"decomposition:<parent>". /decompose sets both fields. Measured over the live
queue on 2026-09-27: 59 live goals carry a "decomposition:" origin with no
`parent_goal`, 27 of them in the exact form. The other 32 are free text
("decomposition: surfaced during ..."), so only the exact form counts. The
/decompose Step 6.4 progress_note line cannot be the source: it is written
AFTER the status write.

WHICH CHILD A DEPENDENT WAITS ON
--------------------------------
The sinks of the child set: children that no other child lists in its
blocked_by. A sequential decomposition has one sink, its last child. A
parallel one has a sink per child, and waiting on all of them is waiting on
the whole deliverable. If every child is some other child's predecessor (a
cycle), the dependent waits on every child. A matching `depends_on` entry is
re-pointed with its `expects` kept, so the depends_on-must-appear-in-blocked_by
rule (goal-schemas.md) still holds after the write.

WHEN NOTHING CAN BE RE-POINTED
------------------------------
If there are live dependents and no child, the plan says refuse, and names the
dependents. Auto re-point is preferred because a refusal inside a peer's loop
is a stall; the refusal is kept only for the case where releasing the
dependents is the one outcome that is certainly wrong.

SCOPE: THE STORE BEING WRITTEN
------------------------------
Only goals in the store this write holds the lock on are seen or changed. A
dependent in the OTHER queue is not stripped by the terminal cleanup, but the
selector's global done_ids would still release it. Measured 2026-09-27: 0 of
the 46 live goals with a non-empty blocked_by have a cross-store edge, so a
second store lock inside this write is not worth its risk today.

Pure: reads no files, env, or clock. `plan` never mutates; `apply` mutates only
the dependents that `plan` named.
"""
from __future__ import annotations

from _goal_census import TERMINAL_STATUSES


def _as_list(value):
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return value
    return []


def _goals(items):
    for asp in items or []:
        for g in asp.get("goals") or []:
            if isinstance(g, dict) and g.get("id"):
                yield g


def plan(items, parent_id):
    """Decide what a status=decomposed write on `parent_id` must do to its dependents.

    Returns {"dependents": [ids], "sinks": [ids], "refuse": bool,
    "message": str | None}. Call before any mutation, so a refusal writes
    nothing.
    """
    origin = f"decomposition:{parent_id}"
    goals = list(_goals(items))
    children = [
        g for g in goals
        if g["id"] != parent_id and (
            g.get("parent_goal") == parent_id
            or (not g.get("parent_goal")
                and str(g.get("origin_signal") or "").strip() == origin))
    ]
    child_ids = [c["id"] for c in children]
    dependents = [
        g["id"] for g in goals
        if g["id"] != parent_id and g["id"] not in child_ids
        and g.get("status") not in TERMINAL_STATUSES
        and parent_id in _as_list(g.get("blocked_by"))
    ]
    sinks = [
        c for c in child_ids
        if not any(c in _as_list(o.get("blocked_by"))
                   for o in children if o["id"] != c)
    ] or child_ids
    refuse = bool(dependents) and not child_ids
    message = None
    if refuse:
        message = (
            f"decomposed_dependents_unrepointable: {parent_id} still has live "
            f"dependents ({', '.join(dependents)}) and no child to re-point "
            f"them at: no goal in this store has parent_goal={parent_id} or "
            f"origin_signal={origin}. The selector counts a decomposed goal "
            f"as DONE, so this write would release them before anything they "
            f"wait on exists. File the children first, or re-point each "
            f"dependent's blocked_by at the goal that now delivers what it "
            f"waits on (/decompose Step 6.2), then retry. If the work was "
            f"dropped rather than decomposed, the status is skipped or "
            f"superseded, not decomposed. (g-115-10977)"
        )
    return {"dependents": dependents, "sinks": sinks,
            "refuse": refuse, "message": message}


def _repoint_depends_on(depends_on, parent_id, sinks):
    if isinstance(depends_on, str):
        return list(sinks) if depends_on == parent_id else depends_on
    if not isinstance(depends_on, list):
        return depends_on
    present = {e.get("goal_id") if isinstance(e, dict) else e
               for e in depends_on if isinstance(e, (dict, str))}
    out = []
    for e in depends_on:
        if isinstance(e, dict) and e.get("goal_id") == parent_id:
            out.extend(dict(e, goal_id=s) for s in sinks if s not in present)
        elif e == parent_id:
            out.extend(s for s in sinks if s not in present)
        else:
            out.append(e)
    return out


def apply(items, result, parent_id):
    """Re-point the dependents `plan` named at its sinks.

    Returns one line per re-pointed goal, for the caller to surface on the
    writer's stderr.
    """
    if result["refuse"] or not result["dependents"]:
        return []
    sinks = result["sinks"]
    wanted = set(result["dependents"])
    lines = []
    for g in _goals(items):
        if g["id"] not in wanted:
            continue
        bb = [b for b in _as_list(g.get("blocked_by")) if b != parent_id]
        bb.extend(s for s in sinks if s not in bb)
        g["blocked_by"] = bb
        if "depends_on" in g:
            g["depends_on"] = _repoint_depends_on(g["depends_on"], parent_id, sinks)
        lines.append(
            f"[aspirations] decomposed {parent_id}: re-pointed {g['id']} "
            f"blocked_by {parent_id} -> {', '.join(sinks)} (g-115-10977)")
    return lines
