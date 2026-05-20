"""
Score-priority mismatch detector for /priority-review (g-244-19).

Detects aspirations whose aggregate score persistently exceeds the median score
of HIGH-priority aspirations. Persistent = 3+ consecutive runs. Either signals
a priority-field mis-set OR a score-formula over-weighting; user decides which.

Stdin (JSON): list of {asp_id, priority, score} for ALL active aspirations
              (both world and agent-local). Each `score` is the aggregate
              eligible-goal score from /priority-review Phase 1 step 3.

Stdout (JSON): {
  "flagged": [
    {asp_id, run_count, score, high_median, last_seen}
  ],
  "high_median": float | null,
  "high_count": int,
  "history_path": str
}

Exit codes:
  0 — ran successfully (flagged may be empty)
  1 — fatal error (stdin parse failure, history-file write failure)

History file: meta/priority-review-mismatch-history.yaml
Schema:
  asp-id:
    run_count: int             # consecutive runs above HIGH-median
    last_observed_score: float
    last_high_median: float
    last_seen: ISO timestamp

Reset rule: when an aspiration's score drops at-or-below HIGH-median, OR its
priority becomes HIGH, OR it becomes inactive — its history entry is purged.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

try:
    from _paths import META_DIR  # type: ignore
except ImportError:
    sys.exit("priority-review-mismatch: failed to import _paths")
from _fileops import locked_modify_yaml  # type: ignore


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"priority-review-mismatch: bad stdin JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, list):
        print("priority-review-mismatch: stdin must be a JSON array", file=sys.stderr)
        return 1

    history_path = Path(META_DIR) / "priority-review-mismatch-history.yaml"

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    active_ids = {entry.get("asp_id") for entry in payload if entry.get("asp_id")}
    high_scores: list[float] = []
    for entry in payload:
        if entry.get("priority") == "HIGH":
            try:
                high_scores.append(float(entry.get("score", 0.0)))
            except (TypeError, ValueError):
                pass

    high_median = statistics.median(high_scores) if len(high_scores) >= 3 else None

    flagged: list[dict] = []

    def _modify(history: dict) -> dict:
        # Coerce non-dict baseline into dict (matches old _load_history fallback).
        if not isinstance(history, dict):
            history = {}
        for entry in payload:
            asp_id = entry.get("asp_id")
            if not asp_id:
                continue
            priority = entry.get("priority")
            try:
                score = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0

            if priority == "HIGH":
                history.pop(asp_id, None)
                continue
            if high_median is None:
                continue
            if score > high_median:
                existing = history.get(asp_id, {}) or {}
                run_count = int(existing.get("run_count", 0)) + 1
                history[asp_id] = {
                    "run_count": run_count,
                    "last_observed_score": round(score, 2),
                    "last_high_median": round(high_median, 2),
                    "last_seen": now_iso,
                }
                if run_count >= 3:
                    flagged.append({
                        "asp_id": asp_id,
                        "run_count": run_count,
                        "score": round(score, 2),
                        "high_median": round(high_median, 2),
                        "last_seen": now_iso,
                    })
            else:
                history.pop(asp_id, None)

        for stale_id in [aid for aid in history if aid not in active_ids]:
            history.pop(stale_id)

        return history

    try:
        # : locked RMW prevents concurrent /priority-review runs from
        # last-writer-wins clobbering the consecutive-run counter. Helper holds
        # the lock across read+modify+write — see core/scripts/_fileops.py:341.
        locked_modify_yaml(history_path, _modify, initial={})
    except OSError as exc:
        print(f"priority-review-mismatch: failed to save history: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "flagged": flagged,
        "high_median": round(high_median, 2) if high_median is not None else None,
        "high_count": len(high_scores),
        "history_path": str(history_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
