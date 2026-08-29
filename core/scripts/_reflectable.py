"""Reflectability split for unreflected pipeline hypotheses ().

g-115-5358 widened `--unreflected` from live-only+stage==resolved to
live+archive+stage in (resolved, archived) — making the number TRUTHFUL
(the full never-reflected backlog). That was a semantics change for every
consumer using the value as a zero-test, threshold, or priority selector:
the backlog is dominated by records that can NEVER be reflected on
(g-115-4558: UNRESOLVABLE has no outcome to learn from; likewise EXPIRED
and outcome-less records). Measured 2026-08-14: 384 unreflected total =
181 UNRESOLVABLE + 150 EXPIRED + 47 no-outcome + 6 reflectable — and the
6 reflectable were exactly the 6 live stage=resolved records, i.e. in the
healthy flow "reflectable" and "awaiting reflection" coincide.

Consumers that gate ACTION (consolidation triage, quiescence drain
targeting, the iteration-close reflect nudge) must key on the REFLECTABLE
subset; the widened total remains the right number for backlog reporting.
This module is the one place that split is defined, so the next widening
changes every consumer together instead of one at a time.
"""

REFLECTABLE_OUTCOMES = {"CONFIRMED", "CORRECTED"}


def is_reflectable(rec) -> bool:
    """True when a pipeline record's outcome is one /reflect-on-outcome can
    actually learn from."""
    if not isinstance(rec, dict):
        return False
    return str(rec.get("outcome") or "").upper() in REFLECTABLE_OUTCOMES


def count_reflectable(records) -> int:
    """Count reflectable records in an --unreflected result array."""
    if not isinstance(records, list):
        return 0
    return sum(1 for r in records if is_reflectable(r))


# ── Fixture tell () ────────────────────────────────────────────────
# The reflection queue is a WORK QUEUE whose prescribed action is a full ABC
# chain. It could not tell a TEST FIXTURE from a finding: both are resolved
# records carrying an outcome and a surprise score. Following the protocol
# literally over a fixture MANUFACTURES learning — fabricated ABC chains, belief
# updates and pattern signatures from a claim that was never a prediction, and
# the artifacts are indistinguishable from real ones afterward.
#
# It FLAGS, it never filters (guard-1072: mark residue in place; never remove
# from a union-by-id merged store). A silent exclusion would hide the residue
# from the only queue positioned to notice it.
#
# WHY THE KEY IS ALWAYS EMITTED, even when empty: a key that appears only when
# non-empty cannot distinguish "nothing suspect" from "this build has no tell",
# and a consumer written against the second reading silently loses the guard.
#
# TWO SIGNALS, and the measured reason there are two (alpha, cc-07, 2026-08-28,
# over the live+archive union, 1809 records):
#   fixture-slug-category  — catches 8, ALL 8 genuinely fixtures, 0 false
#                            positives, and 5/5 of the named  control.
#   duplicate-title        — catches exactly the 5  records. One claim
#                            resolving three ways (CONFIRMED/CORRECTED/CONFIRMED
#                            on an identical title) is a test matrix; no real
#                            prediction can produce it (foxtrot, 2026-08-03).
#
# THE SECOND SIGNAL ADDS CORROBORATION, NOT COVERAGE — measured, and written
# down here so a future maintainer can subtract it on evidence instead of
# re-deriving it. Those 5 are a strict SUBSET of the category signal's 8: over
# 1809 records, duplicate-title catches ZERO that fixture-slug-category misses.
# It is kept because the category signal is a slug ALLOWLIST and therefore
# depends on the fixture author's naming, while duplicate-title is a shape
# signal that does not — and because a flag carrying TWO independent reasons is
# readable as strong where one reason is readable as a category-name
# coincidence. If a later corpus still shows 0 unique catches AND the
# corroboration is not being used, delete it and drop the two-pass tally with
# it; that is the whole cost.
#
# Queue-scoped, which is the population the endpoint actually stamps: 4 of 442
# unreflected flagged (all fixture-slug-category; the other 4 corpus-wide flags
# are already reflected:true and so never enter the queue), key present on
# 442/442.
#
# A PREDICATE THAT LOOKED PERFECT AND WAS NOT — recorded so it is not re-derived.
# A "skeletal" conjunct (no rationale AND no evidence AND no outcome_detail)
# scored 5/5 with zero false positives when tuned on the UNREFLECTED QUEUE, and
# scored 1/5 on the full corpus: 4 of the 5  fixtures DO carry
# outcome_detail (it names the derivation test), and only census-d — the one
# still in the queue — is skeletal. The queue is a biased sample of the corpus
# because the other four are already reflected:true. Tune a detector on the
# population its positive control lives in.
#
# KNOWN MISS, accepted deliberately: 2026-04-20_test-valid (title "Test valid",
# no rationale/evidence/outcome_detail) is a fixture by inspection but carries
# category `framework-test`, which is ALSO a legitimate category. Widening the
# slug set to catch it would risk flagging real framework-test hypotheses, and
# guard-1665's negative control matters more than the marginal catch. Under-flag
# rather than false-positive.

FIXTURE_SLUG_CATEGORIES = {
    "test", "test-cat", "test-category", "foo", "bar", "baz", "tmp", "dummy",
}


def _title_key(rec) -> str:
    return str((rec or {}).get("title") or "").strip()


def fixture_suspect_reasons(rec, duplicate_titles=frozenset()) -> list:
    """Reasons this record looks like a test fixture. Empty list == clean.

    `duplicate_titles` is the set of title strings appearing on 2+ records in
    the corpus being annotated; pass it from annotate_fixture_suspects, which
    computes it. Per-record purity is deliberate so the signal is unit-testable
    without a store.
    """
    if not isinstance(rec, dict):
        return []
    reasons = []
    if str(rec.get("category") or "").strip().lower() in FIXTURE_SLUG_CATEGORIES:
        reasons.append("fixture-slug-category")
    t = _title_key(rec)
    if t and t in duplicate_titles:
        reasons.append("duplicate-title")
    return reasons


def annotate_fixture_suspects(records):
    """Stamp `fixture_suspect` (a list of reasons) on every record IN PLACE.

    Always sets the key — empty list when clean — so absence means "old build",
    not "clean". Returns the same list for call-site convenience.
    """
    if not isinstance(records, list):
        return records
    counts = {}
    for r in records:
        if isinstance(r, dict):
            t = _title_key(r)
            if t:
                counts[t] = counts.get(t, 0) + 1
    dupes = frozenset(t for t, n in counts.items() if n > 1)
    for r in records:
        if isinstance(r, dict):
            r["fixture_suspect"] = fixture_suspect_reasons(r, dupes)
    return records
