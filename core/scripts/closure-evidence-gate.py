#!/usr/bin/env python3
"""closure-evidence-gate.py — refuse a completed close whose outcome rows are not evidenced ().

CLI over gates/closure_evidence.py, which holds the format, the checks and the
measured incident behind them. iteration-close.sh do_verify runs this before
the status write, for every completed close by every agent and Body.
aspirations-verify Q1 runs the same command earlier, so the closer sees a
refusal while it still has the evidence in hand.

WHICH NOTE IT READS. It reads the note that will be the closure evidence once
this close lands:
  1. --outcome-note-file, if given: it rides the status write and REPLACES the
     record's note (the daemon's companion-note path, g-358-36);
  2. else the record's outcome_note: closure-evidence-write never clobbers a
     one-shot goal's note, so a note already there (a worker's Phase 3.9) is
     the one that stays;
  3. else the summary (--summary-file, or stdin with --summary-stdin): do_verify
     writes it after the status write when the record has none.
Checking any other note would check a text that never lands.

THE STORE. A governed path is probed with the storage backend's stat, the
same call `backend-cat.sh head` makes. Which paths are machine-local (session
scratch, temp, .history) comes from the backend's own _machine_local policy, so
this gate and the sync walk agree on what the store can ever hold.

THE RECORD. It comes from the per-goal aspirations-query.sh projection, and
falls back to the whole-aspiration aspirations-read.sh when the query does not
return exactly one record. load_goal says why, with the measured latency.

rc: 0 = pass / noop / override / gate error (fail-open, guard-142).  3 = REFUSED.
Never 1: Python exits 1 on any uncaught exception, so a refusal on 1 would be
indistinguishable from a crash or an unimportable module, and the caller would
refuse every close whenever this gate cannot run (guard-5430). do_verify
refuses only on 3 and warns and proceeds on any other non-zero rc.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# No fallbacks on these imports (guard-391): one that fails crashes the gate
# with rc 1, which do_verify reports as a fault and proceeds past.
from gates.closure_evidence import evaluate, refusal_text  # noqa: E402
from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR, agents_root  # noqa: E402
from _gate_log import log as _gate_log  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  guard-580/581: never a bare "bash"

GATE_ID = "closure-evidence-gate"
REFUSED = 3


def _run_json(script: str, *args: str):
    try:
        proc = subprocess.run(bash_cmd(SCRIPT_DIR / script, *args), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
        return json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None
    except Exception:
        return None


def load_goal(goal_id: str, source: str) -> dict:
    """The goal record, or {} (fail-open).

    The per-goal query first: 2.1 s against 45.6 s for reading the whole
    aspiration, measured on an asp-115 goal, and this runs on every completed
    close. Its projection carries every field read here (outcome_note,
    verification, recurring, participants) whenever the record has them, which
    was checked against guard-1755. It is union-only (both queues), so when an
    id matches in both, the source-exact aspiration read settles it."""
    if not re.match(r"^g-\d+-", goal_id or ""):
        return {}
    recs = _run_json("aspirations-query.sh", "--goal-field", "id", goal_id, "--full")
    if isinstance(recs, list) and len(recs) == 1 and isinstance(recs[0], dict):
        return recs[0]
    data = _run_json("aspirations-read.sh", "--source", source,
                     "--id", "asp-" + goal_id.split("-")[1])
    rec = data if isinstance(data, dict) else (data[0] if isinstance(data, list) and data else None)
    for g in (rec or {}).get("goals") or []:
        if isinstance(g, dict) and g.get("id") == goal_id:
            return g
    return {}


def pick_note(goal: dict, outcome_note_file: str | None, summary: str) -> tuple[str, str]:
    if outcome_note_file:
        return Path(outcome_note_file).read_text(encoding="utf-8", errors="replace"), \
            f"--outcome-note-file {outcome_note_file} (it replaces the record's note)"
    record = str(goal.get("outcome_note") or "")
    if record.strip():
        return record, ("the record's outcome_note. It stays, because a close never "
                        "overwrites it; a --summary/--summary-file would not be written")
    if summary.strip():
        return summary, "the close's --summary text (the record has no outcome_note yet)"
    return "", "nothing: the record has no outcome_note and the close passed no summary"


def _roots() -> dict:
    agents = None
    try:
        agents = agents_root()
    except Exception:
        agents = None
    return {"project": Path(PROJECT_ROOT), "world": WORLD_DIR, "meta": META_DIR,
            "agents": agents, "msys": os.name == "nt"}


def store_probe():
    """probe(path, kind) over the real backend. Framework paths and directories
    are local-only; governed files go to the store unless machine-local."""
    try:
        from storage_backend import get_backend
        backend = get_backend()
    except Exception:
        backend = None
    machine_local = getattr(backend, "_machine_local", None)

    def probe(path: Path, kind: str) -> dict:
        local = path.exists()
        info = {"local": local, "is_dir": path.is_dir(), "machine_local": False, "store": local}
        if kind != "governed" or info["is_dir"]:
            return info
        if backend is None:
            info["store"] = None  # no backend: the store is unknown, never "absent"
            return info
        if machine_local is not None and machine_local(path):
            info["machine_local"] = True
            return info
        try:
            info["store"] = backend.stat(path) is not None
        except Exception:
            info["store"] = None  # store unreadable: a warning, never a refusal
        return info
    return probe


def _log_override(payload: dict) -> None:
    # Resolved at call time with a test seam, the close-review-gate lesson: a
    # test that exercises the override must not append to the production ledger.
    root = os.environ.get("CLOSURE_EVIDENCE_LEDGER_DIR", "").strip() or WORLD_DIR
    if root is None:
        return
    ledger = Path(root) / "closure-evidence-overrides.jsonl"
    try:
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"{GATE_ID}: override ledger write failed: {e}", file=sys.stderr)


def _emit(decision: str, goal_id: str, override: str | None = None, **fields) -> None:
    try:
        _gate_log(GATE_ID, decision, caller="iteration-close.sh do_verify",
                  trigger_matched=(decision in ("block", "override")),
                  payload={"goal_id": goal_id}, override_reason=override)
    except Exception:
        pass
    if override:
        _log_override({"ts": datetime.now().isoformat(timespec="seconds"), "gate": GATE_ID,
                       "goal_id": goal_id, "justification": override,
                       "agent": os.environ.get("MIND_AGENT"),
                       "session_id": os.environ.get("MIND_SID"),
                       "problems": fields.get("problems")})
    if decision == "error":  # do_verify logs stdout, so say it where the closer reads
        print(f"{GATE_ID}: {goal_id} closure evidence NOT checked, fail-open: "
              f"{fields.get('reason')}", file=sys.stderr)
    # ASCII-escaped: a cp1252 stdout cannot encode every character an outcome
    # excerpt carries, and a raise here would crash the gate out of its verdict.
    print(json.dumps({"gate": GATE_ID, "decision": decision, "goal_id": goal_id, **fields},
                     ensure_ascii=True))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal", required=True)
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--outcome-note-file", default=None)
    ap.add_argument("--summary-file", default=None)
    ap.add_argument("--summary-stdin", action="store_true",
                    help="read the close's summary text from stdin (do_verify's path)")
    ap.add_argument("--override", default=None,
                    help="justification; turns a refusal into a logged pass")
    ap.add_argument("--goal-json", default=None, help="goal record JSON path (tests)")
    args = ap.parse_args(argv)
    # Drain stdin FIRST: do_verify pipes the summary in, and an early return
    # below must not leave the writer on a closed pipe. Bytes decoded as UTF-8:
    # under a cp1252 locale sys.stdin.read() raised on a curly quote (byte 0x9d),
    # and the uncaught exit 1 read as a REFUSAL of a note that passes.
    stdin_summary = (sys.stdin.buffer.read().decode("utf-8", "replace")
                     if args.summary_stdin else "")

    if args.goal_json:
        try:
            goal = json.loads(Path(args.goal_json).read_text(encoding="utf-8"))
        except Exception:
            goal = {}
    else:
        goal = load_goal(args.goal, args.source)
    if not goal:
        _emit("error", args.goal, reason="goal record unavailable, fail-open")
        return 0

    try:
        summary = stdin_summary
        if args.summary_file:
            summary = Path(args.summary_file).read_text(encoding="utf-8", errors="replace")
        note, note_source = pick_note(goal, args.outcome_note_file, summary)
        result = evaluate(goal, note, roots=_roots(), probe=store_probe())
    except Exception as e:  # our own fault: never a refusal (guard-142)
        _emit("error", args.goal, reason=f"gate fault: {e}")
        return 0

    fields = {"note_source": note_source, "problems": result.get("problems"),
              "warnings": result.get("warnings"), "reason": result.get("reason")}
    if result["decision"] != "block":
        _emit(result["decision"], args.goal, **fields)
        return 0
    if args.override:
        _emit("override", args.goal, override=args.override, **fields)
        return 0
    try:
        text = refusal_text(args.goal, result, note_source)
    except Exception as e:  # guard-3803: a bug in the message must not cancel the refusal
        text = f"{GATE_ID}: REFUSED. {args.goal}: {result.get('problems')} (message failed: {e})"
    print(text, file=sys.stderr)
    _emit("block", args.goal, **fields)
    return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
