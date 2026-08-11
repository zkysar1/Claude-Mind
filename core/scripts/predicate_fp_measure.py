#!/usr/bin/env python3
"""predicate_fp_measure.py — measure a CANDIDATE GATE PREDICATE before it ships.

Answers two different questions that a raw match count conflates:

  FIRE RATE  = matches / corpus_size   -> decides gate POSTURE (deny vs advisory)
  FP RATIO   = narration / matches     -> decides whether the predicate is CORRECT

The second cannot be computed by this script. Separating a MANDATE from a
MENTION is a judgment call, and reporting only the total is precisely what
makes an unshippable predicate look fine (gap-122, guard-1430). So the tool is
deliberately TWO-PHASE and `sample` REFUSES to emit a verdict:

  1. `sample` — assemble the corpus, apply the predicate, report the
     denominator + match count + fire rate, and emit a SAMPLE for hand/LLM
     classification. verdict is always "unclassified".
  2. `score`  — take the classification back and compute the FP ratio + verdict.

Corpus adapters (the scope decision recorded on gap-122: ONE skill, N loaders —
the predicated thing is a candidate gate predicate in every encounter, which is
the SUBJECT boundary that separated gap-048):

  goals  — every goal record across ALL statuses, deduped by id. A pending-only
           corpus hides the completed majority; that is the whole reason this
           adapter exists rather than a single query.
  files  — line-oriented scan of explicit paths / globs (session transcripts,
           source files).

Exit codes:
  0  measurement produced
  1  refusal — empty corpus, or `score` called with a classification that does
     not cover the sample
  2  input error
"""
import argparse
import glob as globmod
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from _runtime_bash import bash_cmd  # type: ignore  # guard-580: never a bare "bash"

# Every status the store actually uses. IMPORTED, never re-declared: a
# hand-maintained copy drifts, and it already did — the first version of this
# tuple listed the six statuses CLAUDE.md names and silently omitted
# `decomposed` (16 live goals at the time) and `superseded`. An adapter that
# omits a status reports a denominator smaller than the corpus, understating
# the fire rate in exactly the direction that makes a bad predicate look
# shippable — the defect this whole tool exists to catch, in the tool itself.
# Single source of truth: communication-clarity.md rule 5.
from aspirations import VALID_GOAL_STATUSES  # type: ignore  # noqa: E402

GOAL_STATUSES = tuple(sorted(VALID_GOAL_STATUSES))

# Fields a goal-corpus predicate is normally run against. Kept explicit rather
# than "whole record JSON": matching against the serialized record would fire on
# field NAMES and on unrelated metadata, manufacturing false positives that the
# classification step then has to clean up by hand.
GOAL_TEXT_FIELDS = ("title", "description", "outcome_note")


def _run(cmd, timeout=180):
    """Run a subprocess, returning (rc, stdout, stderr). Never raises on rc."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


def load_goal_corpus(statuses=GOAL_STATUSES):
    """Assemble every goal record across ALL statuses, deduped by goal_id.

    aspirations-query.sh REQUIRES a filter, so the corpus has to be built from
    one call per status and merged. --full is load-bearing: the default
    projection is six keys and carries neither `description` nor `outcome_note`,
    so a predicate measured without it would be scanning titles alone and would
    read as near-zero-firing no matter how broad it is.
    """
    by_id, per_status, failures = {}, {}, []
    for st in statuses:
        rc, out, err = _run(bash_cmd("core/scripts/aspirations-query.sh",
                                     "--goal-status", st, "--full"))
        if rc != 0 or not out.strip():
            per_status[st] = 0
            failures.append({"status": st, "rc": rc, "stderr": (err or "")[:200]})
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            per_status[st] = 0
            failures.append({"status": st, "rc": rc, "error": f"json: {exc}"})
            continue
        rows = data if isinstance(data, list) else data.get("goals", data.get("results", []))
        per_status[st] = len(rows)
        for r in rows:
            gid = r.get("goal_id") or r.get("id")
            if gid:
                by_id.setdefault(gid, r)
    return by_id, {"per_status": per_status, "failures": failures}


def goal_unit_text(rec):
    parts = [str(rec.get(f) or "") for f in GOAL_TEXT_FIELDS]
    return "\n".join(p for p in parts if p)


def load_file_corpus(patterns):
    """Line-oriented corpus over explicit paths / globs.

    One unit == one line, because a transcript or source predicate fires on a
    line and the fire rate that decides gate posture is per-line (zeta measured
    12.71% of 102856 Bash CALLS, not of files).
    """
    units, files_read, unreadable = [], [], []
    seen = set()
    for pat in patterns:
        matched = sorted(globmod.glob(pat, recursive=True))
        if not matched and os.path.exists(pat):
            matched = [pat]
        for path in matched:
            if path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                unreadable.append({"path": path, "error": str(exc)})
                continue
            files_read.append(path)
            for n, line in enumerate(text.splitlines(), start=1):
                if line.strip():
                    units.append({"id": f"{path}:{n}", "text": line})
    return units, {"files_read": len(files_read), "unreadable": unreadable}


def apply_predicate(units, pattern, ignore_case):
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        print(json.dumps({"error": "bad_regex", "detail": str(exc)}), file=sys.stderr)
        sys.exit(2)
    matches = []
    for u in units:
        m = rx.search(u["text"])
        if m:
            s, e = m.span()
            lo, hi = max(0, s - 90), min(len(u["text"]), e + 90)
            matches.append({
                "unit_id": u["id"],
                "matched_text": u["text"][s:e][:200],
                "context": u["text"][lo:hi].replace("\n", " ")[:400],
            })
    return matches


def cmd_sample(args):
    if args.corpus == "goals":
        by_id, meta = load_goal_corpus()
        units = [{"id": gid, "text": goal_unit_text(rec)} for gid, rec in by_id.items()]
        corpus_meta = {"adapter": "goals", "fields_scanned": list(GOAL_TEXT_FIELDS), **meta}
    else:
        if not args.path:
            print(json.dumps({"error": "files adapter requires --path"}), file=sys.stderr)
            return 2
        units, meta = load_file_corpus(args.path)
        corpus_meta = {"adapter": "files", "unit": "line", **meta}

    # A PARTIAL corpus is the near-miss of the empty one, and it is worse
    # because it looks like a measurement. If some status queries failed, the
    # denominator is a survivors-only set (guard-3068, rb-6245) and every rate
    # below it is computed against the wrong population — while `corpus_size`
    # reads as a real number. Refusing would be wrong (a single flaky status
    # query would make the tool unusable), so the failure is surfaced where it
    # cannot be scrolled past: a top-level boolean plus a prefix on the note
    # the caller is already told to read.
    corpus_incomplete = bool(corpus_meta.get("failures"))

    corpus_size = len(units)
    if corpus_size == 0:
        # Anti-vacuity floor. A predicate measured over an empty corpus reports
        # 0 matches and 0.0 fire rate, which is indistinguishable from a correct
        # narrow predicate — the empty-reference-corpus inversion, in the
        # direction that makes anything look shippable.
        print(json.dumps({
            "phase": "sample", "verdict": "refused",
            "reason": "empty corpus — a rate over 0 units measures the loader, not the predicate",
            "corpus": corpus_meta,
        }, indent=2))
        return 1

    matches = apply_predicate(units, args.predicate, args.ignore_case)
    n = len(matches)
    sample = matches[: args.sample_size]
    note = ("FIRE RATE ONLY. The FP ratio is NOT computed here and cannot be: "
            "separating a mandate from a mention is a judgment call. Classify "
            "the sample below, then run `score`. Do not ship a predicate on "
            "these numbers alone (gap-122, guard-1430).")
    if corpus_incomplete:
        failed = ", ".join(str(f.get("status")) for f in corpus_meta.get("failures", []))
        note = (f"PARTIAL CORPUS — {len(corpus_meta['failures'])} corpus query(ies) FAILED "
                f"({failed}). corpus_size below is a survivors-only denominator and every "
                f"rate derived from it is WRONG BY AN UNKNOWN AMOUNT. Re-run before quoting "
                f"any number. See corpus.failures. || ") + note

    out = {
        "phase": "sample",
        "verdict": "unclassified",
        "corpus_complete": not corpus_incomplete,
        "verdict_note": note,
        "predicate": args.predicate,
        "ignore_case": bool(args.ignore_case),
        "corpus": corpus_meta,
        "corpus_size": corpus_size,
        "match_count": n,
        "fire_rate": round(n / corpus_size, 6),
        "fire_rate_pct": round(100.0 * n / corpus_size, 4),
        "sample_size": len(sample),
        "sample_is_complete": len(sample) == n,
        "sample": sample,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_score(args):
    """Compute the FP ratio from a hand/LLM classification of the sample."""
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "score reads the classification JSON on stdin"}), file=sys.stderr)
        return 2
    try:
        cls = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": "bad_json", "detail": str(exc)}), file=sys.stderr)
        return 2

    classified = cls.get("classified") or []
    genuine = [c for c in classified if str(c.get("verdict", "")).lower() in ("genuine", "target", "true")]
    narration = [c for c in classified if str(c.get("verdict", "")).lower() in ("narration", "mention", "false")]
    unknown = [c for c in classified if c not in genuine and c not in narration]
    total = len(classified)
    if total == 0:
        print(json.dumps({"phase": "score", "verdict": "refused",
                          "reason": "classification carried 0 entries"}), file=sys.stderr)
        return 1

    fp_ratio = len(narration) / total
    match_count = int(cls.get("match_count") or total)
    corpus_size = int(cls.get("corpus_size") or 0)
    extrapolated = (total < match_count)

    # Thresholds are deliberately NOT a pass/fail gate — they name the decision
    # the caller still has to make. A 90.5%-FP predicate and a 0%-FP predicate
    # both "ran fine"; only the ratio separates them.
    if fp_ratio >= 0.5:
        verdict, rec = "unshippable", "REPLACE the predicate — the majority of fires are narration."
    elif fp_ratio > 0.0:
        verdict, rec = "needs-narrowing", "Narrow the predicate, or ship ADVISORY only."
    else:
        verdict, rec = "clean", "No false positives in the classified set."

    out = {
        "phase": "score",
        "verdict": verdict,
        "recommendation": rec,
        "classified_total": total,
        "genuine": len(genuine),
        "narration": len(narration),
        "unclassifiable": len(unknown),
        "fp_ratio": round(fp_ratio, 4),
        "fp_pct": round(100.0 * fp_ratio, 2),
        "match_count": match_count,
        "corpus_size": corpus_size,
        "fire_rate_pct": (round(100.0 * match_count / corpus_size, 4) if corpus_size else None),
        "extrapolated_from_sample": extrapolated,
        "extrapolation_caveat": (
            f"Only {total} of {match_count} matches were classified; the ratio is a SAMPLE "
            "estimate, not a census. Say so wherever it is quoted." if extrapolated else None
        ),
        "posture_note": ("fire_rate decides gate POSTURE (deny vs advisory); fp_ratio decides "
                         "whether the predicate is CORRECT at all. They are different questions "
                         "and a single number cannot answer both."),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="assemble corpus, apply predicate, emit sample (NO verdict)")
    s.add_argument("--predicate", required=True, help="regex to measure")
    s.add_argument("--corpus", choices=("goals", "files"), default="goals")
    s.add_argument("--path", action="append", help="path or glob (files adapter; repeatable)")
    s.add_argument("--sample-size", type=int, default=20)
    s.add_argument("--ignore-case", action="store_true")
    s.set_defaults(func=cmd_sample)

    c = sub.add_parser("score", help="compute FP ratio from a classification on stdin")
    c.set_defaults(func=cmd_score)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
