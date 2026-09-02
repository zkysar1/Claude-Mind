#!/usr/bin/env python3
"""Claim-integrity census -- detect goals whose claim pair was damaged below
the application layer (own-cloud fenced-PUT reconcile; rb-3636 sub-mechanism B,
class g-115-2306).

WHAT IT LOOKS FOR, and why the shape is the whole signal.

A goal record carrying ``claimed_by`` as a PRESENT KEY WITH VALUE None cannot
have been produced by any application code path in this tree. Every clear site
POPS the key (release, clear-stale-claims, cmd_update_goal, the terminal-status
block, coordination_merge) and every one of them moves
``claimed_by`` / ``claimed_at`` / ``claimed_by_sid`` as a UNIT by explicit
design. So:

  * key ABSENT              -> a normal unclaimed goal (a pop, or never claimed)
  * key PRESENT, value set  -> a normal live claim
  * key PRESENT, value None -> nobody in this codebase wrote that

and the discriminator is sharper still when a SIBLING field survives beside the
null: a record with ``claimed_at`` or ``started`` or ``executed_by`` intact next
to a null ``claimed_by`` is partial-field survival, which no unit-clearing site
can emit. That is reconcile damage, reported here as ``reconcile_damage``.

Damaged records are the dangerous kind, not merely untidy: the goal reads as
status=pending so the selector ranks and offers it, while a live foreign claim
may still be held against it -- duplicate execution of already-claimed work
(guard-4434 is the READING rule; this script is the DETECTION half).

WHY THIS READS RAW JSONL AND NOT ``aspirations-query.sh``.

guard-2467: ``aspirations-query.sh --goal-status`` returns a SIX-KEY projection
that DROPS the claim fields, so a claimed-population audit built on it returns a
false zero. ``--full`` fixes that but costs ~12MB per status. The raw record is
both cheaper and strictly more faithful -- a projection can only ever destroy the
key-presence distinction this check is entirely built on. Same direct
``_load_jsonl`` pattern as ``audit-user-to-agent.py``.

THE FALSE-ZERO CONTROL (read this before trusting any clean verdict).

This check reports "0 damaged" both when the store is healthy and when it has
been pointed at a source that does not carry claim fields at all -- and those two
zeros are byte-identical in every output that does not separate them. That is not
hypothetical: ``aspirations-compact-summary.json`` carries 16 keys over 194 goals
and ``claimed_by`` appears on ZERO of them, so a version of this check built on
the compact would have returned ``clean`` permanently, on every box, forever
(measured 2026-08-22, cc-02).

So ``present_value == 0`` is NEVER clean. It resolves to one of two verdicts,
and which one depends on a signal from the same scan (g-115-7876):

  * ``BLIND``          -- something is marked in-progress (so a claim SHOULD
                          exist) and none carries a value, OR the scan located
                          no non-terminal population at all. Source defect.
  * ``no-live-claims`` -- nothing is claimed AND nothing is in-progress, so the
                          zero is ENTAILED and says nothing about the source.
                          NOT APPLICABLE, and NOT a clean bill of health.

The older text asserted "on a running fleet some goal is always claimed right
now" as though it were unconditional. It holds on a multi-agent fleet and FAILS
on a one-agent deployment or a quiesced window, where BLIND then fired on every
iteration forever -- and a flag no action can clear is one a reader learns to
skip, which was burying this check's real damage findings on the box that
reported it (ZDS-Mind, 2026-08-26: 3 damaged records under a permanent BLIND).

The ``key_presence`` census is printed beside the finding count on EVERY run,
in every verdict, so the zero is always readable (guard-1419: a zero with two
explanations must be disambiguated; guard-2298: print the shape beside the
count), and ``scanned``/``in_progress`` are published beside it so a reader can
re-derive the verdict from the record rather than trusting the label.

PROVENANCE. Under own-cloud the local tree is a read-through cache (guard-980),
so this reads whatever the mirror currently holds and labels it
``local-mirror``. That is honest for a census and is NOT authority: a record
missing here may exist authoritatively, and vice versa. Do not escalate a single
reading to a peer; re-run, or confirm against the daemon, before acting
cross-box.

Read-only. Never mutates. Part of g-115-7094 (verification outcome 3 + check 3,
"re-scan and assert zero new instances over a 72h window").

Usage:
  py -3 core/scripts/claim-integrity-check.py
  py -3 core/scripts/claim-integrity-check.py --output json
  py -3 core/scripts/claim-integrity-check.py --since-hours 72
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import WORLD_DIR, agents_root  # noqa: E402
from _dt import parse_naive_iso  # noqa: E402

# A goal in one of these statuses is finished; a damaged claim pair on it is
# harmless (nothing will re-select it). Restricting to non-terminal keeps the
# finding set to records that can actually cause duplicate execution.
TERMINAL_STATUSES = {
    "completed", "skipped", "expired", "superseded", "decomposed",
}

# Fields that CANNOT survive a legitimate claim clear, because every clear site
# moves them as a unit with claimed_by. Any of these present beside a null
# claimed_by is positive evidence of reconcile damage rather than a pop.
SIBLING_FIELDS = ("claimed_at", "started", "executed_by")

# A goal in one of these statuses ASSERTS that work is underway, so it should
# carry a live claim. This is the `in_progress` signal verdict_for() uses to
# tell "nobody is working" apart from "the field is not reaching this code"
# (). Deliberately NOT the complement of TERMINAL_STATUSES: `pending`
# and `blocked` are non-terminal but assert no work, so counting them would
# make in_progress > 0 on every quiet store and re-arm the permanent BLIND this
# distinction exists to retire.
ACTIVE_STATUSES = {"in-progress"}


def _load_jsonl(path: Path, stats: dict = None) -> list:
    """Parse a JSONL store, skipping unparseable lines rather than dying.

    COUNTS what it skips (guard-3714). A silent `continue` here is the same
    false-zero shape this check's docstring rigorously disambiguates for
    key-presence: one unparseable line hides EVERY goal in that aspiration, so
    the census under-scans and reports "clean" from a population it never saw.
    The count is the discriminator -- many failures means the store really is
    malformed, exactly one means a write boundary. Reported as `parse_skipped`.
    """
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                if stats is not None:
                    stats["parse_skipped"] = stats.get("parse_skipped", 0) + 1
                continue
    return out


def _key_state(goal: dict, field: str) -> str:
    """absent | null | value -- the distinction the whole check rests on."""
    if field not in goal:
        return "absent"
    return "null" if goal.get(field) is None else "value"


def _age_hours(stamp, now: dt.datetime):
    """Age in hours, or None only when the stamp is genuinely unparseable.

    Normalizes at the PARSE BOUNDARY (guard-1398, guard-4372). A stamp read
    from a store may carry a tz offset; bare `fromisoformat` accepts it FINE
    and returns an AWARE datetime, so the failure lands one line later on the
    SUBTRACTION against a naive now() -- past every parse-level test -- as a
    TypeError that `except ValueError` does not catch.

    Widening that except would be WORSE than the crash, which is why this uses
    the shared normalizer instead: an offset-bearing stamp is perfectly VALID,
    and collapsing it into the same None an unparseable stamp returns would
    make main_result's `age_hours is not None` --since-hours filter silently
    DROP real damage findings. guard-4372: never let the normalizer discard an
    aware value into the unparseable return.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    parsed = parse_naive_iso(stamp)
    if parsed is None:
        return None
    return round((now - parsed).total_seconds() / 3600.0, 1)


def _scan_store(path: Path, source: str, now: dt.datetime, presence: dict,
                findings: list, stats: dict = None) -> int:
    scanned = 0
    for asp in _load_jsonl(path, stats):
        if not isinstance(asp, dict):
            continue
        for goal in (asp.get("goals") or []):
            if not isinstance(goal, dict):
                continue
            if goal.get("status") in TERMINAL_STATUSES:
                continue
            scanned += 1
            # The in-progress census (). Rides in `stats` alongside
            # parse_skipped rather than in `presence`, which is reserved for
            # claimed_by key-STATES and must keep absent/null/value as its only
            # keys -- main_result reads presence positionally into
            # key_presence, and a fourth key there would silently widen that
            # published shape. A caller passing stats=None simply does not
            # collect the signal, and verdict_for then falls back to BLIND.
            if stats is not None and goal.get("status") in ACTIVE_STATUSES:
                stats["in_progress"] = stats.get("in_progress", 0) + 1
            state = _key_state(goal, "claimed_by")
            presence[state] = presence.get(state, 0) + 1
            if state != "null":
                continue
            survivors = {f: goal.get(f) for f in SIBLING_FIELDS
                         if _key_state(goal, f) == "value"}
            findings.append({
                "goal_id": goal.get("id"),
                "asp_id": asp.get("id"),
                "source": source,
                "status": goal.get("status"),
                "title": (goal.get("title") or "")[:90],
                "claimed_by_sid": _key_state(goal, "claimed_by_sid"),
                "claimed_by_sid_value": goal.get("claimed_by_sid"),
                "surviving_siblings": survivors,
                # REPORT vs CONCLUSION are deliberately different sets here
                # (guard-4618: read a detector's predicate and its stated
                # purpose separately).
                #
                # `surviving_siblings` REPORTS all of SIBLING_FIELDS because
                # all of it is useful to a reader. `reconcile_damage` CONCLUDES
                # from `claimed_at` ALONE, because that is the only member
                # measured to move as a unit with claimed_by: a real release
                # (, 2026-08-22) popped claimed_by / claimed_by_sid /
                # claimed_at together while `started` and `executed_by` BOTH
                # SURVIVED it, present and populated. So those two carry no
                # evidence -- a legitimate clear leaves them behind routinely,
                # and citing them was the older `bool(survivors)` form's defect.
                #
                # NOT verdict-neutral when introduced: damage went 6 -> 5, and
                # the dropped record is the point.  carries
                # claimed_at as PRESENT-NULL, so it is not a "value" survivor
                # here, while the other five carry a real timestamp. A
                # pre-change estimate said 6 because it measured key PRESENCE;
                # `survivors` requires _key_state == "value". Presence and
                # value-ness are different questions and this file exists
                # precisely to keep them apart -- do not conflate them again.
                #
                # Dropping it is CORRECT, not a loss: claimed_by AND claimed_at
                # both present-null is a whole-unit null-fill, a DIFFERENT
                # shape from partial survival, and calling it partial survival
                # was the old form mislabelling it. So the live population is
                # not homogeneous: 5 partial-survival records, 1 null-fill.
                # That split is a lead for the writer hunt (
                # outcome[0]) -- two shapes may mean two mechanisms.
                "reconcile_damage": "claimed_at" in survivors,
                "age_hours": _age_hours(goal.get("last_modified")
                                        or goal.get("created_at"), now),
                "attributed_to": goal.get("executed_by"),
            })
    return scanned


def verdict_for(present_value: int, findings, *, in_progress=None,
                scanned=None) -> str:
    """BLIND | no-live-claims | damaged | clean -- the false-zero control,
    kept pure so a test can prove it FIRES rather than merely observing it stay
    quiet (rb-5828: a green check that cannot be shown to go red is not
    evidence).

    present_value == 0 outranks everything: no live claim anywhere means either
    nobody is working OR the field is not reaching this code. Reporting the
    second as clean is the exact failure guard-2467 describes.

    THE TWO ZEROS ARE NOT THE SAME ZERO (g-115-7876, filed by omni from
    ZDS-Mind 2026-08-26). The original premise -- "on a running fleet some goal
    is always claimed right now" -- is TRUE on a multi-agent fleet and FALSE on
    a one-agent deployment or a quiesced window, where present_value == 0 is
    the correct steady state. Measured on ZDS: in-progress 0, non-terminal 121,
    key_presence absent=118 null=3 value=0, and goal-selector all_blocked=true
    with candidates=0 -- so the queue could not have produced a claim. There,
    BLIND fired on EVERY iteration, permanently, and a flag no action can clear
    is one a reader learns to skip -- which was burying three real damage
    findings on that same box.

    THE DISAMBIGUATOR IS `in_progress`, AND IT COSTS NOTHING -- it comes from
    the same scan. A goal marked in-progress ASSERTS work is underway, so it
    should carry a live claim; in_progress > 0 beside present_value == 0 is the
    genuine source defect this check was built for. in_progress == 0 means no
    goal even claims to be worked, so present_value == 0 is ENTAILED and
    carries no information about the source.

    Note claims legitimately sit on `pending` goals (aspirations-claim.sh does
    not flip status), but that only ever RAISES present_value -- it cannot
    reach this branch, so it does not weaken the discriminator.

    BOTH NEW SIGNALS FAIL SAFE, which is the whole reason they are keyword-only
    with None defaults: an un-migrated call site passing two positionals gets
    BLIND exactly as before (guard-2275 -- prefer the fail-safe value as the
    default so an un-migrated caller gets more work, never less).

    `scanned` is the coverage count (guard-3489) and it gates the new verdict:
    a scan that located NO non-terminal population has verified nothing, and an
    empty population must return the UNSAFE verdict rather than the safe one
    (guard-2166 -- "all members satisfy P" is vacuously true on an empty set).
    So scanned == 0 stays BLIND even when in_progress == 0.

    `no-live-claims` is NOT a clean bill of health and must never be treated as
    one -- it is a third, honest verdict meaning "this check is not applicable
    right now". guard-2467's protection is preserved exactly: nothing here can
    return "clean" off a zero.
    """
    if present_value == 0:
        # Population never located -> the read is broken, not the fleet quiet.
        if not scanned:
            return "BLIND"
        # Explicitly `== 0`, never falsy: None (un-migrated caller) must NOT
        # satisfy this branch, and `not in_progress` would let it through.
        if in_progress == 0:
            return "no-live-claims"
        return "BLIND"
    return "damaged" if findings else "clean"


def main_result(since_hours=None) -> dict:
    """Run the census and return the result dict.

    Separated from main() so the precheck-eval `claim-integrity` sub-check and
    the CLI share ONE implementation. A hand-copied second scan in the caller
    would be a second predicate free to drift from this one, which is how a
    detector quietly stops covering the population it names.
    """
    now = dt.datetime.now()
    presence: dict = {}
    findings: list = []
    stats: dict = {}
    scanned = 0
    stores = []

    world_store = Path(WORLD_DIR) / "aspirations.jsonl"
    stores.append(str(world_store))
    scanned += _scan_store(world_store, "world", now, presence, findings, stats)

    for agent_dir in sorted(Path(agents_root()).glob("*")):
        if not agent_dir.is_dir():
            continue
        store = agent_dir / "aspirations.jsonl"
        if not store.exists():
            continue
        stores.append(str(store))
        scanned += _scan_store(store, f"agent:{agent_dir.name}", now,
                               presence, findings, stats)

    present_value = presence.get("value", 0)
    # `scanned` is this run's coverage count and `in_progress` its
    # applicability signal; both are already computed above, so the third
    # verdict costs no extra pass over the stores ().
    in_progress = stats.get("in_progress", 0)
    verdict = verdict_for(present_value, findings,
                          in_progress=in_progress, scanned=scanned)

    reported = findings
    if since_hours is not None:
        reported = [f for f in findings
                    if f["age_hours"] is not None
                    and f["age_hours"] <= since_hours]

    damaged = [f for f in reported if f["reconcile_damage"]]
    by_agent: dict = {}
    for f in reported:
        key = f.get("attributed_to") or "(unattributed)"
        by_agent[key] = by_agent.get(key, 0) + 1

    result = {
        "check": "claim-integrity",
        "verdict": verdict,
        "provenance": "local-mirror",
        # >0 means this census under-scanned: an unparseable line hides EVERY
        # goal in that aspiration, so a "clean" verdict beside a nonzero
        # parse_skipped is clean-over-a-population-it-never-saw (guard-3714).
        "parse_skipped": stats.get("parse_skipped", 0),
        "scanned_non_terminal": scanned,
        # Published so a reader can re-derive the verdict from the record
        # (guard-3743: a decision record must reproduce its own verdict from
        # its own recorded inputs). Without it, `no-live-claims` would be an
        # assertion nobody downstream could check.
        "in_progress": in_progress,
        "key_presence": {
            "absent": presence.get("absent", 0),
            "present_null": presence.get("null", 0),
            "present_value": present_value,
        },
        "findings_total": len(findings),
        "findings_reported": len(reported),
        "reconcile_damage_count": len(damaged),
        "per_agent": by_agent,
        "since_hours": since_hours,
        "stores_scanned": stores,
        "findings": reported,
    }
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", choices=("text", "json"), default="text")
    ap.add_argument("--since-hours", type=float, default=None,
                    help="only report findings newer than N hours (check 3's "
                         "72h re-scan window); the census still covers all")
    args = ap.parse_args(argv)

    result = main_result(since_hours=args.since_hours)
    verdict = result["verdict"]
    reported = result["findings"]
    scanned = result["scanned_non_terminal"]
    damaged = [f for f in reported if f["reconcile_damage"]]

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        kp = result["key_presence"]
        print(f"[claim-integrity] verdict={verdict} provenance=local-mirror "
              f"scanned={scanned} in_progress={result['in_progress']}")
        print(f"  key_presence: absent={kp['absent']} "
              f"present_null={kp['present_null']} "
              f"present_value={kp['present_value']}"
              "   <- present_value==0 is never clean: BLIND, or no-live-claims")
        if verdict == "BLIND":
            print("  BLIND: no live claim carries a value anywhere in the "
                  "scanned stores, AND either something is marked in-progress "
                  "(so a claim SHOULD exist) or the scan located no "
                  "non-terminal population at all. That is a source defect "
                  "(projection / wrong store), NOT a clean bill of health. "
                  "Do not report zero. (guard-2467)")
        elif verdict == "no-live-claims":
            print("  no-live-claims: nothing is claimed and nothing is marked "
                  "in-progress, so present_value==0 is ENTAILED and says "
                  "nothing about the source. This check is NOT APPLICABLE "
                  "right now -- it is not a clean bill of health, and it is "
                  "the expected steady state on a one-agent deployment or a "
                  "quiesced window. It becomes BLIND again the moment any goal "
                  "goes in-progress without a claim. (g-115-7876)")
        print(f"  findings: {len(reported)} reported "
              f"({len(damaged)} with partial field survival = reconcile damage)")
        for f in reported:
            surv = ",".join(f["surviving_siblings"]) or "-"
            print(f"    {f['goal_id']:<14} [{f['source']}] status={f['status']} "
                  f"sid={f['claimed_by_sid']} survivors={surv} "
                  f"age={f['age_hours']}h attributed={f['attributed_to']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
