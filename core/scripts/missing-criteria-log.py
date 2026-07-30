#!/usr/bin/env python3
"""Append uncovered generated-checklist failures to meta/missing-verification-criteria.jsonl.

g-306-03 / BRD Gap 15 — "TICKing All the Boxes" (2410.03608). aspirations-verify
Q1.5 generates a 5-10 item yes/no checklist from a goal's title/description/
action, evaluates each against the produced artifact, and surfaces *uncovered*
failures: checklist items that FAILED **and** were not represented in the goal's
own verification.outcomes / verification.checks. Those uncovered items are the
signal — the goal TEMPLATE should have declared them. This sink accumulates them
for later goal-template improvement.

DETECTIVE / telemetry only. This script never mutates goal or aspiration state;
it appends one JSONL line per verify-with-uncovered-items. A covered failure (a
checklist item whose property the goal's OWN criteria declared) is a real verify
failure and is handled by aspirations-verify Q1.5 directly — it is NOT logged
here; only the *uncovered* gaps are.

Reads a JSON record from stdin:
  {
    "goal_id": "g-306-03",             # required, non-empty string
    "items":   ["...", "..."],         # required, non-empty list of uncovered-item strings
    "source":  "world" | "agent",      # optional
    "category": "framework-patterns",  # optional
    "artifact": "<path or description>",# optional — what Q1 verified
    "note":     "..."                  # optional
  }

Stamps `id` + `logged_at` (local system time per the project ISO-8601 rule),
appends one locked line to $META_DIR/missing-verification-criteria.jsonl, and
prints the written record. Exit 0 on success; 1 on bad input or unresolved
META_DIR. Locking is fail-open: telemetry must never block the verify phase.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_FILENAME = "missing-verification-criteria.jsonl"
_OPTIONAL_STR_FIELDS = ("source", "category", "artifact", "note")


def build_record(payload, now):
    """Validate `payload` and return the JSONL record (pure — no I/O).

    Raises ValueError on any contract violation so `main` can map it to exit 1.
    `now` is injected (datetime) so the stamp is deterministic under test.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    goal_id = payload.get("goal_id")
    if not isinstance(goal_id, str) or not goal_id.strip():
        raise ValueError("goal_id is required (non-empty string)")
    goal_id = goal_id.strip()

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items is required (non-empty list of strings)")
    clean_items = [str(i).strip() for i in items if str(i).strip()]
    if not clean_items:
        raise ValueError("items must contain at least one non-empty string")

    record = {
        "id": f"mvc-{goal_id}-{now.strftime('%Y%m%d%H%M%S')}",
        "logged_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "goal_id": goal_id,
        "items": clean_items,
    }
    for key in _OPTIONAL_STR_FIELDS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            record[key] = val.strip()
    return record


def _acquire(lock_path, timeout=10.0, stale_seconds=30.0):
    """Self-contained atomic O_CREAT|O_EXCL lock with stale-break.

    Deliberately does NOT import _fileops — this keeps the appender hermetic
    (no _paths/backend init at import time, so the unit tests need no agent
    binding) and FAIL-OPEN: on timeout it returns without the lock rather than
    losing a telemetry line. A lost-lock race here at worst interleaves two
    rare verify-phase appends; it never corrupts goal state.
    """
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > stale_seconds:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass  # lock vanished between stat and unlink — retry the create
            if time.time() > deadline:
                return  # fail-open: proceed without the lock
            time.sleep(0.05)


def _release(lock_path):
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def append_record(record, path):
    """Append `record` as one JSONL line to `path` under a fail-open lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    lock_path = path.with_name(path.name + ".lock")
    _acquire(lock_path)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    finally:
        _release(lock_path)


def _log_path():
    """Resolve $META_DIR/missing-verification-criteria.jsonl (lazy _paths import)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _paths import META_DIR  # noqa: E402 — lazy so build/append stay hermetic

    if META_DIR is None:
        raise RuntimeError(
            "META_DIR unresolved — set MIND_META env or META_PATH in "
            "agents/<agent>/local-paths.conf"
        )
    return Path(META_DIR) / LOG_FILENAME


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"missing-criteria-log: invalid JSON on stdin: {exc}\n")
        return 1
    try:
        record = build_record(payload, datetime.now())
    except ValueError as exc:
        sys.stderr.write(f"missing-criteria-log: {exc}\n")
        return 1
    try:
        path = _log_path()
    except RuntimeError as exc:
        sys.stderr.write(f"missing-criteria-log: {exc}\n")
        return 1
    append_record(record, path)
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
