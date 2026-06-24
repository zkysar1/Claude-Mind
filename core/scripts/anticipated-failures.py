#!/usr/bin/env python3
"""Anticipated-failures store engine (Phase 3.96 anticipatory reflection).

g-306-22 Phase B. Per-session, per-goal store of anticipated failure modes for
DEEP goals, plus their post-execution outcome. The authoritative design spec is
`world/conventions/anticipated-failures.md` (section 2 = this store's schema);
keep the two in sync.

LOCAL, not a daemon endpoint. The store is machine-local, session-scoped, and
single-writer (the running agent), touched only on deep goals (rare). It uses
`_fileops` file-locking directly rather than a daemon hop -- unlike the
high-frequency `wm-*.sh` family, which routes through the daemon for its
hot-path consistency needs. If cross-agent sharing is ever required, promote to
a daemon endpoint per the daemon-only contract (.claude/rules/no-python-cli-fallback.md).

Store: `agents/<agent>/session/anticipated-failures.jsonl` -- one record per
deep goal that ran Phase 3.96. JSONL (not YAML): lifecycle records belong in
JSONL per CLAUDE.md "Universal Conventions", and `_fileops` provides atomic
locked append/modify primitives for it (locking + history + changelog +
corruption recovery).

Subcommands (one thin .sh wrapper each):
  add     -- stdin JSON record; validate + dedup on goal_id + append; outcome=null.
  read    -- argv goal_id; print the matching record JSON (or `null`); exit 0.
  update  -- argv goal_id; stdin JSON outcome dict; set record.outcome; exit 2 if not found.

Record schema (add input):
  {goal_id, aspiration_id?, category?, estimated_depth?, anticipated_at?,
   anticipated: [{id, mode, why, signal, mitigation?}, ...]}   # 1..4 modes
Outcome schema (update input, filled by Phase 4.1):
  {executed_at, errors_observed: [...], hits: [...], misses: [...],
   surprises: [...], anticipation_score, clean_success}
The anticipation_score itself is computed by the Phase D reflect-on-self
consumer (design section 5), NOT here -- this store only persists what it is
given.

Exit codes: 0 success, 1 invalid input / validation error, 2 goal_id not found
(update only).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _paths import AGENT_NAME, agent_state_dir  # type: ignore  # noqa: E402
import _fileops  # type: ignore  # noqa: E402

STORE_FILENAME = "anticipated-failures.jsonl"

# Design cap: 1..4 anticipated failure modes per deep goal (anticipated-failures.md section 3).
MAX_ANTICIPATED = 4
REQUIRED_ANTICIPATED_FIELDS = ("id", "mode", "why", "signal")  # mitigation optional


def store_path() -> Path:
    """Default store path for the bound agent (CLI path)."""
    return agent_state_dir(AGENT_NAME) / STORE_FILENAME


def _now_iso() -> str:
    # Local system time per CLAUDE.md naming rules (never UTC).
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def validate_record(rec) -> list:
    """Return a list of human-readable validation errors ([] when valid)."""
    if not isinstance(rec, dict):
        return ["record must be a JSON object"]
    errs = []
    gid = rec.get("goal_id")
    if not gid or not isinstance(gid, str):
        errs.append("goal_id (non-empty string) is required")
    ant = rec.get("anticipated")
    if not isinstance(ant, list) or not ant:
        errs.append("anticipated must be a non-empty list (1..4 failure modes)")
    elif len(ant) > MAX_ANTICIPATED:
        errs.append(
            "anticipated must have at most %d modes (design cap)" % MAX_ANTICIPATED
        )
    else:
        for i, item in enumerate(ant):
            if not isinstance(item, dict):
                errs.append("anticipated[%d] must be an object" % i)
                continue
            missing = [k for k in REQUIRED_ANTICIPATED_FIELDS if not item.get(k)]
            if missing:
                errs.append(
                    "anticipated[%d] missing required field(s): %s" % (i, missing)
                )
    return errs


def add_entry(rec: dict, path=None) -> dict:
    """Validate + dedup-on-goal_id + append a record. Raises ValueError on
    invalid input or a duplicate goal_id (one record per deep goal)."""
    path = Path(path) if path else store_path()
    errs = validate_record(rec)
    if errs:
        raise ValueError("; ".join(errs))
    rec.setdefault("anticipated_at", _now_iso())
    rec.setdefault("outcome", None)
    gid = rec["goal_id"]
    state = {"dup": False}

    def _mod(items):
        if any(r.get("goal_id") == gid for r in items):
            state["dup"] = True
            return items  # one record per goal -- do not append a duplicate
        items.append(rec)
        return items

    _fileops.locked_modify_jsonl(path, _mod, initial=[])
    if state["dup"]:
        raise ValueError(
            "a record for goal_id %s already exists (one record per deep goal)" % gid
        )
    return rec


def read_entry(goal_id: str, path=None):
    """Return the record matching goal_id, or None when absent."""
    path = Path(path) if path else store_path()
    if not path.exists():
        return None
    items = _fileops.read_jsonl_with_recovery(path)
    for r in items:
        if r.get("goal_id") == goal_id:
            return r
    return None


def update_outcome(goal_id: str, outcome: dict, path=None) -> dict:
    """Set record.outcome for goal_id. Raises KeyError when goal_id is absent,
    ValueError when outcome is not an object."""
    path = Path(path) if path else store_path()
    if not isinstance(outcome, dict):
        raise ValueError("outcome must be a JSON object")
    state = {"rec": None}

    def _mod(items):
        for r in items:
            if r.get("goal_id") == goal_id:
                r["outcome"] = outcome
                state["rec"] = r
        return items

    _fileops.locked_modify_jsonl(path, _mod, initial=[])
    if state["rec"] is None:
        raise KeyError(goal_id)
    return state["rec"]


def _read_stdin_json():
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("empty stdin: expected a JSON object")
    return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="anticipated-failures")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add", help="stdin JSON record -> validate + dedup + append")
    pr = sub.add_parser("read", help="argv goal_id -> print record JSON or null")
    pr.add_argument("goal_id")
    pu = sub.add_parser("update", help="argv goal_id + stdin JSON outcome -> set outcome")
    pu.add_argument("goal_id")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "add":
            rec = _read_stdin_json()
            print(json.dumps(add_entry(rec)))
            return 0
        if args.cmd == "read":
            print(json.dumps(read_entry(args.goal_id)))  # 'null' when absent
            return 0
        if args.cmd == "update":
            outcome = _read_stdin_json()
            try:
                rec = update_outcome(args.goal_id, outcome)
            except KeyError:
                print(
                    json.dumps({"error": "not_found", "goal_id": args.goal_id}),
                    file=sys.stderr,
                )
                return 2
            print(json.dumps(rec))
            return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": "invalid_input", "detail": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
