# Class-B import-cycle-proof helper ().
#
# SINGLE SOURCE OF TRUTH for the hypothesis surprise score. This module exists
# so the score has exactly ONE implementation reachable from BOTH tiers:
#
#   CLI    core/scripts/reflect-bookkeeping.py  (cmd_surprise, cmd_batch_micro)
#   DAEMON mind_api/src/world/pipeline_write.py (_normalize_record — the write path)
#          core/scripts/pipeline.py             (normalize_record — CLI mirror)
#
# WHY A SEPARATE MODULE RATHER THAN AN IMPORT OF THE CLI. guard-547: daemon
# endpoints carry verbatim duplicates of CLI normalize/validate logic because
# those CLI modules import from `_paths` at module top, which RAISES when the
# path constants are unset — importing them from the daemon is unsafe. That
# reasoning applies to the CLI *module*, not to the arithmetic. Lifted here,
# with ZERO imports and no module-level side effects, it is safe for either
# tier to import directly (same shape as `_path_helpers.py`, which agent_paths
# already shares across both tiers). Duplication was the cost of the import
# hazard; removing the hazard removes the reason to duplicate.
#
# DO NOT re-inline this arithmetic anywhere. A second copy is precisely the
# defect this module was extracted to end — see the  census below.


def compute_surprise(outcome, confidence):
    """Surprise score 0-10 for a resolved hypothesis. SINGLE SOURCE OF TRUTH.

    High surprise = high confidence + wrong, or low confidence + right.

    PURE FUNCTION of (outcome, confidence). It takes no other input, so a
    stored value that disagrees with this function is wrong by definition —
    which is why the write path now DERIVES it rather than accepting it from
    the caller (g-115-3801). Measured before that fix, across the resolved +
    archived union (769 records, 391 scoreable): 158 stored values (40.4%)
    disagreed with this function, and 80 (20.5%) disagreed by enough to change
    the /review-hypotheses Step 3.5 branch — 47 of those UNDER-stated, so a
    mandated broad re-retrieve + reconciliation never ran. Nothing errored and
    every record looked complete; that is what made it invisible.

    `outcome` is case-normalized deliberately: the micro-hypothesis store
    writes lowercase ("corrected"/"confirmed") while /review-hypotheses Step 3
    writes uppercase ("CORRECTED"/"CONFIRMED"). Both must score identically --
    a case-sensitive match would return 0 for every SKILL.md caller, which
    reads as "well-calibrated" and silently skips the Step 3.5 high-surprise
    re-retrieve. Any outcome that is neither (e.g. UNRESOLVABLE, EXPIRED)
    scores 0, which is correct: those are excluded from calibration.

    ROUNDING: round() is round-half-to-EVEN, and .x5 confidences are common in
    the live store (0.45/0.55/0.65/0.75/0.85 all appear). So the boundary reads
    as inconsistent -- CORRECTED at 0.65 scores 6 while 0.75 scores 8, because
    6.5 rounds DOWN and 7.5 rounds UP. That straddles the surprise >= 7
    promotion threshold: a half-UP rule would score 0.65 CORRECTED as 7 and
    fire both the "high_surprise" promotion and the Step 3.5 broad re-retrieve;
    half-to-even scores 6 and fires neither. g-115-3594 LIFTED this arithmetic
    into one place WITHOUT altering it -- the values are byte-identical to the
    previous inline batch-micro code. Whether half-up is the intended rule is a
    separate semantic question; the behavior is pinned by
    test_surprise_rounding_is_bankers_at_the_promotion_boundary so it cannot
    drift silently in either direction while that question is open. g-115-3801
    MOVED this function again, also without altering it: same rounding, same
    case handling, same 0 for non-calibrated outcomes.
    """
    conf = float(confidence or 0.0)
    normalized = (outcome or "").strip().lower()
    if normalized == "corrected":
        return round(conf * 10)
    if normalized == "confirmed":
        return round((1.0 - conf) * 10)
    return 0


# The outcomes compute_surprise actually scores. Every other outcome
# (UNRESOLVABLE, EXPIRED, None) returns 0 from the function above, and 0 is a
# real score meaning "unsurprising" — NOT "not applicable". The write path must
# therefore gate on this set rather than calling the function unconditionally,
# or an unresolved record's `surprise: None` would be overwritten with a 0 that
# reads as a genuine measurement. Exported so the two normalize sites share the
# gate as well as the arithmetic.
SCOREABLE_OUTCOMES = ("confirmed", "corrected")


def derive_surprise(rec):
    """Return the surprise value a record SHOULD carry, or None to leave it alone.

    The write-path half of the single-source-of-truth fix. Returns None — meaning
    "do not touch the stored value" — unless the record is genuinely scoreable:

      * outcome must be CONFIRMED/CORRECTED (case-insensitive). Anything else,
        including an unresolved record with outcome=None, is left untouched so
        `surprise: None` keeps meaning "not yet resolved".
      * confidence must be present and numeric. `compute_surprise` coerces a
        missing confidence to 0.0, which would score a CONFIRMED record as a
        maximally-surprising 10 — a fabricated measurement, and worse than the
        caller-supplied value it replaced. An unparseable confidence is treated
        as absent for the same reason.
    """
    outcome = rec.get("outcome")
    if outcome is None:
        return None
    if str(outcome).strip().lower() not in SCOREABLE_OUTCOMES:
        return None
    confidence = rec.get("confidence")
    if confidence is None:
        return None
    try:
        float(confidence)
    except (TypeError, ValueError):
        return None
    return compute_surprise(outcome, confidence)


def apply_derived_surprise(rec):
    """Set rec['surprise'] to the derived value in place, if the record is scoreable.

    The two-line derive-then-assign, shared so the write paths cannot drift
    apart. Three call sites today: `_normalize_record` and `update_field` in
    mind_api/src/world/pipeline_write.py, and `normalize_record` in
    core/scripts/pipeline.py (the guard-547 parity mirror).

    `update_field` is the reason this is a function rather than two inline
    lines. It normalizes the record and THEN assigns `rec[field] = value`, so
    normalization alone leaves two holes on that path — measured, not reasoned:
    `--field surprise --value 99` lands the caller's 99 on top of the derived 6,
    and `--field outcome --value CONFIRMED` never re-derives at all (surprise
    stayed None). Both are the exact caller-supplied-surprise drift g-115-3801
    exists to end, surviving inside the endpoint that looked covered because it
    does call the normalizer. Re-applying AFTER the assignment closes both.

    Returns the record for convenient chaining; the mutation is in place.
    """
    derived = derive_surprise(rec)
    if derived is not None:
        rec["surprise"] = derived
    return rec
