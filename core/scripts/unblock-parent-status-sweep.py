#!/usr/bin/env python3
# pyright: strict
r"""Unblock-Parent-Status Sweep (g-250-76, rb-908).

Layer D of the capability-routing-enforcement pattern fires
`capability-gate.py --suggest-unblock` synchronously at defer-time, filing
an `Unblock: <verb> for <parent-id>` goal via cmd_update_goal. When the
parent goal subsequently lands in a terminal non-execution state — skipped
(WRONG LAYER finding), completed (resolved without the Unblock), superseded
(sibling decomposition closed the intent), or archived — the Layer D Unblock
survives as actionable work even though its premise has dissolved.

Canonical incident (rb-908):
    g-250-73 'Unblock: behavior for g-250-69' filed 2026-05-13T09:45:14 from
    capability-gate keyword-match on "behavior". g-250-69 was SKIPPED 72
    seconds later (09:46:25) with WRONG LAYER finding ("bumping
    AspirationalModule weights would violate TestAspirationalModuleInvariants:461;
    fix routes through IntentEngineVerticle.scoreCandidate fallback instead").
    g-250-73 should have been auto-skipped at parent-skip time. No mechanism
    existed to detect the parent transition. Resolution required manual
    inspection during /reflect.

This sweep closes the gap as a companion to the rb-428 family:
    - blocker-recheck.py  — Layer C for participants:[user] blockers
    - defer-recheck.py    — explicit dep-chain narrative defers
    - precondition-defer-recheck.py — structured precondition_unmet
    - monitor-stale-check.py — Monitor proc-NNN goals past current run_dir
    - pending-questions-sweep.py — source_goal-completed lifecycle
    - parent-supersession-sweep.py — sibling decomposition supersession
    - unblock-parent-status-sweep.py — THIS sweep (Layer D parent terminal)

Heuristic (conservative for v1 — title-anchored):
    For each candidate Unblock goal U:
        - title starts with "Unblock:" (Layer D canonical title format)
        - status in (pending, in-progress)
        - parent_id parseable from one of three signals (priority order):
            1. origin_signal == "unblock:<g-id>"  (Layer D capability-gate
               emits this exact form via _build_suggestion in capability-gate.py)
            2. title regex r"\bfor\s+(g-\d+-\d+)\b"  (Layer D title shape:
               "Unblock: <verb> for <parent-id>")
            3. discovered_by field == "<g-id>"  (legacy/manual unblock)
        - age (created_at OR defer_reason_set_at) >= --max-age-hours
          (default 0 — fire immediately; tunable for testing)
    Parent status lookup:
        - Read parent across world + agent active queues, then archived if
          present in active scan as status=archived.
        - If parent not found in active scan: treat as 'archived' (parent
          moved to archived/ — this IS the supersession signal).
    Parent in terminal state set:
        {'skipped', 'completed', 'superseded', 'archived'}
    If parent.status in terminal set: U is a sweep candidate.

Action modes:
    --report (default): print JSON, no mutation
    --apply: mark each candidate as status=skipped with outcome_note
        "parent resolved without action needed (parent_id=<X>, parent.status=<Y>)"
        Idempotent: if outcome_note already starts with "parent resolved
        without action needed", skip the rewrite.

Single-writer / fail-quiet (rb-428 family):
    - One Python pass over active queues per call
    - Metric writes use locked_append_jsonl — fail-open on metric error
    - Update failures log but do not abort the sweep
    - Always exits 0 (reporting tool)

Exit: 0 always. JSON result:
    {
      "scanned": N,        # total Unblock:-titled goals examined
      "eligible": N,       # passed age + parent_id parse filters
      "candidates": [...], # parent in terminal state (recommend skip)
      "applied": N,        # Unblocks marked skipped (apply mode only)
      "details": [...],    # per-goal trace incl. parent status
    }

CLI:
    unblock-parent-status-sweep.py [--max-age-hours N] [--apply]
                                   [--output json|human]
                                   [--metrics-log PATH | --metrics-log ""]
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

from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)


# Title must start with literal "Unblock:" — Layer D canonical
UNBLOCK_TITLE_PATTERN = re.compile(r"^\s*Unblock\s*:", re.IGNORECASE)
# origin_signal: exact form emitted by capability-gate._build_suggestion
ORIGIN_SIGNAL_PATTERN = re.compile(r"^unblock:(g-\d+-\d+)\s*$")
# Title pattern: "Unblock: <verb> for <parent-id>"
TITLE_FOR_PATTERN = re.compile(r"\bfor\s+(g-\d+-\d+)\b")
# Bare goal-id check
GOAL_ID_PATTERN = re.compile(r"^g-\d+-\d+$")

# Parent states that imply "no Unblock action is needed"
#  (zeta allowlist audit D1): synced to the SSOT
# aspirations.TERMINAL_GOAL_STATUSES. This is the mirror-source the two
# insight-trigger scripts reference; it carried the identical drift (missing
# expired+decomposed, bogus archived). Parity enforced by
# tests/test_terminal_goal_states_parity.py.
TERMINAL_STATES = {"completed", "skipped", "expired", "decomposed", "superseded"}


def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. Mirrors parent-supersession-sweep convention."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "unblock-parent-status-sweep-metrics.jsonl"


def _append_metric(path, record):
    """Append one metric record. Fail-open by design."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[unblock-parent-status-sweep] WARN: metrics append failed: {e}",
              file=sys.stderr)


def _run(argv, input_text=None):
    result = subprocess.run(argv, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.returncode, result.stdout, result.stderr


def _py(args, input_text=None):
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(source, raw):
    """-tolerant decode for daemon aspirations_read body.

    Mirrors consolidation-health.py::_tolerant_decode (lines 112-172,
    landed via g-115-796) and parent-supersession-sweep::_tolerant_decode
    (A4), adapted for unblock-parent-status-sweep's expected aggregate
    shapes: dict with "aspirations" key OR bare list.

    Contract per g-115-797-A5 / guard-383 / rb-774:
      - Empty / whitespace-only body: return None (caller maps to []).
      - Valid JSON (list OR dict with "aspirations" key): return as-is.
      - Valid prefix + trailing garbage (g-115-766 shape): raw_decode
        returns the prefix; recovery is NOT a source error.
      - JSONDecodeError: ONE stderr diagnostic + sys.exit(1).
      - Non-dict-and-non-list aggregate (e.g. string, number): stderr +
        sys.exit(1).
    guard-383 mandates source errors are FATAL for the N>=2 aggregator
    pattern (_read_aspirations is called once for "world" and once for
    "agent" then merged at line 263-264); returning [] on corruption
    would poison the merged aggregate with a complete-looking lie.
    """
    stripped = (raw or "").lstrip()
    if not stripped:
        return None  # genuinely empty queue — valid state, not source error
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"unblock-parent-status-sweep: {source} JSONDecodeError ({exc}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: source error is fatal
    if not isinstance(obj, (dict, list)):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"unblock-parent-status-sweep: {source} non-dict-and-non-list "
            f"aggregate (type={type(obj).__name__}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: corrupt aggregate shape
    return obj


def _read_aspirations(source):
    """Return list of (aspiration_dict, source_str) tuples.

    Uses _rt.aspirations_read (daemon client) — the aspirations.py read CLI
    was deleted in the 2026-05-14 cutover. Parse path is g-115-766-tolerant
    via _tolerant_decode — see that helper for the contract.
    """
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        # guard-383: source error is FATAL for the N>=2-source aggregator
        # pattern (lines 263-264 merge "world" + "agent"). A silent [] would
        # poison the merged aggregate with a complete-looking lie.
        print(f"unblock-parent-status-sweep: {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _tolerant_decode(source, raw)
    if data is None:
        return []
    aspirations = data.get("aspirations") if isinstance(data, dict) else data
    return [(asp, source) for asp in (aspirations or [])]


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", ""))
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _is_unblock_goal(g):
    return bool(UNBLOCK_TITLE_PATTERN.match(g.get("title", "") or ""))


def _parse_parent_id(g):
    """Extract parent goal-id from Layer D shape. Returns goal-id or None.

    Priority order (matches Layer D capability-gate emission):
      1. origin_signal "unblock:<g-id>" — exact form
      2. Title "Unblock: <verb> for <g-id>" — Layer D canonical title
      3. discovered_by "<g-id>" — legacy/manual unblock
    """
    os_ = (g.get("origin_signal") or "").strip()
    m = ORIGIN_SIGNAL_PATTERN.match(os_)
    if m:
        return m.group(1)
    title = g.get("title") or ""
    mm = TITLE_FOR_PATTERN.search(title)
    if mm:
        return mm.group(1)
    db = (g.get("discovered_by") or "").strip()
    if GOAL_ID_PATTERN.match(db):
        return db
    return None


def _build_status_index(all_aspirations):
    """Return {goal_id: status} across world + agent active queues."""
    idx = {}
    for asp, _src in all_aspirations:
        for g in (asp.get("goals") or []):
            gid = g.get("id")
            if gid:
                idx[gid] = g.get("status")
    return idx


def _mark_skipped(source, goal_id, parent_id, parent_status):
    """Mark Unblock goal as skipped with parent-resolved outcome_note.

    INVARIANT: uses sys.executable directly. Same rationale as
    parent-supersession-sweep._mark_superseded — bash on Windows can resolve
    to WSL bash.exe with surprising PATH semantics; aspirations-update-goal.sh
    just shells aspirations.py with the same args.
    """
    note = (f"parent resolved without action needed "
            f"(parent_id={parent_id}, parent.status={parent_status})")
    rc1, _, err1 = _py([str(SCRIPT_DIR / "aspirations.py"),
                        "--source", source, "update-goal",
                        goal_id, "outcome_note", note])
    if rc1 != 0:
        print(f"[unblock-parent-status-sweep] update outcome_note rc={rc1}: {err1.strip()}",
              file=sys.stderr)
        return False
    rc2, _, err2 = _py([str(SCRIPT_DIR / "aspirations.py"),
                        "--source", source, "update-goal",
                        goal_id, "status", "skipped"])
    if rc2 != 0:
        print(f"[unblock-parent-status-sweep] update status rc={rc2}: {err2.strip()}",
              file=sys.stderr)
        return False
    return True


def _is_already_swept(g):
    """Idempotency check: skip if outcome_note already names the sweep."""
    note = (g.get("outcome_note") or "")
    return note.startswith("parent resolved without action needed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=0.0,
                    help=("Minimum Unblock age before consideration "
                          "(default 0 — fire immediately when parent state "
                          "becomes terminal)."))
    ap.add_argument("--apply", action="store_true",
                    help=("Mark candidates as skipped with parent-resolved "
                          "outcome_note (default: report only)."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log. Default: "
                          "<WORLD_PATH>/unblock-parent-status-sweep-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()

    metrics_path = _resolve_metrics_log(args.metrics_log)

    all_aspirations = (_read_aspirations("world")
                       + _read_aspirations("agent"))
    status_idx = _build_status_index(all_aspirations)

    scanned = 0
    eligible = 0
    candidates = []
    applied = 0
    details = []

    for asp, source in all_aspirations:
        if asp.get("status") and asp["status"] != "active":
            continue
        for g in (asp.get("goals") or []):
            if not _is_unblock_goal(g):
                continue
            scanned += 1
            if g.get("status") not in ("pending", "in-progress"):
                continue
            if _is_already_swept(g):
                # Idempotent: already swept once, leave alone
                continue
            parent_id = _parse_parent_id(g)
            if parent_id is None:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "action": "skipped",
                    "reason": "parent_id not parseable from origin_signal/title/discovered_by",
                })
                continue
            ref_ts = (g.get("created_at") or g.get("defer_reason_set_at")
                      or g.get("started"))
            age_h = _age_hours(ref_ts)
            if age_h is None or age_h < args.max_age_hours:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "parent_id": parent_id,
                    "age_hours": (round(age_h, 1) if age_h is not None else None),
                    "action": "skipped",
                    "reason": f"age {age_h} below threshold {args.max_age_hours}h",
                })
                continue
            eligible += 1
            # Parent missing from active scan → archived (it WAS active before
            # being archived; absence is the terminal signal).
            parent_status = status_idx.get(parent_id, "archived")
            if parent_status not in TERMINAL_STATES:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "parent_id": parent_id,
                    "parent_status": parent_status,
                    "age_hours": round(age_h, 1),
                    "action": "skipped",
                    "reason": (f"parent not in terminal state "
                               f"(parent.status={parent_status})"),
                })
                continue
            entry = {
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "source": source,
                "parent_id": parent_id,
                "parent_status": parent_status,
                "age_hours": round(age_h, 1),
                "title": g.get("title", ""),
                "action": "would_mark",
            }
            candidates.append({
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "parent_id": parent_id,
                "parent_status": parent_status,
            })
            if args.apply:
                ok = _mark_skipped(source, g.get("id"), parent_id, parent_status)
                entry["action"] = "marked" if ok else "mark_failed"
                if ok:
                    applied += 1
                    _append_metric(metrics_path, {
                        "type": "unblock_parent_resolved",
                        "timestamp": dt.datetime.now().isoformat(
                            timespec="seconds"),
                        "goal_id": g.get("id"),
                        "source": source,
                        "aspiration_id": asp.get("id"),
                        "parent_id": parent_id,
                        "parent_status": parent_status,
                        "age_hours_at_mark": round(age_h, 2),
                        "agent": os.environ.get("MIND_AGENT", "") or None,
                    })
            details.append(entry)

    _append_metric(metrics_path, {
        "type": "run_summary",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "scanned": scanned,
        "eligible": eligible,
        "candidates": len(candidates),
        "applied": applied,
        "mode": "apply" if args.apply else "report",
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "candidates": candidates,
        "applied": applied,
        "details": details,
    }

    if args.output == "human":
        print(f"unblock-parent-status-sweep: scanned={scanned} "
              f"eligible={eligible} candidates={len(candidates)} "
              f"applied={applied} mode={'apply' if args.apply else 'report'}")
        for c in candidates:
            print(f"  {c['goal_id']} → parent {c['parent_id']} "
                  f"(status={c['parent_status']})")
    else:
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
