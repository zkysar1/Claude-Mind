#!/usr/bin/env python3
"""worker-closure-audit.py — reducer-side sampling audit of worker closures ( item 2).

THE CONVERGENCE SPLIT REMOVES AN OUTSIDE READER; THIS PUTS ONE BACK. A worker Body
executes a unit and closes it. Until g-306-417 the reducer re-derived that judgment
from notes it did not write, for 50+ worker closures a day — the largest per-goal
reducer cost. Moving the judgment to the worker removes the cost AND removes the only
second pair of eyes, leaving a Body that grades its own homework. This module is the
compensating control: the reducer SAMPLES worker closures and records agreement or
disagreement, so self-grading drift is observable instead of silent.

BUILT BEFORE ITS PRODUCER, DELIBERATELY (rb-4452). worker-loop Phase 4a does not yet
invoke /aspirations-verify with scope=own-unit, so no closure yet carries an LLM
verdict field. Shipping the auditor first means the invariant CONSTRAINS that wiring's
design instead of being retrofitted onto whatever it happens to write — the same
ordering close-review-gate.py used against g-357-41. It is not speculative in the
meantime: every check below runs against fields that exist on worker closures TODAY
(measured 2026-09-03: 202 worker closures, 0 carrying any verdict field), so the audit
produces real signal now and strictly more once the verdict field lands.

REPORT-ONLY, ALWAYS. This never refuses, never mutates a goal, and never blocks a
completion review — rc is 0 unless the module itself faults. An audit that can wedge
the review it audits would be traded away the first time it misfired.

THE CHECKS ARE MEASURED INCIDENTS, NOT INVENTED HEURISTICS. Each maps to a guardrail
earned from a live failure, which is also why each carries a CONFIDENCE rather than a
flat pass/fail:

  empty_note                 guard-2852(a)  HIGH   closed with no record of what happened
  note_done_status_disagrees guard-2852(b)  HIGH   note says DONE, status disagrees
  remainder_language         guard-4007     MEDIUM closed on a batch; note says work remains
  criterion_unrun            guard-1968     LOW    criteria declared, no evidence they ran

Verdict is AGREE (nothing fired), DISAGREE (a HIGH fired — an objective contradiction
inside the record), or REVIEW (only MEDIUM/LOW fired — a signal for a human, not a
finding). The LOW check is a keyword heuristic over free prose and WILL over-report;
it is kept because a missed unverified close is the costly direction, and it is
confined to REVIEW so it can never manufacture a DISAGREE.

Sampling is EVERY HIGH-priority closure plus a deterministic fraction of the rest.
Deterministic (sha256 of the goal id, not random) so a re-run over the same aspiration
samples the same goals and two runs are comparable — a randomly-resampling auditor
produces a different denominator every pass and its trend line means nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_FRACTION = 0.25
REPORT_REL = "audit-reports/worker-closure-audit.jsonl"

# ─── check definitions ─────────────────────────────────────────────────────

# Work-remains language (guard-4007): a population-scoped goal closed on one batch.
REMAINDER_RE = re.compile(
    r"\b(\d+\s+(?:entries|items|records|goals|files|observations)?\s*remain(?:ing|s)?"
    r"|still\s+(?:undrained|pending|outstanding|remaining)"
    r"|not\s+cleared"
    r"|partial(?:ly)?\s+(?:complete|done|drained)"
    r"|remainder\b"
    r"|deliberately\s+NOT\s+cleared)",
    re.IGNORECASE,
)

# Note asserts completion (guard-2852(b)); paired with a status that disagrees.
DONE_RE = re.compile(
    r"\b(DONE\b|all\s+\d+\s+outcomes\s+met|complete[d]?\s+successfully"
    r"|landed\b|verified\s+complete)",
    re.IGNORECASE,
)

# Evidence that a declared criterion was actually exercised (guard-1968).
EVIDENCE_RE = re.compile(
    r"(\b\d+\s*/\s*\d+\b"          # 2/2, 46/46
    r"|\bexit\s*(?:code\s*)?0\b"
    r"|\brc=0\b"
    r"|\bpassed\b|\bgreen\b"
    r"|\btest_[a-z0-9_]+"
    r"|\[match\]"
    r"|\bmd5\b|\bsha256\b"
    r"|\bmeasured\b|\bprobed\b|\bverified\b|\bconfirmed\b)",
    re.IGNORECASE,
)

TERMINAL_OK = {"completed"}


def _note(goal: dict) -> str:
    """Every free-text field a closure can leave its account in."""
    parts = []
    for key in ("outcome_note", "outcome_notes", "progress_note", "key_finding", "notes"):
        v = goal.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(str(x) for x in v)
    return "\n".join(parts).strip()


def _declared_criteria(goal: dict) -> int:
    v = goal.get("verification") or {}
    if not isinstance(v, dict):
        return 0
    return len(v.get("outcomes") or []) + len(v.get("checks") or [])


def run_checks(goal: dict) -> list[dict]:
    """Return the checks that FIRED. Empty list == agreement."""
    fired = []
    note = _note(goal)
    status = (goal.get("status") or "").strip().lower()

    if len(note) < 40:
        fired.append({
            "check": "empty_note",
            "confidence": "high",
            "guardrail": "guard-2852",
            "detail": f"outcome_note is {len(note)} chars; the record carries no account "
                      f"of what was done",
        })

    if status not in TERMINAL_OK and DONE_RE.search(note):
        m = DONE_RE.search(note)
        fired.append({
            "check": "note_done_status_disagrees",
            "confidence": "high",
            "guardrail": "guard-2852",
            "detail": f"note asserts completion ({m.group(0)!r}) but status is {status!r} "
                      f"— the stranded-claim release class",
        })

    if status in TERMINAL_OK:
        m = REMAINDER_RE.search(note)
        if m:
            fired.append({
                "check": "remainder_language",
                "confidence": "medium",
                "guardrail": "guard-4007",
                "detail": f"closed completed while its own note says work remains "
                          f"({m.group(0)!r})",
            })

    if status in TERMINAL_OK and _declared_criteria(goal) > 0 and note:
        if not EVIDENCE_RE.search(note):
            fired.append({
                "check": "criterion_unrun",
                "confidence": "low",
                "guardrail": "guard-1968",
                "detail": f"{_declared_criteria(goal)} criteria declared but the note shows "
                          f"no evidence marker (counts, exit code, test name, measurement)",
            })

    return fired


def verdict_for(fired: list[dict]) -> str:
    if not fired:
        return "AGREE"
    if any(f["confidence"] == "high" for f in fired):
        return "DISAGREE"
    return "REVIEW"


# ─── sampling ──────────────────────────────────────────────────────────────

def is_worker_closure(goal: dict) -> bool:
    return (goal.get("completed_by_role") or "").strip().lower() == "worker"


def sampled(goal: dict, fraction: float) -> tuple[bool, str]:
    """Every HIGH, plus a deterministic fraction of the rest."""
    if (goal.get("priority") or "").strip().upper() == "HIGH":
        return True, "every-high"
    gid = str(goal.get("id") or goal.get("goal_id") or "")
    if not gid:
        return False, "no-id"
    bucket = int(hashlib.sha256(gid.encode("utf-8")).hexdigest()[:8], 16) % 1000
    if bucket < int(round(fraction * 1000)):
        return True, f"fraction<{fraction}>"
    return False, "not-sampled"


# ─── store access ──────────────────────────────────────────────────────────

def load_goals(asp_id: str, source: str) -> list[dict]:
    script = Path(__file__).resolve().parent / "aspirations-read.sh"
    if not script.is_file():
        return []
    try:
        # bash_cmd, NEVER a bare "bash" argv0 (guard-580): on win32 CreateProcess
        # searches System32 before PATH, so "bash" resolves to the WSL launcher and
        # hangs or dies against a dead LxssManager. Measured on this very script
        # 2026-09-03 — the hand-rolled argv returned an empty goal list that the
        # except below swallowed into a confident "0 worker closures" on an
        # aspiration holding 16. The script is bash_cmd's FIRST POSITIONAL, not a list.
        from _runtime_bash import bash_cmd  # type: ignore
        res = subprocess.run(
            bash_cmd(script, "--source", source, "--id", asp_id),
            capture_output=True, text=True, timeout=180,
        )
        if res.returncode != 0 or not res.stdout.strip():
            print(f"worker-closure-audit: store read failed rc={res.returncode} "
                  f"{res.stderr.strip()[:200]}", file=sys.stderr)
            return []
        data = json.loads(res.stdout)
    except Exception as exc:
        # LOUD, never silent: an unreadable store and an aspiration with no worker
        # closures are the same empty list downstream, and only one of them is news.
        print(f"worker-closure-audit: store read error {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []
    asp = data.get("aspiration") if isinstance(data, dict) else None
    asp = asp if isinstance(asp, dict) else (data if isinstance(data, dict) else {})
    goals = asp.get("goals")
    return goals if isinstance(goals, list) else []


def report_path() -> Path | None:
    try:
        from _paths import WORLD_DIR  # type: ignore
        if WORLD_DIR:
            return Path(WORLD_DIR) / REPORT_REL
    except Exception:
        pass
    # Only reached when _paths itself is unimportable (a bare checkout, a test
    # harness). NOT a silent alternative to the line above: WORLD_DIR is the
    # single source of truth and resolves these same vars through _absolutize.
    w = os.environ.get("MIND_WORLD") or os.environ.get("WORLD_PATH")
    return Path(w) / REPORT_REL if w else None


def emit(records: list[dict]) -> str:
    """Append audit rows to the world-scoped readable place. Never fatal."""
    path = report_path()
    if path is None:
        return "unwritten: world path unresolved"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from _fileops import locked_append_jsonl  # type: ignore
        for rec in records:
            locked_append_jsonl(str(path), rec)
        return f"appended {len(records)} row(s) -> {path}"
    except Exception as exc:
        return f"unwritten: {type(exc).__name__}: {exc}"


# ─── main ──────────────────────────────────────────────────────────────────

def audit(goals: list[dict], fraction: float, asp_id: str, reviewer: str) -> dict:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    closures = [g for g in goals if is_worker_closure(g)]
    rows, counts = [], {"AGREE": 0, "DISAGREE": 0, "REVIEW": 0}
    for g in closures:
        take, why = sampled(g, fraction)
        if not take:
            continue
        fired = run_checks(g)
        v = verdict_for(fired)
        counts[v] += 1
        rows.append({
            "audited_at": now,
            "aspiration": asp_id,
            "goal_id": g.get("id") or g.get("goal_id"),
            "title": (g.get("title") or "")[:160],
            "priority": g.get("priority"),
            "status": g.get("status"),
            "closed_by": g.get("completed_by"),
            "closed_by_sid": g.get("completed_by_sid"),
            "sample_reason": why,
            "verdict": v,
            "checks_fired": fired,
            "reviewer": reviewer,
        })
    return {
        "aspiration": asp_id,
        "goals_total": len(goals),
        "worker_closures": len(closures),
        "sampled": len(rows),
        "fraction": fraction,
        "counts": counts,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sampling audit of worker closures (report-only).")
    ap.add_argument("--asp", help="aspiration id to audit")
    ap.add_argument("--source", default="world", choices=["world", "agent"])
    ap.add_argument("--fraction", type=float, default=DEFAULT_FRACTION,
                    help=f"fraction of non-HIGH closures to sample (default {DEFAULT_FRACTION})")
    ap.add_argument("--goals-json", help="read goals from a JSON file instead of the store (tests)")
    ap.add_argument("--reviewer", default=os.environ.get("MIND_AGENT", "unknown"))
    ap.add_argument("--json", action="store_true", help="emit the full result as JSON")
    ap.add_argument("--dry-run", action="store_true", help="do not append to the report store")
    args = ap.parse_args()

    if not args.asp and not args.goals_json:
        print("worker-closure-audit: need --asp or --goals-json", file=sys.stderr)
        return 2

    if args.goals_json:
        try:
            raw = json.loads(Path(args.goals_json).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"worker-closure-audit: unreadable --goals-json: {exc}", file=sys.stderr)
            return 2
        goals = raw if isinstance(raw, list) else (raw.get("goals") or [])
        asp_id = args.asp or raw.get("id") if isinstance(raw, dict) else (args.asp or "(inline)")
    else:
        goals = load_goals(args.asp, args.source)
        asp_id = args.asp

    result = audit(goals, args.fraction, asp_id or "(unknown)", args.reviewer)

    if result["rows"] and not args.dry_run:
        result["emit"] = emit(result["rows"])
    elif args.dry_run:
        result["emit"] = "dry-run: not written"
    else:
        result["emit"] = "no sampled rows"

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    c = result["counts"]
    print(f"worker-closure-audit  asp={result['aspiration']}  "
          f"worker_closures={result['worker_closures']}  sampled={result['sampled']}")
    print(f"  AGREE={c['AGREE']}  DISAGREE={c['DISAGREE']}  REVIEW={c['REVIEW']}")
    for r in result["rows"]:
        if r["verdict"] == "AGREE":
            continue
        print(f"  [{r['verdict']}] {r['goal_id']} ({r['priority']}) {r['title'][:70]}")
        for f in r["checks_fired"]:
            print(f"      - {f['check']} [{f['confidence']}, {f['guardrail']}]: {f['detail']}")
    print(f"  {result['emit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
