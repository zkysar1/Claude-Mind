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

THE CARRIERS (g-375-162). Each NOT MET row's carrier is looked up with the
same per-goal query, and a lookup that fails is a warning (carrier_lookup). A
refusal's JSON line carries `remedy`, the fix commands the refusal prints, so
verify-preflight prints them as well.

THE ADVISORY (g-375-52). Whatever the verdict, the note it read is also
checked for session-scratch citations that lack their inline lines or a host.
The advisory goes to STDERR, because do_verify sends stdout to a log and lets
stderr through to the closer, and it is also recorded in the JSON line as
scratch_citations so its firing rate can be counted from that log. A check
that faulted is recorded as scratch_advisory_error, so a skip never counts as
quiet. It never changes the rc.

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
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# No fallbacks on these imports (guard-391): one that fails crashes the gate
# with rc 1, which do_verify reports as a fault and proceeds past.
from gates.closure_evidence import (  # noqa: E402
    advisory_text, evaluate, refusal_text, remedy_lines, scratch_citations)
from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR, SESSIONS_DIRNAME, agents_root  # noqa: E402
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


def carrier_lookup(carriers_json: str | None, goal_json: str | None):
    """status(goal_id) for the per-row carrier check (): the goal's
    status, None when no record has the id, and a raise when it cannot look,
    which the check turns into a warning. The per-goal query is union-only, so
    an id can match in both queues, and a live match in either counts: the
    residual-work gate reads both queues too. Where this box does not own the
    agent queue, which on a worker Body is every queue, the endpoint refuses an
    empty identity lookup as an unverified absence (agent_queue_unverified_empty,
    rc 1). So there a carrier id that matches nothing is a warning, while a
    record that comes back is judged by its status.

    --carriers-json is the test seam, {goal_id: status or null}, and an id it
    lacks raises. A --goal-json run without it looks nothing up, so no test
    reads the live store."""
    if carriers_json:
        table = json.loads(Path(carriers_json).read_text(encoding="utf-8"))

        def from_table(gid: str):
            if gid not in table:
                raise LookupError(f"{gid} is not in --carriers-json")
            return table[gid]
        return from_table
    if goal_json:
        return None
    cache: dict = {}

    def from_store(gid: str):
        if gid not in cache:
            recs = _run_json("aspirations-query.sh", "--goal-field", "id", gid, "--full")
            if not isinstance(recs, list):
                raise LookupError("no record list: a failed query, or an absence this box "
                                  "cannot verify")
            from gates.residual_work import ACTIVE_STATUSES
            found = [r.get("status") for r in recs if isinstance(r, dict)]
            cache[gid] = next((s for s in found if s in ACTIVE_STATUSES),
                              found[0] if found else None)
        return cache[gid]
    return from_store


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


def scratch_advisory(goal_id: str, note: str) -> tuple[list, str, str]:
    """(findings, stderr text, error) for the  advisory. Never raises:
    the verdict outranks it, and a crash here exits 1, which do_verify reads as
    a gate fault and proceeds past, so a refusal would be lost with it. The
    error is returned so the JSON line can tell a skipped check from a quiet
    one (guard-2421)."""
    try:
        host = socket.gethostname()
        found = scratch_citations(note, sessions_dirname=SESSIONS_DIRNAME, hostname=host)
        return found, (advisory_text(goal_id, found, host) if found else ""), ""
    except Exception as e:
        return [], f"{GATE_ID}: session-scratch advisory skipped ({e})", str(e)


def _advise(text: str) -> None:
    if text:
        print(text, file=sys.stderr)


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
    ap.add_argument("--carriers-json", default=None,
                    help="carrier statuses {goal_id: status|null} (tests); see carrier_lookup")
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
        result = evaluate(goal, note, roots=_roots(), probe=store_probe(),
                          carrier_status=carrier_lookup(args.carriers_json, args.goal_json))
    except Exception as e:  # our own fault: never a refusal (guard-142)
        _emit("error", args.goal, reason=f"gate fault: {e}")
        return 0

    fields = {"note_source": note_source, "problems": result.get("problems"),
              "warnings": result.get("warnings"), "reason": result.get("reason")}
    found, advice, advice_error = scratch_advisory(args.goal, note)
    if found:
        fields["scratch_citations"] = found
    if advice_error:
        fields["scratch_advisory_error"] = advice_error
    if result["decision"] != "block":
        _emit(result["decision"], args.goal, **fields)
        _advise(advice)
        return 0
    if args.override:
        _emit("override", args.goal, override=args.override, **fields)
        _advise(advice)
        return 0
    # pick_note's own test: with no --outcome-note-file, a record note is the one
    # checked, and only that note can be fixed by appending to it.
    stored = not args.outcome_note_file and bool(str(goal.get("outcome_note") or "").strip())
    try:
        fields["remedy"] = remedy_lines(args.goal, result, goal=goal, note=note, stored=stored,
                                        source=args.source)
    except Exception as e:  # the commands are help; their fault must not cost the refusal
        fields["remedy"], fields["remedy_error"] = [], str(e)
    remedy = fields["remedy"]
    try:
        text = refusal_text(args.goal, result, note_source, remedy=remedy, stored=stored)
    except Exception as e:  # guard-3803: a bug in the message must not cancel the refusal
        text = f"{GATE_ID}: REFUSED. {args.goal}: {result.get('problems')} (message failed: {e})"
    print(text, file=sys.stderr)
    _advise(advice)  # after the refusal, which must stay the first screen ()
    _emit("block", args.goal, **fields)
    return REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
