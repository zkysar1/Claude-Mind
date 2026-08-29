#!/usr/bin/env python3
"""closed-against-own-note-check.py — REPORT-ONLY detector ().

Flags TERMINAL goals whose own outcome_note/progress_note asserts they are NOT done.
No apply path: a wrong reopen is cheap, a wrong auto-close is not, and reopening a
noted goal drops it straight into the completed-not-closed population. Classifier and
its rationale: core/scripts/closed_against_own_note.py.

ALWAYS PRINTS THE DENOMINATOR (guard-2298, and the goal spec asks for it by name): the
scanned count, the how-many-carry-a-note count, and the flagged count, per status. A
flagged count without its denominator cannot distinguish a clean store from a query
that returned nothing.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from closed_against_own_note import TERMINAL_STATUSES, classify_goal  # noqa: E402


def load_status(status: str, timeout: int) -> List[Dict[str, Any]]:
    """One status via the daemon-routed query. An unreadable half is NOT an empty half."""
    from _paths import CORE_ROOT, PROJECT_ROOT
    from _runtime_bash import bash_cmd  # resolved bash, posix path (guard-581)
    script = os.path.join(str(CORE_ROOT), "scripts", "aspirations-query.sh")
    proc = subprocess.run(
        bash_cmd(script, "--goal-status", status, "--full"),
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(
            f"REFUSING: aspirations-query.sh --goal-status {status} --full failed "
            f"(rc={proc.returncode}, bytes={len(proc.stdout)}). An unreadable store is "
            f"NOT an empty one (guard-2298).\nstderr tail: {proc.stderr[-500:]}"
        )
    data = json.loads(proc.stdout)
    if not isinstance(data, list):
        raise SystemExit(f"REFUSING: {status} query returned a non-list; shape changed?")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--statuses", default=",".join(TERMINAL_STATUSES),
                    help="comma-separated terminal statuses to scan")
    ap.add_argument("--min-confidence", default="medium",
                    choices=["low", "medium", "high"],
                    help="report rows at or above this confidence (default medium)")
    ap.add_argument("--limit", type=int, default=25, help="max rows to print")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    order = ["low", "medium", "high"]
    floor = order.index(args.min_confidence)
    per_status: Dict[str, Dict[str, int]] = {}
    flagged: List[Dict[str, Any]] = []

    for status in [s.strip() for s in args.statuses.split(",") if s.strip()]:
        rows = load_status(status, args.timeout)
        noted = sum(1 for g in rows if g.get("outcome_note") or g.get("progress_note"))
        hits = [c for c in (classify_goal(g) for g in rows) if c]
        kept = [c for c in hits if order.index(c["confidence"]) >= floor]
        per_status[status] = {"scanned": len(rows), "with_note": noted,
                              "any_marker": len(hits), "flagged": len(kept)}
        flagged.extend(kept)

    flagged.sort(key=lambda c: (-order.index(c["confidence"]), c["goal_id"] or ""))
    result = {"population": per_status, "min_confidence": args.min_confidence,
              "flagged_total": len(flagged), "flagged": flagged[: args.limit],
              "truncated": max(0, len(flagged) - args.limit), "apply_path": None}

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    for st, p in per_status.items():
        print(f"[closed-against-own-note] {st}: scanned={p['scanned']} "
              f"with_note={p['with_note']} any_marker={p['any_marker']} "
              f"flagged(>={args.min_confidence})={p['flagged']}")
    print(f"[closed-against-own-note] TOTAL flagged={len(flagged)} "
          f"(report-only — no apply path by design)")
    for c in flagged[: args.limit]:
        top = max(sum(c["fields"].values(), []),
                  key=lambda h: (h["strength"] == "strong", h["position"] == "head",
                                 not h["in_quotes"]))
        print(f"  [{c['confidence']:>6}] {c['goal_id']:<14} {c['status']:<10} {c['title']}")
        print(f"           {top['label']} @{top['position']}"
              f"{' (QUOTED)' if top['in_quotes'] else ''}: …{top['context']}…")
    if result["truncated"]:
        print(f"  … {result['truncated']} more not shown (--limit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
