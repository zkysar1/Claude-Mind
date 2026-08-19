"""Quiesce-window ripeness evaluator — pure parse + verdict, no I/O side effects.

WHAT THIS OWNS. `world/conventions/fleet-quiesce-window.md` states that ripeness
"is evaluated on a recurring cadence so the SYSTEM surfaces 'the window is ripe'
rather than the user having to ask", and names a host sub-check. Until g-353-26
that wiring did not exist anywhere -- and the convention's own design ("the check
emits NOTHING when the verdict is hold") made a NEVER-WIRED evaluator and a
STANDING HOLD produce byte-identical output: silence plus a stale verdict line.
That is the guard-1419 shape -- one silence, two explanations, opposite actions
(keep waiting vs. call the window). This module is the disambiguator, so it is
deliberately LOUD about which of the two it is reporting.

THE STALE-ROW PROBLEM, which is the whole reason this is not a sum over YES rows.
A manifest row's "Window-ready?" cell is written by its AUTHOR at admission time
and is not updated when the item later runs. Measured 2026-08-13: five rows
(Q1/Q2/Q5/Q6/Q7) still read YES while the file's own outcome record above them
showed all five executed in the 2026-08-10 window. Summing the YES cells counts
finished work and manufactures ripeness -- the exact failure that would spend a
window (five terminals on five boxes) on an empty batch. So every row carrying a
goal id is cross-checked against that goal's LIVE status, and a row whose goal is
terminal is reported as `stale_row` rather than counted.

WHY THE CROSS-CHECK IS A REPORT AND NOT A SILENT FILTER. The convention's own
rb-5301 lesson says goal status LAGS live state and every row must be re-probed
against live state before a window is spent. So goal status is the cheapest
machine-readable signal, NOT ground truth: it can be stale in BOTH directions. A
row silently dropped for a terminal goal would hide exactly the disagreement a
human needs to see, so the drop is always named in the payload.

Pure: no file reads, no network, no writes. The caller supplies the markdown text
and a goal-status map; this returns a verdict dict. That split is what lets the
whole ladder be unit-tested without a world.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Terminal goal statuses — a row pointing at one of these has already run.
TERMINAL = frozenset({"completed", "skipped", "expired"})

# Criterion (a) floor, from the convention: "the window-READY items total >= 30
# min of work. Below 30 min the ceremony costs more than the work."
BATCH_FLOOR_MINUTES = 30

# The defer marker the convention NAMES as the criterion-(b) trigger. Kept for
# reference only -- do NOT match on it. Measured 2026-08-13: the sole goal in the
# fleet actually frozen on this window (, manifest row Q1) carries
# `human_blocked: requires a fleet-quiesced window only the user can create ...`,
# so an exact-prefix match finds zero and reports a healthy hold forever. The
# caller matches the substring "quiesce" over defer_reason instead, and the
# reason string below names what was MATCHED rather than this nominal form --
# a verdict that cites a marker the evidence does not contain is the kind of
# authoritative-sounding wrongness that survives review.
QUIESCE_DEFER_MARKER_NOMINAL = "precondition_unmet:fleet_quiesced_window"

_GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,4}\b")
_ROW_RE = re.compile(r"^\|\s*(Q\d+)\s*\|(.*)$")
# Ranges use an EN DASH in this file ("~70–90 min"), not a hyphen. Matching only
# ASCII '-' silently reads "70–90" as the single number 70 with trailing junk;
# it happens to land on the conservative value here, so it would never announce
# itself. Both separators are accepted so the low-end rule is deliberate.
_MINUTES_RE = re.compile(r"(\d+)\s*(?:[-–—]\s*(\d+)\s*)?m", re.IGNORECASE)


def _cell_says_done(cell: str) -> bool:
    """True when the readiness cell is a tombstone rather than a readiness claim.

    Checked BEFORE the YES test on purpose: a tombstone cell can contain both
    tokens ("DONE ... do not re-run" alongside a stale bold YES elsewhere in the
    row), and reading it as ready would re-admit finished work.
    """
    upper = cell.upper()
    return "DONE" in upper or "TOMBSTONE" in upper


def _cell_says_ready(cell: str) -> bool:
    return "YES" in cell.upper()


def parse_minutes(cell: str) -> Optional[int]:
    """Low end of an estimate cell, in minutes. None when unscoreable.

    Takes the LOW end of a range ("~20–30 min" -> 20) because criterion (a) is a
    floor: crossing it on the optimistic end of an estimate is how a half-batch
    gets a window called. Under-counting delays a window; over-counting spends
    one. Those costs are not symmetric.

    A row with no parseable estimate returns None and is reported as
    `unscoreable` -- never silently treated as 0, which would let an unestimated
    row sit in the ready set contributing nothing while looking counted. The
    convention's intake template calls Est. required for exactly this reason.
    """
    m = _MINUTES_RE.search(cell)
    if not m:
        return None
    return int(m.group(1))


def parse_rows(markdown: str) -> List[Dict[str, Any]]:
    """Extract manifest rows from the convention's markdown.

    Rows are identified by a leading `| Q<N> |` cell. The file also contains
    OUTCOME tables keyed by the same Q-ids (`| Q2 | ✅ 4/4 passed ... |`), so a
    row is only admitted when it has the manifest table's 7-column shape. The
    outcome tables are 2-column, which is what separates them -- not their
    position in the file, which moves every time a window is held.
    """
    rows: List[Dict[str, Any]] = []
    for line in markdown.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        qid, rest = m.group(1), m.group(2)
        cells = [c.strip() for c in rest.split("|")]
        # Trailing empty cell from the row's closing pipe.
        if cells and not cells[-1]:
            cells.pop()
        if len(cells) < 6:
            # 2-column outcome-record row, not a manifest row.
            continue
        item, why, shape, who, est, ready = cells[0], cells[1], cells[2], cells[3], cells[4], cells[5]
        goal_ids = _GOAL_ID_RE.findall(item)
        rows.append({
            "qid": qid,
            "item": item[:300],
            "goal_id": goal_ids[0] if goal_ids else None,
            "shape": shape[:40],
            "who": who[:60],
            "est_raw": est[:80],
            "est_minutes": parse_minutes(est),
            "ready_raw": ready[:300],
            "done": _cell_says_done(ready),
            "ready_claimed": _cell_says_ready(ready) and not _cell_says_done(ready),
        })
    return rows


# Sentinel marking the ONE line this tool owns in the convention. A dedicated
# stamp rather than a rewrite of the hand-written "Current ripeness verdict"
# prose: the narrative verdict carries run order, caveats and reasoning a
# generator cannot reproduce, so a tool that overwrote it would destroy exactly
# the context a reader needs -- and the convention already refused the mirror of
# that mistake ("hand-stamping a verdict this file's own tooling is about to own
# would fork the instrument"). One owner per line, in both directions.
STAMP_SENTINEL = "<!-- ripeness-stamp -->"


def render_stamp(result: Dict[str, Any], now_iso: str) -> str:
    """One line recording that an evaluation HAPPENED, and what it said.

    Written on GO **and on HOLD**. The hold case is the load-bearing one and the
    entire reason this exists: the convention's no-chatter design makes a
    never-wired evaluator and a standing hold produce byte-identical silence, so
    without a dated line there is nothing to tell "evaluated, still holding" from
    "nobody is evaluating". A stamp that only appeared when ripe would reproduce
    the original defect exactly (guard-2352 -- a recorder placed only on the fire
    path is absent precisely on the population you need to account for).
    """
    counts = result["counts"]
    return (
        f"**Last evaluated: {now_iso} — {result['verdict']}** — {result['reason']}. "
        f"({counts['ready']} ready / {result['total_ready_minutes']} min, "
        f"{counts['stale_row']} stale-row, {counts['tombstoned']} tombstoned; "
        f"auto-written by `core/scripts/quiesce-ripeness-check.sh --update-verdict`.)"
    )


def apply_stamp(markdown: str, stamp: str) -> Optional[str]:
    """Replace the line after STAMP_SENTINEL. None when the sentinel is absent.

    Returns None rather than inserting the sentinel: a tool that creates its own
    write target on a file it does not find as expected will happily stamp a
    renamed, moved, or half-migrated document. Absent sentinel is a condition for
    the caller to report, not to repair.
    """
    lines = markdown.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if STAMP_SENTINEL in line:
            nl = "\n" if not stamp.endswith("\n") else ""
            if i + 1 < len(lines):
                lines[i + 1] = stamp + nl
            else:
                lines.append(stamp + nl)
            return "".join(lines)
    return None


def evaluate(
    markdown: str,
    goal_status: Optional[Dict[str, Dict[str, Any]]] = None,
    quiesce_deferred: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Score the manifest and return a ripeness verdict.

    `goal_status` maps goal-id -> {"status": str, "priority": str}. A row whose
    goal is absent from the map is NOT treated as terminal: an unreadable goal is
    unknown, not done, and dropping it would quietly shrink the batch (rb-245 --
    a zero produced by a failed lookup reads identically to a real zero).

    `quiesce_deferred` is the list of goal ids frozen on
    `precondition_unmet:fleet_quiesced_window`, which the convention names as an
    independent criterion-(b) trigger.
    """
    goal_status = goal_status or {}
    quiesce_deferred = quiesce_deferred or []
    rows = parse_rows(markdown)

    # WHOLESALE-LOOKUP-FAILURE GUARD. Individually, an absent goal is "unknown,
    # not terminal" -- deliberate, see the docstring. But if the manifest names
    # goals and NOT ONE resolved, the lookup did not find nothing: it FAILED, and
    # treating that as "nothing is terminal" silently counts every stale row.
    # Measured on this file's own first version (fresh-eyes probe, 2026-08-13):
    # simulating that failure moved 5 ready rows/95 min -> 7 rows/115 min, HID
    # BOTH stale rows, and still printed GO -- indistinguishable from a healthy
    # run, and it re-enables the very "call a window for already-done work"
    # failure this module exists to prevent.
    # Same shape as the learning-routing incident (CLAUDE.md): an empty id-set
    # silently licensed 17,466 nullings. The general fix is identical -- an
    # unloadable corpus makes the question UNEVALUABLE, never answered-in-the-
    # permissive-direction. So: refuse to score, rather than score optimistically.
    named = [r["goal_id"] for r in rows if r["goal_id"]]
    if named and not goal_status:
        return {
            "verdict": "CANNOT-EVALUATE",
            "criterion_a": False,
            "criterion_b": False,
            "total_ready_minutes": 0,
            "batch_floor_minutes": BATCH_FLOOR_MINUTES,
            "reason": (
                f"status lookup returned NOTHING for all {len(named)} goal(s) named by the "
                f"manifest -- the lookup failed rather than finding nothing. Refusing to "
                f"score: with an empty status map every stale row counts and the batch "
                f"inflates silently."
            ),
            "counts": {
                "rows_parsed": len(rows), "ready": 0, "stale_row": 0,
                "tombstoned": 0, "not_ready": 0, "unscoreable_estimate": 0,
            },
            "ready": [], "stale_row": [], "unscoreable_estimate": [],
            "quiesce_deferred_goals": list(quiesce_deferred),
        }

    ready: List[Dict[str, Any]] = []
    stale: List[Dict[str, Any]] = []
    tombstoned: List[Dict[str, Any]] = []
    not_ready: List[Dict[str, Any]] = []
    unscoreable: List[Dict[str, Any]] = []

    for r in rows:
        if r["done"]:
            tombstoned.append(r)
            continue
        if not r["ready_claimed"]:
            not_ready.append(r)
            continue
        gid = r["goal_id"]
        live = goal_status.get(gid) if gid else None
        if live and str(live.get("status")) in TERMINAL:
            r["live_status"] = live.get("status")
            stale.append(r)
            continue
        if live:
            r["live_status"] = live.get("status")
            r["live_priority"] = live.get("priority")
        if r["est_minutes"] is None:
            unscoreable.append(r)
        ready.append(r)

    total_minutes = sum(r["est_minutes"] or 0 for r in ready)
    crit_a = total_minutes >= BATCH_FLOOR_MINUTES

    high_rows = [r for r in ready if str(r.get("live_priority", "")).upper() == "HIGH"]
    crit_b_defer = [g for g in quiesce_deferred if g]
    crit_b = bool(high_rows) or bool(crit_b_defer)

    reasons: List[str] = []
    if crit_b:
        if high_rows:
            reasons.append(
                "criterion (b): HIGH-priority ready item(s) "
                + ", ".join(f"{r['qid']}({r['goal_id']})" for r in high_rows)
            )
        if crit_b_defer:
            reasons.append(
                f"criterion (b): {len(crit_b_defer)} goal(s) whose defer_reason "
                f"names a quiesced window ({', '.join(crit_b_defer)})"
            )
    if crit_a:
        reasons.append(
            f"criterion (a): {total_minutes} min ready >= {BATCH_FLOOR_MINUTES} min floor"
        )
    if not reasons:
        reasons.append(
            f"hold: {total_minutes} min ready < {BATCH_FLOOR_MINUTES} min floor "
            f"and no HIGH ready item or quiesce-deferred goal"
        )

    return {
        "verdict": "GO" if (crit_a or crit_b) else "HOLD",
        "criterion_a": crit_a,
        "criterion_b": crit_b,
        "total_ready_minutes": total_minutes,
        "batch_floor_minutes": BATCH_FLOOR_MINUTES,
        "reason": "; ".join(reasons),
        "counts": {
            "rows_parsed": len(rows),
            "ready": len(ready),
            "stale_row": len(stale),
            "tombstoned": len(tombstoned),
            "not_ready": len(not_ready),
            "unscoreable_estimate": len(unscoreable),
        },
        "ready": [
            {k: r.get(k) for k in ("qid", "goal_id", "est_minutes", "live_status", "live_priority")}
            for r in ready
        ],
        # Named, never silently dropped: a row the manifest calls ready whose goal
        # has already gone terminal is the manifest lagging reality, and that lag
        # is itself the finding (it recurred within one day on 2026-08-11).
        "stale_row": [
            {k: r.get(k) for k in ("qid", "goal_id", "live_status", "est_minutes")}
            for r in stale
        ],
        "unscoreable_estimate": [
            {"qid": r["qid"], "goal_id": r["goal_id"], "est_raw": r["est_raw"]}
            for r in unscoreable
        ],
        "quiesce_deferred_goals": crit_b_defer,
    }
