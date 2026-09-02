#!/usr/bin/env python3
"""Ratchet the count of STRUCTURED-BUT-UNEVALUATABLE verification checks.

g-115-5195. The Layer-C detective for verification-check validity: goals carry
`verification.checks`, and a check that LOOKS machine-checkable but cannot be
evaluated is worse than an honest prose note — it reads as evidence and is not.

THIS SHIPS SOMETHING DIFFERENT FROM WHAT THE GOAL DESCRIPTION SPECIFIES, and
that is deliberate. Two supersessions, in order:

  1. zeta re-measured the seed on 2026-08-28 and wrote, verbatim, "Re-measuring
     CHANGED THE DESIGN — do not build what the description specifies." Its
     finding: a ratchet on the BLENDED validity rate "would move almost entirely
     with PROSE-CHECK FILING VOLUME, not with check quality. That is an
     anti-detector." Correct, and this file does not ratchet the blended rate.

  2. zeta's prescribed replacement — register the two RATES (a) structured-share
     and (b) well-formedness as baselines — collides with the convention that
     governs the file it names. `core/config/conventions/audit-baselines.md`
     requires "a non-negative integer count of drift items" and says of ratios:
     "If the metric is a ratio, latency, or anything continuous — use a
     different mechanism. Not this file," listing "baselining a ratio" as an
     anti-pattern. So the rates are REPORTED here, never ratcheted.

WHAT IS RATCHETED, AND WHY IT IS NOT THE OBVIOUS THING.

  RATCHETED: `unevaluatable_structured` — dict-shaped checks that declare a type
  yet cannot be evaluated, being either (i) an unknown type after vocabulary
  normalization, or (ii) a known type missing a field its evaluator hard-requires.
  Every item is individually actionable by editing one check, and the count is
  INSENSITIVE TO PROSE FILING VOLUME: filing a hundred prose checks moves it by
  zero. That is precisely the property zeta found the blended rate lacked.

  REPORTED BUT NOT RATCHETED: `structured_share` and `well_formedness` (in basis
  points, integers, for diffability) plus the raw populations. Ratcheting
  structured-share would re-create the anti-detector — it falls when authors file
  prose, with no defect introduced. This mirrors the disposition in the sibling
  `goal-field-census-ratchet.py`, which ratchets `distinct_keys` and reports
  `stray_occurrences` for the same class of reason (guard-1816: a ratchet on a
  number the available write paths cannot lower produces a permanent WARN that
  everyone learns to ignore, which is worse than not measuring it).

NEVER CALLS evaluate(). `predicate.evaluate()` EXECUTES commands — a corpus
sweep using it timed out at 300s and would run arbitrary `command_succeeds`
bodies across every goal in the fleet. This classifies STATICALLY: it calls
`predicate.normalize_check` (pure, non-mutating, documented as such) and then
tests field PRESENCE against a declared table. No subprocess, no git, no gh.

THE REQUIRED-FIELD TABLE REFUSES TO GO STALE. The description called
PREDICATE_TYPES "eight-member"; it has nine, and the accepted vocabulary is
actually 22 tokens (9 canonical + 8 type aliases + 5 not-machine-checkable
aliases). Rather than hardcode any of that, `_assert_table_complete()` diffs the
declared table against the LIVE registry and exits 2 if a canonical type has no
declaration. A tenth predicate type therefore breaks this loudly at the next run
instead of being silently classified as valid.

WHY THE TABLE IS DECLARED RATHER THAN DERIVED. Feeding each evaluator an empty
dict does reveal its required fields from the returned reason — but
`_eval_vcs_commits_since` defaults `repo` to PROJECT_ROOT and shells out to git
rather than bailing, so a derive-by-probing validator would execute a subprocess
per run and, worse, would execute whatever a future evaluator does on empty
input. The table below was derived that way ONCE, by hand, with that evaluator
read rather than probed; the completeness assertion is what keeps it honest.

zeta's own numbers illustrate the hazard this file is built to avoid. Its
"unknown-type breakdown" counted `test_check`, `command_check` and `manual` as
unknown types. All three are ACCEPTED vocabulary — the first two alias to
`command_succeeds`, the third to `not_machine_checkable` — so an un-normalized
scan reports valid checks as broken. That normalization is g-115-5186's shipped
repair (commit 1a07f3f0e, "8.4% -> 40.6% valid"), which is also the corpus
repair the description gates the ratchet floor on: it has landed, so seeding is
no longer premature.

Usage:
  python verification-check-validity-ratchet.py [--json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import META_DIR  # type: ignore  # noqa: E402
from _fileops import locked_modify_yaml  # type: ignore  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402
from aspirations import VALID_GOAL_STATUSES  # noqa: E402
from predicate import (  # noqa: E402
    PREDICATE_TYPES,
    normalize_check,
)

KEY = "verification_check_validity"
BASELINES_PATH = Path(META_DIR) / "audit-baselines.yaml"

# Required fields per CANONICAL type, derived from each evaluator's own
# hard-required bail-out (the reason string it returns when the field is
# absent). A tuple means "all of these"; a frozenset nested in the tuple means
# "at least one of these". Field ALIASES are already resolved by
# normalize_check() before this table is consulted.
REQUIRED_FIELDS: dict = {
    # "missing anchor (ISO timestamp)" / "missing or invalid delay_seconds"
    "after_time": ("anchor", "delay_seconds"),
    # "missing command"
    "command_succeeds": ("command",),
    # "missing required field: path"
    "file_check": ("path",),
    # "missing required field: path and after_ref"
    "file_exists_after": ("path", "after_ref"),
    # "missing required field: goal_id and after_ref"
    "goal_completed_after": ("goal_id", "after_ref"),
    # "must specify at least one of min, max" is checked BEFORE "missing command"
    "metric_threshold": ("command", frozenset({"min", "max"})),
    # returns passed=True unconditionally; an honest declaration needs no fields
    "not_machine_checkable": (),
    # "missing/invalid repo (need owner/name)" / "missing/invalid pr"
    "pr_merged": ("repo", "pr"),
    # "must specify since_goal_last_achieved or after_ref" (repo/min_count default)
    "vcs_commits_since": (frozenset({"since_goal_last_achieved", "after_ref"}),),
}


def _assert_table_complete() -> None:
    """Refuse to run if the live registry has a type this table does not declare.

    The whole point: a tenth predicate type must break this LOUDLY rather than
    be silently classified as well-formed. Extra declarations are tolerated (a
    type may be retired); missing ones are fatal.
    """
    missing = sorted(set(PREDICATE_TYPES) - set(REQUIRED_FIELDS))
    if missing:
        raise RuntimeError(
            "REQUIRED_FIELDS is stale: predicate.PREDICATE_TYPES declares "
            f"{sorted(PREDICATE_TYPES)} but this table has no entry for "
            f"{missing}. Read the new evaluator's hard-required bail-out and add "
            "it — do NOT delete this assertion, it is what keeps the sweep from "
            "reporting an unknown type as valid."
        )


def _missing_required(check: dict) -> list:
    """Return the required fields absent from an already-normalized check."""
    spec = REQUIRED_FIELDS.get(check.get("type"), ())
    missing = []
    for req in spec:
        if isinstance(req, frozenset):
            if not any(check.get(f) is not None for f in req):
                missing.append("one-of:" + "|".join(sorted(req)))
        elif check.get(req) is None:
            missing.append(req)
    return missing


def _classify(raw) -> tuple:
    """Classify ONE check statically. Returns (bucket, detail).

    Buckets: prose | unknown_type | malformed | valid
    """
    if not isinstance(raw, dict):
        return "prose", type(raw).__name__
    norm = normalize_check(raw)
    ptype = norm.get("type", "")
    if ptype not in PREDICATE_TYPES:
        return "unknown_type", (ptype or "<no type field>")
    missing = _missing_required(norm)
    if missing:
        return "malformed", f"{ptype}:missing[{','.join(missing)}]"
    return "valid", ptype


def _census() -> dict:
    """Sweep verification.checks across EVERY goal at EVERY valid status.

    Status enumeration comes from VALID_GOAL_STATUSES, never a hand-written
    list. zeta's seed measurement "excludes completed/archived goals", which is
    exactly the undercount the sibling ratchet documents: a scan that misses
    statuses drifts its own baseline downward and then reports "regressed" the
    first time someone counts correctly.
    """
    buckets = {"prose": 0, "unknown_type": 0, "malformed": 0, "valid": 0}
    details: dict = {}
    offenders: list = []
    seen: set = set()
    goals_scanned = 0
    goals_with_checks = 0

    for status in sorted(VALID_GOAL_STATUSES):
        proc = subprocess.run(
            bash_cmd("core/scripts/aspirations-query.sh",
                     "--goal-status", status, "--full"),
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            continue
        out = proc.stdout
        start = out.find("[")
        if start < 0:
            continue
        try:
            rows = json.loads(out[start:])
        except Exception:
            continue
        for g in rows:
            gid = g.get("goal_id") or g.get("id")
            if not gid or gid in seen:
                continue
            seen.add(gid)
            goals_scanned += 1
            checks = ((g.get("verification") or {}).get("checks")
                      if isinstance(g.get("verification"), dict) else None)
            if not isinstance(checks, list) or not checks:
                continue
            goals_with_checks += 1
            for idx, c in enumerate(checks):
                bucket, detail = _classify(c)
                buckets[bucket] += 1
                if bucket in ("unknown_type", "malformed"):
                    details[detail] = details.get(detail, 0) + 1
                    # A detective that reports a count without naming WHERE is
                    # half a detective — the count is only actionable with the
                    # goal id and check index to edit.
                    offenders.append({"goal_id": gid, "check_index": idx,
                                      "bucket": bucket, "detail": detail})

    total = sum(buckets.values())
    structured = total - buckets["prose"]
    # Basis points, integers, so successive runs diff cleanly. REPORTED ONLY —
    # these are ratios and the governing convention forbids ratcheting them.
    share_bp = round(10000 * structured / total) if total else 0
    wellformed_bp = round(10000 * buckets["valid"] / structured) if structured else 0

    return {
        "unevaluatable_structured": buckets["unknown_type"] + buckets["malformed"],
        "checks_total": total,
        "checks_prose": buckets["prose"],
        "checks_structured": structured,
        "checks_valid": buckets["valid"],
        "checks_unknown_type": buckets["unknown_type"],
        "checks_malformed": buckets["malformed"],
        "structured_share_bp": share_bp,
        "well_formedness_bp": wellformed_bp,
        "goals_scanned": goals_scanned,
        "goals_with_checks": goals_with_checks,
        "statuses_scanned": len(VALID_GOAL_STATUSES),
        # Most-common defects first; stable order keeps runs diffable.
        "defects": dict(sorted(details.items(), key=lambda kv: (-kv[1], kv[0]))[:25]),
        "offenders": sorted(offenders, key=lambda o: (o["goal_id"], o["check_index"])),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and report without touching the baseline file")
    args = ap.parse_args()

    try:
        _assert_table_complete()
        current = _census()
    except Exception as e:
        print(f"ERROR: verification-check validity census failed: {e}",
              file=sys.stderr)
        return 2

    if current["goals_scanned"] == 0 or current["checks_total"] == 0:
        # POSITIVE CONTROL. Zero goals (or zero checks across every goal) means
        # the query failed or the store moved, never a healthy fleet — and a
        # seeded baseline of 0 would then flag every subsequent honest run as
        # "regressed" (rb-245: verify the population exists before believing a
        # zero; guard-2298: never accept a zero without a passing control).
        msg = (f"aspirations-query returned {current['goals_scanned']} goal(s) and "
               f"{current['checks_total']} check(s) across all "
               f"{current['statuses_scanned']} statuses — the store is unreachable "
               f"or empty; refusing to seed a baseline of 0")
        print(json.dumps({"verdict": "skipped", "message": msg}, indent=2)
              if args.json else
              f"[verification-check-validity-ratchet] SKIPPED: {msg}")
        return 0

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # Read the prior baseline INSIDE the lock: sibling ratchets share this
        # file and this lock, and without the locked RMW two writers each ratchet
        # against an already-stale baseline and the second reverts the first.
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior = entry.get("baseline")
        cur = current["unevaluatable_structured"]

        if prior is None:
            verdict, new_baseline = "seeded", cur
            message = (
                f"Seeded baseline at {cur} unevaluatable structured check(s) "
                f"({current['checks_unknown_type']} unknown-type, "
                f"{current['checks_malformed']} malformed) out of "
                f"{current['checks_structured']} structured / "
                f"{current['checks_total']} total checks across "
                f"{current['goals_scanned']} goal(s). Future runs compare against it."
            )
        elif cur > prior:
            verdict, new_baseline = "regressed", prior  # never raise the baseline
            message = (
                f"WARN: unevaluatable structured checks grew from baseline {prior} "
                f"to {cur} (+{cur - prior}). Someone filed a check that declares a "
                f"machine-checkable type but cannot be evaluated — see `defects` for "
                f"which. DO NOT RE-SEED: new_baseline is pinned to `prior` on "
                f"purpose, and coordination_merge.merge_audit_baselines merges "
                f"`baseline` by MIN (one-way shrink, never grow), because "
                f"audit-baselines.md names growing a baseline on regression as THE "
                f"anti-pattern that defeats the ratchet. A hand re-seed therefore "
                f"verifies STABLE locally and is silently reverted at the next merge."
            )
        elif cur < prior:
            verdict, new_baseline = "ratcheted", cur
            message = (f"Ratcheted down from {prior} to {cur} unevaluatable "
                       f"structured check(s) (-{prior - cur}).")
        else:
            verdict, new_baseline = "stable", prior
            message = f"Stable at {cur} unevaluatable structured check(s)."

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": cur,
            "verdict": verdict,
            "goals_scanned": current["goals_scanned"],
            "checks_total": current["checks_total"],
            "checks_structured": current["checks_structured"],
            "checks_valid": current["checks_valid"],
            "structured_share_bp": current["structured_share_bp"],
            "well_formedness_bp": current["well_formedness_bp"],
            "hostname": os.environ.get("HOSTNAME") or socket.gethostname(),
        })
        baselines[KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            # Named so a future reader cannot mistake WHICH number is gated. The
            # two rates are deliberately NOT ratcheted — see the module docstring.
            "ratcheted_metric": "unevaluatable_structured",
            "reported_not_ratcheted": "structured_share_bp, well_formedness_bp",
            "history": history[-50:],
        }
        captured.update(verdict=verdict, new_baseline=new_baseline, message=message)
        return baselines

    if args.dry_run:
        entry = {}
        try:
            import yaml  # type: ignore
            if BASELINES_PATH.is_file():
                entry = (yaml.safe_load(BASELINES_PATH.read_text(encoding="utf-8"))
                         or {}).get(KEY) or {}
        except Exception:
            entry = {}
        prior = entry.get("baseline")
        captured.update(
            verdict="dry-run", new_baseline=prior,
            message=f"current={current['unevaluatable_structured']} "
                    f"prior_baseline={prior} (no write)")
    else:
        try:
            locked_modify_yaml(BASELINES_PATH, _modify, initial={})
        except Exception as e:
            print(f"WARN: could not persist baseline to {BASELINES_PATH}: {e}",
                  file=sys.stderr)
            # OVERWRITE, never setdefault. _modify runs INSIDE locked_modify_yaml
            # and populates `captured` before the write; if the write then fails,
            # setdefault is a no-op and this would report the COMPUTED verdict as
            # though it had persisted. A tool must not claim a write it did not make.
            computed = captured.get("verdict")
            captured["verdict"] = "error"
            captured["new_baseline"] = None
            captured["message"] = (
                f"baseline operation FAILED and nothing was persisted: {e}"
                + (f" (the computed verdict was '{computed}' — it did NOT "
                   f"take effect)" if computed else ""))

    result = {
        "verdict": captured.get("verdict"),
        "baseline": captured.get("new_baseline"),
        "message": captured.get("message"),
        **current,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[verification-check-validity-ratchet] "
              f"{str(result['verdict']).upper()}: {result['message']}")
        print(f"  checks: {current['checks_total']} total = "
              f"{current['checks_prose']} prose + {current['checks_structured']} "
              f"structured ({current['checks_valid']} valid, "
              f"{current['checks_unknown_type']} unknown-type, "
              f"{current['checks_malformed']} malformed)")
        print(f"  REPORTED (not ratcheted): structured-share "
              f"{current['structured_share_bp'] / 100:.1f}%, well-formedness "
              f"{current['well_formedness_bp'] / 100:.1f}%")
        print(f"  goals: {current['goals_with_checks']} with checks / "
              f"{current['goals_scanned']} scanned across "
              f"{current['statuses_scanned']} statuses")
        if current["defects"]:
            print("  top defects:")
            for d, n in list(current["defects"].items())[:10]:
                print(f"    {n:5d}  {d}")
        if current["offenders"]:
            print("  offenders (goal_id / check index / defect):")
            for o in current["offenders"][:25]:
                print(f"    {o['goal_id']}  checks[{o['check_index']}]  {o['detail']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
