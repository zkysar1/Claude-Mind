#!/usr/bin/env python3
"""Confounder-matched batch-vs-corpus rate comparison (gap-101, ).

THE QUESTION THIS ANSWERS. You looked at a SELECTED batch, noticed that some
shape shows up a lot, and are about to encode that as a finding. Is the shape a
property of the phenomenon, or of the selection that produced the batch?

Two controls answer two DIFFERENT questions, and both run every time:

  CONFOUNDER control ("is my instrument reading documentation depth?")
      A text-derived indicator usually correlates with narrative length, and so
      does the selection. Re-compute the corpus rate MATCHED on the confounder
      at the batch's minimum and median. Also report what fraction of corpus
      hits fall BELOW the batch minimum -- if that is ~0, the indicator is a
      detector for the confounder rather than for the phenomenon.

  OUTCOME control ("does this shape distinguish a wrong prediction from a right
      one?") Compare the indicator rate among positive-outcome records against
      negative-outcome records at CORPUS scale. Under the separation bar, the
      shape is real and common and says nothing about being wrong.

PRECEDENCE, AND IT IS THE WHOLE POINT (g-115-5237 addendum, guard-2144). THE
OUTCOME CONTROL DECIDES. A surviving confounder control is NOT clearance to
encode. Measured on a live batch: mechanism-misattribution read 30.0% in-batch
vs 4.2% corpus (7.1x); length-matching did NOT collapse it (5.9% at batch min,
6.0% at median) and 18% of corpus hits fell below the batch minimum, so the
confounder control said KEEP -- and the outcome control overruled it at +3.0pp
separation, under the 10pp bar. Verdict BATCH-SCOPED; nothing encoded. Both
verdicts are emitted SEPARATELY so a caller can see when they disagree.

REPORT THE SEPARATION EVEN WHEN IT IS NEAR ZERO. A second indicator on the same
batch read 40% in-batch and separated by 0.1pp at corpus scale (12.5% vs 12.6%)
-- a shape that is real, common, and wholly unrelated to being wrong. That is
guard-2273's zero-discriminating-power reached from the other side, and only the
outcome arm surfaces it.

WHAT THIS DOES NOT COVER, stated so a green verdict is not read as more than it
is: guard-2144 separately requires printing the batch's outcome_date and
category distributions beside the corpus slice's, because a batch collapsing
onto <=2 dates or one work lane is a LANE artifact no rate comparison can see.
That check belongs to the caller; this tool does not perform it and a KEEP here
does not clear it.

Every ratio is emitted with the FORMULA that produced it (guard-2573): two
defensible formulas over the same quantity can invert an apparent trend, and
because each number is individually correct there is nothing to notice.

Usage:
  py -3 core/scripts/batch_corpus_rate.py --input <records.json> [--json]

Input JSON:
  {
    "batch":  [ {...record...}, ... ],
    "corpus": [ {...record...}, ... ],
    "indicator_field":  "hit",          # truthy = the shape is present
    "confounder_field": "narrative_len", # numeric; omit to skip that control
    "outcome_field":    "outcome",
    "positive_outcome": "CORRECTED",
    "negative_outcome": "CONFIRMED"
  }

Exit codes:
  0 - comparison ran; read `verdict` (KEEP | BATCH-SCOPED | INSUFFICIENT-DATA)
  1 - the verdict is BATCH-SCOPED (do not encode the batch finding)
  2 - input error: unreadable file, bad shape, or a named field absent from the
      records. A missing field is an ERROR and never an implicit zero -- a
      hand-written extractor that mismatches its input's shape returns empty,
      and empty is a well-formed, actionable-looking answer (guard-2421).
"""
import argparse
import json
import statistics
import sys

# The bar /replay Step 3 already uses for outcome separation. Below it, the
# indicator does not distinguish a wrong prediction from a right one.
OUTCOME_SEPARATION_PP = 10.0

# "~0" from the gap-101 spec, made explicit. If at most this fraction of corpus
# hits sit below the batch's confounder minimum, the indicator is only ever
# firing on records the selection would have picked anyway.
BELOW_MIN_FRACTION_BAR = 0.05

# Below this, a matched cell is too thin to carry a verdict on its own.
MIN_MATCHED_N = 10


def _truthy(v):
    """Indicator presence. A string "false"/"0"/"" is FALSE, not a non-empty
    string that happens to be truthy in Python."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "none", "null")
    return bool(v)


def _require_field(records, field, where):
    """A named field must EXIST on the records. Absent -> exit 2, never 0.

    guard-2421: a reader that mismatches its input's shape returns empty rather
    than raising, and the empty reads as a real answer. rb-245 is the sibling
    for a wrong field NAME. Both end here.
    """
    if not records:
        return
    missing = sum(1 for r in records if field not in r)
    if missing == len(records):
        raise KeyError(
            "field %r is absent from ALL %d %s records — check the field name "
            "against one real record before believing any rate computed here"
            % (field, len(records), where))


def _rate(records, indicator_field):
    hits = sum(1 for r in records if _truthy(r.get(indicator_field)))
    n = len(records)
    return {"hits": hits, "n": n, "rate": (hits / n) if n else None}


def _ratio(a, b):
    """a/b as a multiple, or None when it is not defined.

    Returns None rather than 0.0 or inf on a zero denominator: a made-up number
    here would propagate into the verdict as though it were measured.
    """
    if a is None or b is None or b == 0:
        return None
    return round(a / b, 3)


def _pct(x):
    return None if x is None else round(100.0 * x, 2)


def confounder_control(batch, corpus, indicator_field, confounder_field):
    """Re-compute the corpus rate matched on the confounder at batch min/median.

    Returns the unmatched pair, the two matched cells, the below-minimum
    fraction, and a verdict. Never decides the overall call — see PRECEDENCE.
    """
    out = {"applicable": True, "confounder_field": confounder_field}

    b_vals = [r.get(confounder_field) for r in batch
              if isinstance(r.get(confounder_field), (int, float))]
    if not b_vals:
        out.update(applicable=False, verdict="INSUFFICIENT-DATA",
                   reason="no numeric %r on any batch record" % confounder_field)
        return out

    b_min, b_med = min(b_vals), statistics.median(b_vals)
    out["batch_min"], out["batch_median"] = b_min, b_med

    corpus_rate = _rate(corpus, indicator_field)
    batch_rate = _rate(batch, indicator_field)
    out["unmatched"] = {
        "batch_rate_pct": _pct(batch_rate["rate"]),
        "corpus_rate_pct": _pct(corpus_rate["rate"]),
        "ratio": _ratio(batch_rate["rate"], corpus_rate["rate"]),
        "formula": "ratio = batch_hits/batch_n / (corpus_hits/corpus_n)",
        "bases": {"batch_n": batch_rate["n"], "corpus_n": corpus_rate["n"]},
    }

    matched = {}
    for label, threshold in (("at_batch_min", b_min), ("at_batch_median", b_med)):
        subset = [r for r in corpus
                  if isinstance(r.get(confounder_field), (int, float))
                  and r[confounder_field] >= threshold]
        cell = _rate(subset, indicator_field)
        matched[label] = {
            "threshold": threshold,
            "matched_n": cell["n"],
            "matched_hits": cell["hits"],
            "matched_rate_pct": _pct(cell["rate"]),
            "ratio_vs_batch": _ratio(batch_rate["rate"], cell["rate"]),
            "formula": ("matched_rate = hits/n over corpus records with %s >= %s"
                        % (confounder_field, threshold)),
            "thin": cell["n"] < MIN_MATCHED_N,
        }
    out["matched"] = matched

    # The diagnostic that names the failure mode directly: if essentially no
    # corpus HIT sits below the batch's confounder floor, the indicator only
    # ever fires where the selection was already looking.
    corpus_hits = [r for r in corpus if _truthy(r.get(indicator_field))
                   and isinstance(r.get(confounder_field), (int, float))]
    below = sum(1 for r in corpus_hits if r[confounder_field] < b_min)
    frac = (below / len(corpus_hits)) if corpus_hits else None
    out["below_minimum"] = {
        "corpus_hits_considered": len(corpus_hits),
        "below_batch_min": below,
        "fraction": None if frac is None else round(frac, 4),
        "bar": BELOW_MIN_FRACTION_BAR,
        "formula": "fraction = corpus hits with %s < batch_min / all corpus hits"
                   % confounder_field,
    }

    if frac is None:
        out["verdict"] = "INSUFFICIENT-DATA"
        out["reason"] = "no corpus hits carry a numeric confounder"
    elif frac <= BELOW_MIN_FRACTION_BAR:
        out["verdict"] = "CONFOUNDED"
        out["reason"] = ("only %.1f%% of corpus hits fall below the batch "
                         "minimum (bar %.0f%%) — the indicator is a detector "
                         "for %s, not for the phenomenon"
                         % (100 * frac, 100 * BELOW_MIN_FRACTION_BAR,
                            confounder_field))
    else:
        out["verdict"] = "KEEP"
        out["reason"] = ("%.1f%% of corpus hits fall below the batch minimum, "
                         "so the indicator fires outside the selection's range"
                         % (100 * frac))
    return out


def outcome_control(corpus, indicator_field, outcome_field,
                    positive_outcome, negative_outcome):
    """Does the indicator separate positive from negative outcomes at CORPUS
    scale? This control DECIDES the overall verdict."""
    pos = [r for r in corpus if r.get(outcome_field) == positive_outcome]
    neg = [r for r in corpus if r.get(outcome_field) == negative_outcome]
    p, n = _rate(pos, indicator_field), _rate(neg, indicator_field)

    out = {
        "positive_outcome": positive_outcome,
        "negative_outcome": negative_outcome,
        "positive": {"rate_pct": _pct(p["rate"]), "n": p["n"], "hits": p["hits"]},
        "negative": {"rate_pct": _pct(n["rate"]), "n": n["n"], "hits": n["hits"]},
        "bar_pp": OUTCOME_SEPARATION_PP,
        "formula": ("separation_pp = 100*(positive_hits/positive_n) - "
                    "100*(negative_hits/negative_n)"),
    }

    if p["rate"] is None or n["rate"] is None:
        out["separation_pp"] = None
        out["verdict"] = "INSUFFICIENT-DATA"
        out["reason"] = ("one arm is empty (positive n=%d, negative n=%d) — no "
                         "separation is computable" % (p["n"], n["n"]))
        return out

    sep = 100.0 * (p["rate"] - n["rate"])
    out["separation_pp"] = round(sep, 2)
    # ALWAYS reported, including when it is ~0 — a near-zero separation is the
    # finding, not an absence of one.
    if sep >= OUTCOME_SEPARATION_PP:
        out["verdict"] = "KEEP"
        out["reason"] = ("separation %.1fpp meets the %.0fpp bar — the shape "
                         "distinguishes %s from %s"
                         % (sep, OUTCOME_SEPARATION_PP, positive_outcome,
                            negative_outcome))
    else:
        out["verdict"] = "BATCH-SCOPED"
        out["reason"] = ("separation %.1fpp is under the %.0fpp bar — the shape "
                         "may be real and common, but it does not distinguish "
                         "%s from %s" % (sep, OUTCOME_SEPARATION_PP,
                                         positive_outcome, negative_outcome))
    return out


def compare(spec):
    batch = spec.get("batch") or []
    corpus = spec.get("corpus") or []
    indicator_field = spec.get("indicator_field", "indicator")
    outcome_field = spec.get("outcome_field", "outcome")
    confounder_field = spec.get("confounder_field")
    positive_outcome = spec.get("positive_outcome", "CORRECTED")
    negative_outcome = spec.get("negative_outcome", "CONFIRMED")

    if not batch or not corpus:
        raise ValueError("both `batch` and `corpus` must be non-empty "
                         "(batch n=%d, corpus n=%d)" % (len(batch), len(corpus)))

    _require_field(batch, indicator_field, "batch")
    _require_field(corpus, indicator_field, "corpus")
    _require_field(corpus, outcome_field, "corpus")

    result = {
        "indicator_field": indicator_field,
        "batch_n": len(batch),
        "corpus_n": len(corpus),
        "batch_rate_pct": _pct(_rate(batch, indicator_field)["rate"]),
        "corpus_rate_pct": _pct(_rate(corpus, indicator_field)["rate"]),
    }
    result["unmatched_ratio"] = _ratio(
        _rate(batch, indicator_field)["rate"],
        _rate(corpus, indicator_field)["rate"])
    result["unmatched_formula"] = (
        "unmatched_ratio = (batch_hits/batch_n) / (corpus_hits/corpus_n)")

    if confounder_field:
        _require_field(corpus, confounder_field, "corpus")
        result["confounder_control"] = confounder_control(
            batch, corpus, indicator_field, confounder_field)
    else:
        result["confounder_control"] = {
            "applicable": False, "verdict": "NOT-RUN",
            "reason": "no confounder_field supplied — the length-match arm did "
                      "NOT run, which is not the same as it passing"}

    result["outcome_control"] = outcome_control(
        corpus, indicator_field, outcome_field,
        positive_outcome, negative_outcome)

    # PRECEDENCE: the outcome control decides. See the module docstring.
    oc = result["outcome_control"]["verdict"]
    cc = result["confounder_control"]["verdict"]
    result["verdict"] = oc
    result["decided_by"] = "outcome_control"
    result["verdicts_conflict"] = (
        oc in ("KEEP", "BATCH-SCOPED") and cc in ("KEEP", "CONFOUNDED")
        and ((oc == "KEEP") != (cc == "KEEP")))
    if result["verdicts_conflict"]:
        result["conflict_note"] = (
            "the two controls DISAGREE (confounder=%s, outcome=%s). They answer "
            "different questions and the outcome control decides: a surviving "
            "confounder control is not clearance to encode." % (cc, oc))
    result["precedence_note"] = (
        "verdict is the OUTCOME control's; the confounder control is reported "
        "beside it and never overrides it (guard-2144)")
    result["not_covered"] = (
        "date/lane collapse is NOT checked here — guard-2144 separately requires "
        "comparing the batch's outcome_date and category distributions against "
        "the corpus slice's. A KEEP here does not clear that.")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Confounder-matched batch-vs-corpus rate comparison")
    ap.add_argument("--input", required=True,
                    help="JSON file with batch/corpus and field names")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON only (default also prints a summary)")
    args = ap.parse_args(argv)

    try:
        with open(args.input, encoding="utf-8") as fh:
            spec = json.load(fh)
    except Exception as e:
        print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))
        return 2

    try:
        result = compare(spec)
    except (KeyError, ValueError) as e:
        print(json.dumps({"error": str(e).strip('"')}))
        return 2

    print(json.dumps(result, indent=1))
    if not args.json:
        cc = result["confounder_control"]
        oc = result["outcome_control"]
        print("", file=sys.stderr)
        print("batch %s%% (n=%d) vs corpus %s%% (n=%d), unmatched ratio %s"
              % (result["batch_rate_pct"], result["batch_n"],
                 result["corpus_rate_pct"], result["corpus_n"],
                 result["unmatched_ratio"]), file=sys.stderr)
        print("  confounder control: %s — %s"
              % (cc.get("verdict"), cc.get("reason", "")), file=sys.stderr)
        print("  outcome control   : %s — %s"
              % (oc.get("verdict"), oc.get("reason", "")), file=sys.stderr)
        if result["verdicts_conflict"]:
            print("  CONFLICT: %s" % result["conflict_note"], file=sys.stderr)
        print("VERDICT: %s (decided by the outcome control)"
              % result["verdict"], file=sys.stderr)

    return 1 if result["verdict"] == "BATCH-SCOPED" else 0


if __name__ == "__main__":
    sys.exit(main())
