"""Classify a GitHub Actions run failure as BUDGET-EXHAUSTED vs a real failure.

Origin: g-115-7699, after the 2026-08-23/24 outage (g-115-7387) where the
account-wide Actions spending limit was hit and CI was dark ~20h. Detection
existed only as per-run CI-FAIL emails -- edge-triggered and indistinguishable
from real failures.

THE SIGNATURE. A budget-blocked run completes conclusion=failure in seconds with
every non-skipped job at steps=0, carrying the annotation "The job was not
started because an Actions budget is preventing further use." A real failure
EXECUTES steps.

TWO SIGNALS, DELIBERATELY NOT EQUAL (guard-1265). The step count is STRUCTURAL --
it comes from the API's own job objects. The annotation is a STRING, and
guard-1265's 2026-07-31 correction is precisely that a field which looks
structured can be string-derived, so a string match must never be the sole
basis. Here the structural signal DECIDES and the annotation only CORROBORATES;
`annotation_only` is reported so a caller can see when the two disagree.

THE EMPTY-POPULATION TRAP (guard-1715). "every non-skipped job has steps == 0"
is VACUOUSLY TRUE for a run whose jobs are all skipped, or whose job list failed
to load -- and that reads as BUDGET-EXHAUSTED, which would fire the loud alert
this detector exists to make trustworthy. So a non-empty non-skipped population
is REQUIRED, and its size is returned for the caller to assert on.
"""

BUDGET_ANNOTATION = "budget is preventing further use"

# A skipped job never runs steps, so it carries no evidence either way and must
# be excluded from the population BEFORE the all-zero test is applied.
_SKIPPED = {"skipped"}


def classify_run(run):
    """run: {"conclusion": str, "jobs": [{"conclusion": str, "steps": [...],
    "annotations": [str]}]} -> dict verdict.

    Returns keys: verdict (budget_exhausted | real_failure | not_a_failure |
    indeterminate), non_skipped (int), zero_step (int), annotation_hits (int),
    annotation_only (bool), reason (str).
    """
    conclusion = (run.get("conclusion") or "").lower()
    jobs = run.get("jobs") or []

    non_skipped = [j for j in jobs
                   if (j.get("conclusion") or "").lower() not in _SKIPPED]
    zero_step = [j for j in non_skipped if len(j.get("steps") or []) == 0]
    ann_hits = [j for j in jobs
                if any(BUDGET_ANNOTATION in (a or "").lower()
                       for a in (j.get("annotations") or []))]

    out = {
        "non_skipped": len(non_skipped),
        "zero_step": len(zero_step),
        "annotation_hits": len(ann_hits),
        "annotation_only": False,
        "verdict": "indeterminate",
        "reason": "",
    }

    if conclusion != "failure":
        out["verdict"] = "not_a_failure"
        out["reason"] = "conclusion=%s; this classifier only judges failures" % (
            conclusion or "<empty>")
        return out

    # guard-1715: refuse to decide on an empty population. An all-skipped run and
    # a run whose jobs did not load are BOTH indeterminate, never budget.
    if not non_skipped:
        out["verdict"] = "indeterminate"
        out["reason"] = ("no non-skipped jobs (%d job(s) total) — the all-zero "
                         "test is vacuous here, so this is NOT budget-exhausted"
                         % len(jobs))
        if ann_hits:
            out["annotation_only"] = True
            out["reason"] += "; %d annotation hit(s) present but unsupported by " \
                             "any executed-step evidence" % len(ann_hits)
        return out

    if len(zero_step) == len(non_skipped):
        out["verdict"] = "budget_exhausted"
        out["reason"] = ("all %d non-skipped job(s) at steps=0"
                         % len(non_skipped))
        if ann_hits:
            out["reason"] += "; corroborated by %d budget annotation(s)" % len(ann_hits)
        else:
            # Structural signal alone still decides — the annotation is
            # corroboration, not a requirement. Say so rather than hedging.
            out["reason"] += "; no budget annotation found (structural signal decides)"
        return out

    out["verdict"] = "real_failure"
    out["reason"] = ("%d of %d non-skipped job(s) executed steps"
                     % (len(non_skipped) - len(zero_step), len(non_skipped)))
    if ann_hits:
        # Disagreement: the string says budget, the structure says otherwise.
        out["annotation_only"] = True
        out["reason"] += ("; %d budget annotation(s) present but CONTRADICTED by "
                          "executed steps — structural signal wins (guard-1265)"
                          % len(ann_hits))
    return out
