#!/usr/bin/env python3
"""Monitor Stale Check — auto-complete superseded Monitor goals.

Scans every Monitor goal across agent + world queues whose title contains a
processor run-ID (`proc-NNNNNNNNNN` or `proc-NNNNNNNNNNNNN`). For each goal
still `pending` or `in-progress`, compares the goal's embedded proc-ID
against the current run_dir reported by `processor-run.sh check-complete`.
If the goal's ID is older (< current) AND the goal's age exceeds
--max-age-hours (default 48), marks it superseded.

Dry-run by default; pass --apply to actually mutate goal status.

Safety gates (fail-closed — a missing prerequisite skips the sweep):
 - No processor-run.sh check-complete → no comparison target → skip entirely
 - run_dir missing from JSON (status == "running" or parse failure) → skip
 - Goal's title does not match the proc-NNN regex → skip that goal

LOST-UPDATE GUARD (g-115-6415). This is the FOURTH scan-then-write sweep over
shared goal records, and it was the one left unguarded when its three siblings
were fixed under g-115-6332: it was enumerated and correctly classified as
destructive by that goal's first worker run, then dropped between runs, so it
had neither a fix nor a stated reason. Two properties make it worth guarding
rather than tolerating:

  * It writes status=COMPLETED, and its outcome_note template is a ~40-char
    f-string. A stale decision therefore replaces an arbitrarily large worker
    outcome_note — the only durable account of what shipped — with that string.
  * Because it writes COMPLETED rather than skipped, a goal it damages is left
    in a fully plausible terminal state. g-115-6332's damage signature keys on
    `status=skipped AND completed_by AND outcome_class`, so it cannot see this
    sweep's damage BY CONSTRUCTION, not by tuning.

Before each write the sweep re-reads the goal from the STORE OF RECORD (not the
local read-through mirror) and refuses on: terminal status, completion
provenance, absence, or an unverifiable mirror-only read. Every refusal emits a
metrics row — a silent no-op is indistinguishable from never having raced. The
outcome_note write now PRESERVES any existing note beneath the generated line
instead of replacing it.

Exit code: always 0 (reporting tool). The guard's refusals are reported in
`refused_count`, never as a non-zero exit — this runs inside precheck Phase 0
and must never block the loop.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402

# IMPORTED, never redefined — `_is_owncloud_backend` is the SSOT for backend
# dispatch and a local copy would drift in the direction that makes this guard
# read the mirror while believing it read the store (guard-2783 / guard-1753).
from _team_state import _is_owncloud_backend  # noqa: E402

# The refusal POLICY is shared with the three sibling scan-then-write sweeps
# (unblock-parent-status, parent-supersession, routing-audit-target). Only the
# policy is shared; the authoritative READ stays behind a local seam so this
# file's test stubs resolve at call time (guard-2385). See _sweep_write_guard's
# header for why the split falls exactly there.
from _sweep_write_guard import (  # noqa: E402
    reread_goal_authoritative as _shared_reread_goal_authoritative,
    stale_candidate_reason as _shared_stale_candidate_reason,
)

PROC_ID_RE = re.compile(r"proc-(\d{10,13})")
RUN_DIR_ID_RE = re.compile(r"(\d{10,13})")

#: THIS SWEEP'S OWN legitimate-state predicate — deliberately NOT inherited.
#: It is the same tuple `_sweep_write_guard.DEFAULT_OPEN_STATUSES` happens to
#: carry, and that coincidence is the reason to write it out here rather than
#: lean on the default: it is derived from THIS sweep's candidate filter in
#: `main()` ("status not in (pending, in-progress) -> skip"), not from what a
#: sibling decided. 's own instruction was that each sweep needs its
#: own predicate because applying one sweep's to another breaks a working
#: sweep. `main()`'s candidate filter reads THIS constant rather than repeating
#: the literal, so the scan and the guard cannot drift apart: a scan wider than
#: the guard would emit candidates the guard then refuses one at a time, which
#: reads as a broken guard rather than as a scan bug.
MONITOR_OPEN_STATUSES = ("pending", "in-progress")


def _run(argv, input_text=None):
    r = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr


def _py(args, input_text=None):
    """Invoke a core script via the current Python interpreter. See blocker-recheck.py."""
    return _run([sys.executable] + args, input_text=input_text)


def _resolve_world_dir() -> Path:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import _paths  # noqa: WPS433 — local path dep
        return Path(_paths.WORLD_DIR)
    except Exception:
        return Path()


def _normalize_proc_id(digits: str) -> int:
    """proc-IDs come in two widths: 10-digit (seconds) and 13-digit (ms).
    Normalize both to ms so comparisons are valid across widths."""
    n = int(digits)
    if n < 10_000_000_000:
        return n * 1000
    return n


def _get_current_run_id(world_dir: Path) -> tuple:
    """Return (run_dir_basename, normalized_ms) or (None, None) if unavailable.

    Reads MSC_CHECK_COMPLETE_JSON populated by monitor-stale-check.sh rather
    than shelling into bash ourselves (sig-005 bypass).
    """
    raw = os.environ.get("MSC_CHECK_COMPLETE_JSON", "").strip()
    if not raw:
        return None, None
    try:
        data = json.loads(raw.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None, None
    run_dir = data.get("run_dir")
    if not run_dir:
        return None, None
    # run_dir may be "proc-NNN" or raw digits; accept both.
    m = RUN_DIR_ID_RE.search(str(run_dir))
    if not m:
        return run_dir, None
    return run_dir, _normalize_proc_id(m.group(1))


def _load_goals(source: str) -> list:
    """Load active aspirations for a source queue, flattening goals.

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    The daemon's /v1/aspirations/read endpoint requires at least one filter
    flag; we pass active=True since the candidate filter at line 192 only
    matches goals with status pending or in-progress anyway. Closes delta
    finding msg-20260516-102012-delta-1169 (Apply landed by bravo iter 43).
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"monitor-stale-check: {source} read failed: {e.body or e}", file=sys.stderr)
        return []
    if not out or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    flat = []
    for asp in data if isinstance(data, list) else []:
        asp_id = asp.get("id")
        for g in asp.get("goals", []):
            g["_asp_id"] = asp_id
            g["_source"] = source
            flat.append(g)
    return flat


def _age_hours(goal: dict) -> float:
    """Age in hours from goal.created (ISO) to now. -1 if unparseable."""
    created = goal.get("created") or goal.get("createdAt")
    if not created:
        return -1.0
    try:
        t = dt.datetime.fromisoformat(created.rstrip("Z"))
    except ValueError:
        return -1.0
    return (dt.datetime.now() - t).total_seconds() / 3600.0


def _read_aspirations(source):
    """Return list of ``(aspiration_dict, source_str)`` tuples — the shape
    `_sweep_write_guard._find_goal` consumes.

    Deliberately separate from `_load_goals` above, which flattens to goals and
    stamps `_asp_id`/`_source` for the SCAN. This one feeds the authoritative
    RE-READ, so it must stay a faithful view of the store rather than a
    scan-shaped projection.

    RAISES on a read failure rather than returning ``[]``. That is the
    fail-CLOSED direction and it is the opposite of `_load_goals`'s fail-open
    ``[]``: an empty scan simply sweeps nothing, but an empty re-read would make
    every goal look ABSENT from the store of record, which
    `stale_candidate_reason` correctly treats as a refusal. The shared reader
    catches this and reports PROV_NONE, so the write is refused loudly instead
    of proceeding on an unverified read.
    """
    raw = _rt.aspirations_read(source=source, active=True)
    if not raw or not raw.strip():
        return []
    data = json.loads(raw)
    aspirations = data.get("aspirations") if isinstance(data, dict) else data
    return [(asp, source) for asp in (aspirations or [])]


def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. Mirrors the sibling sweeps' convention."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "monitor-stale-check-metrics.jsonl"


def _append_metric(path, record):
    """Append one metric record. Fail-open by design — a metrics miss must
    never turn into a refused sweep or a crashed precheck."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:  # noqa: BLE001
        print(f"[monitor-stale-check] WARN: metrics append failed: {e}",
              file=sys.stderr)


def _reread_goal_authoritative(source, goal_id):
    """``(goal, provenance)`` from the STORE OF RECORD — thin seam over the
    shared reader in `_sweep_write_guard`.

    Both collaborators are passed EXPLICITLY and resolve as module globals at
    call time, which keeps `monkeypatch.setattr(mod, "_read_aspirations", ...)`
    and `monkeypatch.setattr(mod, "_is_owncloud_backend", ...)` working
    (guard-2385). Do not let the shared module import them itself: the patches
    would still apply and would silently stop being consulted.
    """
    return _shared_reread_goal_authoritative(
        source, goal_id,
        read_aspirations=_read_aspirations,
        is_owncloud=_is_owncloud_backend,
        label="monitor-stale-check",
    )


def _stale_candidate_reason(goal, provenance):
    """``None`` when the write may proceed, else the refusal reason.

    Judges an ALREADY-PERFORMED re-read against THIS sweep's own predicate.
    Takes `(goal, provenance)` rather than `(source, goal_id)` — unlike the
    sibling's same-named seam — because `_apply_completion` needs the record
    itself to preserve the existing outcome_note, and reading it twice would
    open a second window between the judgement and the value it judged
    (guard-3020: never base a read-modify-write on an earlier snapshot).
    """
    return _shared_stale_candidate_reason(
        goal, provenance, open_statuses=MONITOR_OPEN_STATUSES
    )


def _compose_note(reason: str, existing) -> str:
    """Sweep line FIRST, any pre-existing note PRESERVED beneath it.

    `aspirations-update-goal.sh <id> outcome_note` REPLACES the field, it does
    not append (guard-1691 / guard-3626). This sweep's generated reason is ~40
    chars, so a bare replace silently destroys an arbitrarily large record of
    what a worker actually shipped — the precise damage g-115-6415 was filed
    for, and the executor-side half of guard-4033.

    The sweep line stays at the HEAD so the `superseded-by-newer-run` token
    remains greppable at a fixed position (an existing regression pin asserts
    it is present) and so a reader sees the disposition first; the filer's
    account survives underneath instead of being traded for it (guard-1227 — a
    sweep that terminates a goal must leave the filer a recoverable trace).
    """
    prior = (existing or "").strip()
    if not prior:
        return reason
    return f"{reason}\n\n[preserved prior outcome_note — {len(prior)} chars]\n{prior}"


def _apply_completion(goal: dict, current_run_dir: str, metrics_path=None) -> tuple:
    """Mark the goal completed via aspirations.py update-goal.

    g-115-6415: re-asserts this sweep's candidate predicate against the STORE OF
    RECORD immediately before writing, and refuses when it no longer holds. The
    scan that produced this candidate ran over every eligible goal in both
    queues, so the scan->apply gap is unbounded in principle.

    `metrics_path` is keyword-with-default so the pre-existing two-argument call
    shape in `test_monitor_stale_check_apply_completion.py` keeps working — that
    file pins an argparse arg-ORDER defect (g-115-4820) which is orthogonal to
    this guard and must not be disturbed to land it.
    """
    source = goal["_source"]
    goal_id = goal["id"]

    fresh, prov = _reread_goal_authoritative(source, goal_id)
    stale = _stale_candidate_reason(fresh, prov)
    if stale is not None:
        print(f"[monitor-stale-check] REFUSED {goal_id}: {stale}", file=sys.stderr)
        # COUNT the refusal. A silent no-op is indistinguishable from never
        # having raced, which would make this guard's own effectiveness
        # unmeasurable — and an unmeasurable guard is the one that gets
        # "simplified" away later.
        _append_metric(metrics_path, {
            "type": "monitor_stale_refused_stale_candidate",
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id,
            "source": source,
            "aspiration_id": goal.get("_asp_id"),
            "current_run_dir": current_run_dir,
            "reason": stale,
            "agent": os.environ.get("MIND_AGENT", "") or None,
        })
        return False, f"refused: {stale}"

    reason = f"superseded-by-newer-run ({current_run_dir})"
    # --source is a TOP-LEVEL argparse argument on aspirations.py (registered on
    # `parser`, not on the `update-goal` subparser), so it MUST precede the
    # subcommand. The reversed order exits rc=2 "unrecognized arguments" before
    # any goal lookup happens, which made this whole function a permanent no-op
    # (). All five sibling direct callers already use this order.
    rc, out, err = _py(
        [
            str(SCRIPT_DIR / "aspirations.py"),
            "--source",
            goal["_source"],
            "update-goal",
            goal["id"],
            "status",
            "completed",
        ]
    )
    if rc != 0:
        return False, err.strip() or out.strip()
    # Annotate outcome_note for the encoding/journal path. Composed from the
    # AUTHORITATIVE re-read taken above, not from the scan-time `goal` dict —
    # the scan record is minutes old and its outcome_note may predate a worker's
    # close (guard-3020).
    note = _compose_note(reason, (fresh or {}).get("outcome_note"))
    _py(
        [
            str(SCRIPT_DIR / "aspirations.py"),
            "--source",
            goal["_source"],
            "update-goal",
            goal["id"],
            "outcome_note",
            note,
        ]
    )
    # The MUTATION record. guard-1231: a sweep that moves a goal to a terminal
    # status must be surfaced to the filer, and a metrics row is only half of
    # that — `sweep-mutation-surface.py` is the consumer, and its SWEEP_LOGS map
    # carries this file's log so the row is actually read.
    _append_metric(metrics_path, {
        "type": "monitor_stale_completed",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "goal_id": goal_id,
        "source": source,
        "aspiration_id": goal.get("_asp_id"),
        "current_run_dir": current_run_dir,
        "preserved_prior_note_chars": len(((fresh or {}).get("outcome_note") or "").strip()),
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })
    return True, reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=48.0)
    ap.add_argument("--apply", action="store_true", help="Actually complete goals (default: dry-run)")
    ap.add_argument("--metrics-log", default=None,
                    help=('Path to JSONL metrics log. Default: '
                          '<WORLD_PATH>/monitor-stale-check-metrics.jsonl. '
                          'Pass "" to disable.'))
    args = ap.parse_args()
    metrics_path = _resolve_metrics_log(args.metrics_log)

    world_dir = _resolve_world_dir()
    if not world_dir or not world_dir.exists():
        print(json.dumps({"skipped": "world_dir_unresolved"}))
        return 0

    current_run_dir, current_ms = _get_current_run_id(world_dir)
    if current_ms is None:
        print(
            json.dumps(
                {
                    "skipped": "no_current_run_id",
                    "run_dir": current_run_dir,
                }
            )
        )
        return 0

    goals = _load_goals("world") + _load_goals("agent")
    candidates = []
    for g in goals:
        # SAME constant the write guard re-asserts against. Sharing the literal
        # is what keeps "what this sweep may act on" one decision instead of two
        # that drift — a scan wider than the guard would produce candidates the
        # guard then refuses one by one, which reads as a broken guard rather
        # than as a scan bug.
        if g.get("status") not in MONITOR_OPEN_STATUSES:
            continue
        title = g.get("title") or ""
        if "Monitor" not in title:
            continue
        m = PROC_ID_RE.search(title)
        if not m:
            continue
        goal_ms = _normalize_proc_id(m.group(1))
        if goal_ms >= current_ms:
            continue
        age_h = _age_hours(g)
        if age_h < args.max_age_hours:
            continue
        candidates.append(
            {
                "goal_id": g["id"],
                "asp_id": g["_asp_id"],
                "source": g["_source"],
                "title": title,
                "status": g.get("status"),
                "age_hours": round(age_h, 1),
                "goal_proc_id": m.group(1),
                "current_run_dir": current_run_dir,
            }
        )

    actions = []
    if args.apply:
        for c in candidates:
            ok, detail = _apply_completion(
                next(
                    g
                    for g in goals
                    if g["id"] == c["goal_id"] and g["_source"] == c["source"]
                ),
                current_run_dir,
                metrics_path=metrics_path,
            )
            actions.append(
                {
                    "goal_id": c["goal_id"],
                    "completed": ok,
                    "detail": detail,
                    "refused": (not ok) and str(detail or "").startswith("refused: "),
                }
            )

    refused_count = sum(1 for a in actions if a.get("refused"))
    if args.apply:
        # Always-written summary. `sweep-mutation-surface.py` skips records typed
        # "run_summary" by name, so this row is safe to emit every run and gives
        # the refusal rate a denominator (a refusal count with no candidate count
        # cannot distinguish "never raced" from "never ran").
        _append_metric(metrics_path, {
            "type": "run_summary",
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "candidate_count": len(candidates),
            "completed_count": sum(1 for a in actions if a.get("completed")),
            "refused_count": refused_count,
            "current_run_dir": current_run_dir,
            "agent": os.environ.get("MIND_AGENT", "") or None,
        })

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "current_run_dir": current_run_dir,
                "max_age_hours": args.max_age_hours,
                "candidates": candidates,
                "candidate_count": len(candidates),
                "actions_taken": actions,
                "refused_count": refused_count,
                "metrics_log": str(metrics_path) if metrics_path else None,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
