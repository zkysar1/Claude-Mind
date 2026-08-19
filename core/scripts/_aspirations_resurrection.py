"""Pure predicate for RESURRECTED aspirations — the single source of truth
shared by the daemon's archive_sweep (which APPLIES the verdict:
mind_api/src/endpoints/aspirations_write.py::_reconcile_resurrected) and the
read-only scan behind the /verify-learning check
(core/scripts/aspirations-resurrection-scan.py). Keeping the predicate here
means the detector and the remedy cannot disagree about what a resurrection is.

THE CLASS (goal-completion audit, 2026-08-16). coordination_merge.merge_aspirations
is a UNION by aspiration id, so a record removed from the live file (retire /
complete / archive_sweep) has no representation a peer's merge can see; any
box still holding the pre-retirement copy re-adds it PRISTINE — goals back to
pending, no outcome_note, no last_modified. Measured: 9 of 29 live aspirations
were also present in the archive; 8 were resurrected retirements (7 asp-xw-*
cross-world stubs + asp-240).

THE PREDICATE. A live goal is a RESURRECTED copy when the archive holds the
same aspiration in a terminal status AND holds that goal id in a terminal
status, while the live copy is non-terminal, non-recurring, UNCLAIMED, and
not modified after the archive's terminal date. A live goal the archive never
saw, or one claimed / modified since, is POST-ARCHIVE WORK — a legitimate
reopen (asp-328 shape) that keeps the aspiration live and is never touched.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from _goal_census import TERMINAL_STATUSES  # type: ignore
except Exception:  # pragma: no cover - import-order guard for odd sys.path
    TERMINAL_STATUSES = frozenset(
        {"completed", "skipped", "expired", "decomposed", "superseded"})

TERMINAL_ASP_STATUSES = frozenset({"completed", "retired"})


def archive_terminal_stamp(asp: Dict[str, Any]) -> str:
    """The date (YYYY-MM-DD) an archived aspiration reached its terminal
    status — retired_at / completed_at / archived_at, first present."""
    for key in ("retired_at", "completed_at", "archived_at"):
        val = asp.get(key)
        if val:
            return str(val)[:10]
    return ""


def archive_by_id(archive: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Last row per id wins (a legacy re-append leaves the newest last)."""
    out: Dict[str, Dict[str, Any]] = {}
    for rec in archive:
        if isinstance(rec, dict) and rec.get("id"):
            out[rec["id"]] = rec
    return out


def classify(live_asp: Dict[str, Any], arch: Dict[str, Any]
             ) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], bool]:
    """For one live aspiration and its archive row, return
    ([(live_goal, archived_goal), ...] resurrected pairs, post_archive_work).
    Empty pairs + False means "not a resurrection at all"."""
    if arch.get("status") not in TERMINAL_ASP_STATUSES:
        return [], False
    stamp = archive_terminal_stamp(arch)
    arch_goals = {g.get("id"): g for g in (arch.get("goals") or [])
                  if isinstance(g, dict)}
    post_archive_work = False
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for g in live_asp.get("goals") or []:
        if not isinstance(g, dict):
            continue
        ag = arch_goals.get(g.get("id"))
        if ag is None:
            post_archive_work = True
            continue
        if g.get("recurring") or g.get("status") in TERMINAL_STATUSES:
            continue
        if ag.get("status") not in TERMINAL_STATUSES:
            continue
        if g.get("claimed_by"):
            post_archive_work = True
            continue
        lm = str(g.get("last_modified") or "")[:10]
        if stamp and lm and lm > stamp:
            post_archive_work = True
            continue
        pairs.append((g, ag))
    return pairs, post_archive_work


def find_resurrected(items: Iterable[Dict[str, Any]],
                     archive: Iterable[Dict[str, Any]]
                     ) -> List[Dict[str, Any]]:
    """Read-only sweep over the live store: one entry per live aspiration
    carrying resurrected goals — {asp_id, arch_status, stamp, goal_ids,
    post_archive_work, would_rearchive}."""
    by_id = archive_by_id(archive)
    out: List[Dict[str, Any]] = []
    for a in items:
        if not isinstance(a, dict):
            continue
        arch = by_id.get(a.get("id"))
        if not arch:
            continue
        pairs, paw = classify(a, arch)
        if not pairs:
            continue
        resurrected_ids = {g.get("id") for g, _ in pairs}
        still_open = [g.get("id") for g in a.get("goals") or []
                      if isinstance(g, dict) and not g.get("recurring")
                      and g.get("status") not in TERMINAL_STATUSES
                      and g.get("id") not in resurrected_ids]
        out.append({
            "asp_id": a.get("id"),
            "arch_status": arch.get("status"),
            "stamp": archive_terminal_stamp(arch),
            "goal_ids": sorted(resurrected_ids),
            "post_archive_work": paw,
            "would_rearchive": (not paw and not still_open),
        })
    return out
