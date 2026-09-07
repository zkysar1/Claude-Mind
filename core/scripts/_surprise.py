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
#
# THE ZERO-IMPORTS CONTRACT ABOVE STILL HOLDS AS WRITTEN. `decimal` is stdlib,
# pure, and has no module-level side effects, so it does not reintroduce the
# hazard that guard-547 is about: the danger was importing the CLI modules,
# which pull `_paths` at module top and RAISE when the path constants are
# unset. A stdlib numeric import cannot do that. Do NOT read this import as a
# licence to add project imports here ().
from decimal import ROUND_HALF_UP, Decimal


def _round_half_up(value):
    """Round a float to the nearest int, ties going AWAY from zero.

    Built on Decimal rather than `int(value + 0.5)`, which the g-115-3740
    filing named specifically: that idiom mishandles the float-error cases in
    this exact corpus. CONFIRMED at conf 0.95 computes `(1.0 - 0.95) * 10` ==
    0.5000000000000004, not 0.5 — a value that is genuinely above the tie and
    must score 1 under ANY rule. Decimal quantization over the actual binary
    float gives the mathematically correct answer for the number we really
    have; the +0.5 idiom gives the right answer here by luck and the wrong one
    elsewhere.

    Measured on every .x5 confidence in the live store (2026-09-07):
      CORRECTED 0.45 -> 5 (was 4), 0.65 -> 7 (was 6), 0.85 -> 9 (was 8)
      CONFIRMED 0.15 -> 9 (was 8), 0.35 -> 7 (was 6), 0.55 -> 5 (was 4)
    Every other value in that sweep is unchanged, including the 0.95 float-error
    case above.
    """
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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

    ROUNDING — DECIDED 2026-09-07 (g-115-3740): **HALF-UP**, ties away from
    zero, via `_round_half_up` below. This replaces Python `round()`, which is
    round-half-to-EVEN. The question is CLOSED; do not re-open it a third time.

    WHY. .x5 confidences are common in the live store (0.45/0.55/0.65/0.75/0.85
    all appear) and half-to-even made the boundary read as inconsistent to
    anyone checking by hand: CORRECTED at 0.65 scored 6 while 0.75 scored 8,
    because 6.5 rounded DOWN and 7.5 rounded UP. Nothing in any SKILL.md,
    config or convention ever specified half-to-even; it was an artifact of
    reaching for `round()`, whose name reads as ordinary half-up to most
    readers. Half-up is what the prose "round(confidence * 10)" always meant.

    WHAT IT IS NOT. This is a CORRECTNESS fix and NOT a fix to high-surprise
    tier starvation — do not close any goal believing the >= 7 tier now works.
    Measured over the 588-record replay-candidate pool (555 scoreable,
    2026-08-19, by another agent — inherited, not re-measured here) the rounding
    rule alone moved 10 records across >= 7, all sitting at raw 6.5. Best case
    after this change on that pool is ~11 of 555 (2.0%). The tier is also
    lopsided for an INDEPENDENT reason: it fires on CORRECTED@conf>=0.70 or
    CONFIRMED@conf<=0.30, and the low arm is almost never populated.

    ⚠ DO NOT RESTATE THAT LOW ARM AS "STRUCTURALLY UNREACHABLE". This docstring
    said exactly that on 2026-09-07, sourced from the filing's "minimum
    confidence ever written was 0.35 (0.40 in the later n=86 sweep)", and the
    same unit's own Q2 check FALSIFIED it before the goal closed. Re-measured
    the SAME DAY over the live pipeline (619 records, 198 scoreable with a
    numeric confidence, alpha worker cc-08, uname -r 6.8.0-138-generic):
    min confidence 0.30, max 0.95, ONE record at conf <= 0.30 and 21 at
    conf >= 0.70. So the low arm is REACHABLE and holds one record — sparse,
    not impossible — and the floor has moved DOWN since the figure that was
    quoted as a bound. Note the two populations differ (live pipeline here vs
    the replay-candidate pool there), so this does not refute the earlier
    census; it does show the floor is a moving observation and not a structural
    property. guard-1659: a number stated inside a goal's description is a
    hypothesis, and this one reached a durable docstring before being checked.

    The remaining instrument questions
    — thresholding on the RAW score rather than a rounded one, and whether a
    single FLEET-wide threshold is right when per-resolver confidence means
    span 0.0931 — are deliberately NOT settled here and are not blocked by
    this change.

    COMPARABILITY. Historical stored scores were computed under half-to-even
    and are NOT rewritten by this change. At the .x5 points a score written
    before 2026-09-07 can be one lower than the same input scores today. The
    write path DERIVES surprise (g-115-3801), so any record re-normalized after
    this date carries the new rule; treat a cross-era comparison at a .x5
    confidence as a one-point band, not an exact match.

    g-115-3594 LIFTED this arithmetic into one place without altering it and
    g-115-3801 MOVED it again, also without altering it. This change is the
    first alteration since it was written, and the boundary pin in
    test_surprise_rounding_is_half_up_at_the_promotion_boundary was updated in
    the same commit.
    """
    conf = float(confidence or 0.0)
    normalized = (outcome or "").strip().lower()
    if normalized == "corrected":
        return _round_half_up(conf * 10)
    if normalized == "confirmed":
        return _round_half_up((1.0 - conf) * 10)
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
