"""Change since the last reading, for the advisory ratchet lanes ().

A ratchet records a count against a baseline that only shrinks, so its REGRESSED line says
how far the count sits above that baseline and never whether it moved since the last time
this box looked: a regression that worsened overnight and one unchanged for two weeks print
the same shape. This module answers that from the history the lane already keeps, with no
new field beside `baseline` and no goal id in the output.

WHY PER BOX. The merged history interleaves every box's readings, and a metric whose
population differs per box (goal counts, stalled goals) reads differently on each, so the
newest row in the list is often another box's and comparing against it would report a
population difference as a change. A row counts as this box's only when it carries this
box's `hostname`, which makes the comparison one between two readings of the same population.

WHY ROWS AND NOT A FIELD BESIDE `baseline`. coordination_merge.merge_audit_baselines takes
`baseline` by MIN, `history` by content-union and every other key whole from one side
(core/config/conventions/audit-baselines.md, "Merge across boxes"). A key inside a history
row is part of that row's content and survives the union untouched. A key beside `baseline`
can disagree with the MIN baseline it sits next to.

The lookup never relies on position: the union sorts by canonical content, not by time, so
the newest row of this box is found by `recorded_at`.
"""
from __future__ import annotations

import os
import socket
from datetime import datetime

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


def box_name() -> str:
    """The host a history row is attributed to (the expression the census lane used inline)."""
    return os.environ.get("HOSTNAME") or socket.gethostname()


def _age(previous_at: str, now_at: str):
    """'1h22m' style age between two naive stamps, or None when either will not parse."""
    try:
        seconds = int((datetime.strptime(str(now_at)[:19], STAMP_FORMAT)
                       - datetime.strptime(str(previous_at)[:19], STAMP_FORMAT)).total_seconds())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def since_last_reading(history, current: int, host: str, now_at: str) -> dict:
    """Compare `current` with this box's newest earlier reading in `history`.

    Call it INSIDE the lane's lock and BEFORE the new row is appended, so the row being
    written is never its own predecessor. Lower is better, as for every baseline in the file.

    Returns {"state": "none" | "unchanged" | "worsened" | "improved", "host", "current",
    "rows", "unattributed"} plus "previous", "previous_at", "delta" and "age" when a
    reading of this box exists. `unattributed` counts rows with no hostname: they predate
    the attribution and cannot be matched to any box.
    """
    rows = [r for r in (history or []) if isinstance(r, dict)]
    mine = [r for r in rows
            if r.get("hostname") == host and r.get("recorded_at")
            and isinstance(r.get("drift_total"), int)
            and not isinstance(r.get("drift_total"), bool)]
    out = {"state": "none", "host": host, "current": current, "rows": len(rows),
           "unattributed": sum(1 for r in rows if not r.get("hostname"))}
    if not mine:
        return out
    last = max(mine, key=lambda r: str(r["recorded_at"]))
    previous = last["drift_total"]
    out.update(previous=previous, previous_at=str(last["recorded_at"]),
               age=_age(last["recorded_at"], now_at), delta=current - previous)
    out["state"] = ("unchanged" if current == previous
                    else "worsened" if current > previous else "improved")
    return out


def describe(since: dict) -> str:
    """One line for a lane's output.

    It names no goal: a tracking-goal id printed in tool output tells every later reader
    the defect is covered, and keeps telling them after that goal closes (guard-3263).
    """
    state = since["state"]
    if state == "none":
        note = (f"; {since['unattributed']} earlier row(s) carry no hostname"
                if since["unattributed"] else "")
        return (f"no earlier reading from this box ({since['host']}) among the "
                f"{since['rows']} recorded{note}")
    age = f" ({since['age']} ago)" if since.get("age") else ""
    if state == "unchanged":
        return (f"unchanged since {since['previous_at']}{age} on {since['host']}: "
                f"{since['current']}")
    sign = "+" if since["delta"] > 0 else ""
    return (f"{state} since {since['previous_at']}{age} on {since['host']}: "
            f"{since['previous']} -> {since['current']} ({sign}{since['delta']})")
