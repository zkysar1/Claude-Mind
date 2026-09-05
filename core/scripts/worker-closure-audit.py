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
#
# DELIBERATELY NARROW, and every dropped pattern was dropped on measurement
# (2026-09-03, 110 live samples). `still pending` and a bare `remainder` fired 12
# times and were WRONG every time: closure notes in this corpus cite sibling goal
# ids constantly (" ... still pending"), so generic queue vocabulary
# describes OTHER goals far more often than this one's leftovers. Only
# self-referential, quantified forms survive. Under-matching is the correct
# direction here — a missed remainder costs one unaudited goal, while a checker
# that cries wolf on ordinary cross-references gets ignored wholesale.
REMAINDER_RE = re.compile(
    r"\b(\d+\s+(?:entries|items|records|goals|files|observations)\s+remain(?:ing|s)?"
    r"|\d+\s*,?\s*\d*\s*remain\s*\("
    r"|still\s+undrained"
    r"|(?:deliberately\s+)?NOT\s+cleared"
    r"|partial(?:ly)?\s+(?:complete|done|drained))",
    re.IGNORECASE,
)

# guard-4007's PRESCRIBED REMEDY is "file the successor FIRST and name its id in
# the outcome_note". A note that does so is COMPLIANT, so flagging it punishes the
# exact behaviour the guardrail asks for — measured:  wrote "Filed as
#  (Case A — the unfinished remainder of sanctioned scope)" and was
# flagged for it. When a goal id sits near the remainder language, a tracker
# exists and the check suppresses.
SUCCESSOR_RE = re.compile(r"\bg-\d+-\d+\b", re.IGNORECASE)
SUCCESSOR_WINDOW = 300

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


NOTE_FIELDS = ("outcome_note", "outcome_notes", "progress_note", "key_finding", "notes")


def _note_parts(goal: dict) -> list[tuple[str, str]]:
    """(field, text) for every free-text field a closure leaves its account in."""
    parts = []
    for key in NOTE_FIELDS:
        v = goal.get(key)
        if isinstance(v, str) and v.strip():
            parts.append((key, v))
        elif isinstance(v, list) and v:
            parts.append((key, "\n".join(str(x) for x in v)))
    return parts


def _note(goal: dict) -> str:
    return "\n".join(t for _, t in _note_parts(goal)).strip()


def _where(goal: dict, rx: re.Pattern,
           suppress_near: re.Pattern | None = None,
           window: int = SUCCESSOR_WINDOW) -> tuple[str, str] | None:
    """(field, matched_text) for the first field the pattern hits.

    The field is reported in every finding because the account is spread across
    five fields: the first live finding (g-306-420) matched in progress_note
    while outcome_note was clean, and a reader who greps only outcome_note
    concludes the audit misfired. Naming the field turns a 3-command hunt into a
    1-command confirmation.

    `suppress_near` skips a match that has an exonerating token within `window`
    characters either side — used so a remainder that already names its successor
    goal reads as compliance rather than a defect.
    """
    for key, text in _note_parts(goal):
        for m in rx.finditer(text):
            if suppress_near is not None:
                lo = max(0, m.start() - window)
                hi = min(len(text), m.end() + window)
                if suppress_near.search(text[lo:hi]):
                    continue
            return key, m.group(0)
    return None


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

    # RECURRING GOALS REST AT `pending` BY DESIGN — a run completes, the note says
    # DONE, and the goal returns to pending for its next interval. That is health,
    # not the stranded-claim class, so this ONE check must exclude them. Measured
    # 2026-09-03 before this guard existed: all 12 DISAGREE verdicts on 
    # were recurring goals (recurring=True, achievedCount up to 426), i.e. the
    # entire HIGH-confidence bucket on the largest aspiration was noise. The other
    # three checks stay live for recurring goals — an empty note or an unrun
    # criterion is a defect whatever the goal's cadence.
    if status not in TERMINAL_OK and not goal.get("recurring"):
        hit = _where(goal, DONE_RE)
        if hit:
            field, text = hit
            fired.append({
                "check": "note_done_status_disagrees",
                "confidence": "high",
                "guardrail": "guard-2852",
                "field": field,
                "detail": f"{field} asserts completion ({text!r}) but status is {status!r} "
                          f"— the stranded-claim release class",
            })

    if status in TERMINAL_OK:
        hit = _where(goal, REMAINDER_RE, suppress_near=SUCCESSOR_RE)
        if hit:
            field, text = hit
            fired.append({
                "check": "remainder_language",
                "confidence": "medium",
                "guardrail": "guard-4007",
                "field": field,
                "detail": f"closed completed while its own {field} says work remains "
                          f"({text!r}) and names no successor goal to track it",
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


def self_verdict_of(goal: dict):
    """The worker's OWN declared verdict, or None when it never recorded one.

    g-306-417: until verify_verdict existed this module had nothing to compare
    against, so its AGREE/DISAGREE measured whether the RECORD was internally
    consistent -- not whether the auditor agreed with the WORKER. Reading the
    field is what makes the module's stated job ("records agreement or
    disagreement" on self-grading) literally true rather than aspirational.

    None is the honest answer for every closure written before the field
    landed, and it must NEVER be read as agreement (guard-963: an aggregator
    must not report a clean verdict over zero compared items).
    """
    v = goal.get("verify_verdict")
    if not isinstance(v, dict):
        return None
    verdict = v.get("verdict")
    return verdict.strip().lower() if isinstance(verdict, str) and verdict.strip() else None


def agreement_for(goal: dict, fired: list[dict]) -> str:
    """agree | disagree | not_comparable -- the WORKER-vs-AUDITOR comparison.

    Deliberately NOT a new check: a check would fire on all 202 pre-field
    closures and flood the report (guard-3343 -- adding a check to a multi-check
    reporter changes what its summary means). This is a per-row READING, so an
    absent verdict costs nothing and is counted separately.
    """
    self_v = self_verdict_of(goal)
    if self_v is None:
        return "not_comparable"
    if self_v == "completed" and any(f["confidence"] == "high" for f in fired):
        return "disagree"
    return "agree"


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
    agree_counts = {"agree": 0, "disagree": 0, "not_comparable": 0}
    for g in closures:
        take, why = sampled(g, fraction)
        if not take:
            continue
        fired = run_checks(g)
        v = verdict_for(fired)
        counts[v] += 1
        agree_counts[agreement_for(g, fired)] += 1
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
            "self_verdict": self_verdict_of(g),
            "agreement": agreement_for(g, fired),
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
        "agreement_counts": agree_counts,
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
    print(f"  record-consistency: AGREE={c['AGREE']}  DISAGREE={c['DISAGREE']}  REVIEW={c['REVIEW']}")
    a = result["agreement_counts"]
    print(f"  worker-vs-auditor: agree={a['agree']}  disagree={a['disagree']}  "
          f"not_comparable={a['not_comparable']} (no verify_verdict recorded)")
    if a["agree"] == 0 and a["disagree"] == 0 and a["not_comparable"]:
        print("  NOTE: ZERO closures carried a self-verdict, so NO agreement was "
              "measured — this is not evidence of agreement (guard-963).")
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
