#!/usr/bin/env python3
"""Gate retirement evaluator — Phase 5 of the gate audit/retirement plan.

Reads `meta/gate-firings.jsonl` + `core/config/gates.yaml` and produces
per-gate recommendations: retire | tighten | widen | investigate | keep
| insufficient_data | uninstrumented.

Recommender, not judge — every recommendation includes the raw counts and
ratios that produced it so a reviewer (or downstream evolution loop) can
sanity-check the math. Never deletes anything; output is JSON-only.

Usage:
  py -3 core/scripts/gate-retirement-eval.py [--days N] [--min-fires K]
                                             [--gate <id>] [--output json|human]

  --days N       Look-back window in days (default 30).
  --min-fires K  Volume threshold applied to both named guards
                 (MIN_TOTAL_FIRINGS, MIN_RATE_SAMPLES) (default 10).
  --gate <id>    Restrict output to a single gate id.
  --output       json (default) or human-readable summary table.

Recommendation rules (see core/config/conventions/gate-overrides.md and
gates.yaml for context). Two named volume guards gate distinct sample sets:
MIN_TOTAL_FIRINGS gates `total` (retire / widen / insufficient_data),
MIN_RATE_SAMPLES gates `block + override` (tighten). Both default to
`--min-fires`.

  retire         decision != noop count == 0 over window
                 AND total >= MIN_TOTAL_FIRINGS
                 AND retirement_eligible: true
                 → Gate has never fired meaningfully. Delete it.

  tighten        override / (block + override) > tighten_threshold (0.5)
                 AND (block + override) >= MIN_RATE_SAMPLES
                 AND NOT recently-improved (recency suppression, below)
                 → FP-dominant: caller bypasses more than half the time.
                   Trigger pattern is too generous.
                 Recency suppression (g-115-1509): if the RECENT window
                 (TIGHTEN_RECENCY_DAYS) holds >= MIN_RATE_SAMPLES block+override
                 AND its override rate has dropped to <= tighten_threshold, the
                 over-firing has been addressed (e.g. a widened whitelist) and
                 the high cumulative rate is lag — suppress tighten and fall
                 through to keep. Suppresses ONLY on positive evidence of
                 improvement; insufficient recent samples → tighten preserved
                 (a low-volume, steadily-over-firing gate stays flagged).

  widen          noop / total >= widen_threshold (0.95)
                 AND total >= MIN_TOTAL_FIRINGS
                 AND retirement_eligible: true
                 AND keyword_bias == "generous" (i.e., FN-dominant intent)
                 AND NOT recently-widened (recency suppression, below)
                 → Gate almost never triggers; may be missing real cases.
                 Recency suppression (g-115-1510): if the RECENT window
                 (WIDEN_RECENCY_DAYS) holds >= MIN_TOTAL_FIRINGS total firings
                 AND its noop rate has dropped below widen_threshold, the gate
                 has started catching cases (e.g. trigger patterns were added)
                 and the high cumulative noop rate is lag — suppress widen and
                 fall through to keep. The mirror image of the tighten recency
                 suppression: measure the CURRENT under-firing rate, not the
                 lagging one. Suppresses ONLY on positive evidence the gate now
                 fires; insufficient recent samples → widen preserved.

  investigate    fail_open count > 0
                 → Gate threw an exception at least once. Look at the
                   recorded gate_error and fix.

  inert_candidate  (post-scoring transform, g-115-2108) an action
                 recommendation (retire/tighten/widen) whose gate had ZERO
                 firings in the recent window AND whose implementation
                 (wrapper script or gates/ module) was git-modified AFTER
                 the gate's last recorded firing. Every data point behind
                 the recommendation predates the current implementation, so
                 prescribing tighten/widen from it re-flags already-fixed
                 gates forever (canonical: stale-read-gate — g-115-1796
                 neutered it 2026-07-06, firings end 2026-06-22, yet the
                 06-14..06-22 override rate kept re-recommending tighten;
                 spawned duplicate goals g-115-1796 → g-115-2106). The
                 recency suppressions above cannot catch this: they
                 suppress only on POSITIVE recent evidence, and an inert
                 gate has no recent samples at all. Consumer action: verify
                 live behavior; honest disposition is usually retirement or
                 telemetry re-enable, never the preserved recommendation.
                 evidence.inert_prior_recommendation carries the converted
                 value. Fail-open: git unavailable / paths missing → no
                 transform (original recommendation stands).

  keep           None of the above; gate behaving within expected envelope.

  insufficient_data   total firings < MIN_TOTAL_FIRINGS
                      → Cannot make a confident recommendation yet.

  uninstrumented      gate.instrumented: false in gates.yaml
                      → Gate has no telemetry to evaluate.

Contract: never raises. A bad firing record is skipped with a stderr WARN.
A missing gates.yaml is fatal (exit 2).
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from _paths import META_DIR, CONFIG_DIR, PROJECT_ROOT
# Decision enum — single source of truth in _gate_log.py. Importing (not
# redeclaring) means any future change to the taxonomy lands in one place;
# mismatches between writer and reader become impossible by construction.
from _gate_log import _VALID_DECISIONS, firings_paths
from itertools import chain as _chain

GATES_YAML = CONFIG_DIR / "gates.yaml"
FIRINGS_JSONL = META_DIR / "gate-firings.jsonl"

TIGHTEN_THRESHOLD = 0.5  # override / (block + override) above this → tighten
WIDEN_THRESHOLD = 0.95   # noop / total at or above this → widen (FN-only)
FAIL_OPEN_RECENCY_DAYS = 7  # fail_opens older than this are HISTORICAL, not a
                            # current bug. The investigate-on-fail_open rule
                            # windows on recency so a long-resolved exception
                            # burst inside the --days window stops re-flagging
                            # every eval run (rb-1531 recency-windowing pattern;
                            #  — origin-signal/capability/stale-read
                            # each carried a May fail_open burst, 0 in last 7d).
TIGHTEN_RECENCY_DAYS = 7    # block/override firings older than this are
                            # HISTORICAL for the tighten signal. The tighten rule
                            # measures override / (block + override) over the full
                            # --days window; a gate FIXED inside that window
                            # (widened whitelist, tightened pattern) keeps its
                            # PRE-fix high-override firings for ~--days, so the
                            # cumulative rate stays above threshold and the eval
                            # re-recommends tighten ~25d after the fix landed
                            # (: origin-signal-gate re-flagged at 50.2%
                            # on 2026-06-16 though  fixed it 2026-06-14;
                            # verified post-fix rate 30.6%). Symmetric to
                            # FAIL_OPEN_RECENCY_DAYS (rb-1531 — cumulative
                            # counters are not current weaknesses): measure the
                            # CURRENT over-firing rate, not the lagging one.
WIDEN_RECENCY_DAYS = 7      # noop/total firings older than this are HISTORICAL
                            # for the widen signal. The widen rule measures
                            # noop / total over the full --days window; a gate
                            # WIDENED inside that window (trigger patterns added
                            # so it catches more) keeps its PRE-widen high-noop
                            # firings for ~--days, so the cumulative noop rate
                            # stays >= widen_threshold and the eval re-recommends
                            # widen long after the fix landed — the identical
                            # lagging-window staleness as tighten, mirror-imaged
                            # (, follow-on from ). Symmetric
                            # to TIGHTEN_RECENCY_DAYS / FAIL_OPEN_RECENCY_DAYS
                            # (rb-1931 — recency-window the recommendation signal,
                            # not just the cumulative rate): measure the CURRENT
                            # under-firing rate, not the lagging one.


def _load_gates():
    """Load gates.yaml. Return list of gate dicts. Fatal if file is missing.

    Reads ONLY `gates:` — the audited+instrumented list. The sibling
    `pending_audit:` list (gates discovered but not yet asymmetry-analyzed)
    is INTENTIONALLY excluded: scoring gates without instrumented telemetry
    would manufacture noise. Promote pending_audit entries by completing
    their audit row in gates.yaml; until then they are invisible here.
    """
    if not GATES_YAML.is_file():
        print(f"[gate-retirement-eval] FATAL: {GATES_YAML} not found.",
              file=sys.stderr)
        sys.exit(2)
    try:
        data = yaml.safe_load(GATES_YAML.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[gate-retirement-eval] FATAL: {GATES_YAML} parse error: {e}",
              file=sys.stderr)
        sys.exit(2)
    return data.get("gates", []) or []


def _load_firings(since):
    """Yield firing records with ts >= `since`. Skip malformed lines (WARN)."""
    # : read the store through its composition seam, not a hardcoded
    # filename, so date segments are picked up with no change here. Today this
    # resolves to exactly FIRINGS_JSONL.
    paths = firings_paths(FIRINGS_JSONL.parent)
    if not paths:
        return
    skipped = 0
    for line in _chain.from_iterable(
            p.read_text(encoding="utf-8").splitlines() for p in paths):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            skipped += 1
            continue
        ts_raw = rec.get("ts")
        if not isinstance(ts_raw, str):
            skipped += 1
            continue
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            skipped += 1
            continue
        # decision is guaranteed valid by _gate_log.log() (invalid/null coerces
        # to "fail_open" at write time). A record reaching here without a valid
        # decision indicates schema drift or a non-canonical writer — skip with
        # a WARN so the count surfaces rather than silently corrupting stats.
        if rec.get("decision") not in _VALID_DECISIONS:
            skipped += 1
            continue
        if ts >= since:
            yield rec
    if skipped:
        print(f"[gate-retirement-eval] WARN: skipped {skipped} malformed "
              f"firing record(s)", file=sys.stderr)


def _gate_impl_paths(script_str):
    """Return existing implementation paths for a gate's `script:` value.

    Covers BOTH layers of the wrapper/engine split: the gates.yaml `script:`
    wrapper (e.g. core/scripts/stale-read-gate.py) AND the gates/ module the
    wrapper delegates to (core/scripts/gates/stale_read.py — derived by
    stripping a trailing `-gate` from the stem and swapping hyphens for
    underscores; only included when it exists on disk). The engine module is
    where behavior changes usually land (g-115-1796 touched gates/stale_read.py,
    not the wrapper), so watching the wrapper alone would miss most fixes.
    """
    paths = []
    p = (PROJECT_ROOT / script_str).resolve() if not Path(script_str).is_absolute() \
        else Path(script_str)
    if p.is_file():
        paths.append(p)
    stem = Path(script_str).stem
    if stem.endswith("-gate"):
        stem = stem[: -len("-gate")]
    module = PROJECT_ROOT / "core" / "scripts" / "gates" / (stem.replace("-", "_") + ".py")
    if module.is_file() and module not in paths:
        paths.append(module)
    return paths


def _gate_modified_epoch(script_str):
    """Latest git commit epoch across a gate's implementation paths.

    Returns (epoch:int, iso:str) of the newest commit touching any impl path,
    or (None, None) when git is unavailable, the paths have no history, or
    anything else goes wrong — fail-open, so the inert transform simply does
    not fire and the original recommendation stands.
    """
    best_epoch, best_iso = None, None
    for p in _gate_impl_paths(script_str):
        try:
            out = subprocess.run(
                ["git", "log", "-1", "--format=%ct %cI", "--", str(p)],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                timeout=15)
            line = (out.stdout or "").strip()
            if out.returncode != 0 or not line:
                continue
            epoch_s, iso = line.split(" ", 1)
            epoch = int(epoch_s)
            if best_epoch is None or epoch > best_epoch:
                best_epoch, best_iso = epoch, iso
        except Exception:
            continue
    return best_epoch, best_iso


# Recommendations the inert transform may convert. investigate is excluded:
# a recent-window fail_open is itself recency-gated, and a historical-only
# fail_open already falls through to the asymmetry rules.
_INERT_CONVERTIBLE = ("retire", "tighten", "widen")

# Cost-rank map for the all-noop cost-asymmetry downgrade ().
# Unknown / missing values rank None → the cost side contributes no downgrade
# (fail toward pre-existing behavior; taxonomy drift adds a rank here, it
# never silently flips a recommendation).
_COST_RANK = {"low": 0, "medium": 1, "high": 2}

# Minimum gate age before ACTION recommendations are trusted ().
# An embedded per-goal-write classifier reaches MIN_TOTAL_FIRINGS in days —
# volume is not maturity. Gates younger than min(--days, this) get their
# action recommendation replaced by insufficient_data via
# _apply_min_age_transform (post-score, lazy git probe — same shape as the
# inert transform below).
MIN_GATE_AGE_DAYS = 14

# Recommendations the min-age transform may convert — the action set.
# investigate is excluded on purpose: a YOUNG gate that is fail_open'ing
# (raising exceptions) still needs investigation NOW, and an all-noop
# FN-costly young gate routed to investigate is a cheap look, not a deletion.
_MIN_AGE_CONVERTIBLE = ("retire", "tighten", "widen")


def _gate_registry_first_epoch(gid):
    """Epoch of the commit that INTRODUCED `id: <gid>` into gates.yaml.

    Registry presence is the preferred age source while the firings ledger
    carries the g-115-2294 month-hole (missing older records make a gate's
    first ledger ts too RECENT, understating age exactly when the min-age
    rule needs it). Returns int epoch or None — fail-open: no git, no match,
    or any error → None and the caller falls back to the ledger bound.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-S", f"id: {gid}", "--format=%ct", "--reverse",
             "--", "core/config/gates.yaml"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=20)
        first = (out.stdout or "").strip().splitlines()
        if out.returncode != 0 or not first:
            return None
        return int(first[0])
    except Exception:
        return None


def _apply_min_age_transform(scored, gate_age_days, window_days):
    """Replace an action recommendation on a too-young gate ().

    Pure function (no I/O) so the self-test can exercise it synthetically.
    `gate_age_days` None → age unknown (git + ledger both failed) → no
    transform (fail-open, original recommendation stands). Threshold is
    min(window_days, MIN_GATE_AGE_DAYS): a deliberately short --days window
    should not be blocked by the default age floor.
    """
    if scored.get("recommendation") not in _MIN_AGE_CONVERTIBLE:
        return scored
    if gate_age_days is None:
        return scored
    threshold = min(window_days, MIN_GATE_AGE_DAYS) if window_days \
        else MIN_GATE_AGE_DAYS
    evidence = scored.setdefault("evidence", {})
    evidence["gate_age_days"] = round(gate_age_days, 1)
    evidence["gate_age_threshold_days"] = threshold
    if gate_age_days >= threshold:
        return scored
    original = scored["recommendation"]
    evidence["pre_min_age_recommendation"] = original
    scored["recommendation"] = "insufficient_data"
    scored["reason"] = (
        f"Gate is {gate_age_days:.1f}d old (< {threshold}d min age) — "
        f"volume is not maturity (an embedded per-goal-write classifier "
        f"reaches MIN_TOTAL_FIRINGS in days). Withholding the "
        f"'{original}' recommendation until the gate has aged; re-eval "
        f"after {threshold}d. (g-115-2295)")
    return scored


def _apply_inert_transform(scored, recent_total,
                           last_firing_epoch, last_firing_ts,
                           modified_epoch, modified_ts):
    """Convert a stale action recommendation to inert_candidate ().

    Pure function (no I/O) so the self-test can exercise it synthetically.
    Converts ONLY when ALL hold:
      - recommendation is retire/tighten/widen (action derived from firings)
      - recent_total == 0 (the gate produced NO firings of any decision in
        the recent window — nothing current backs the recommendation)
      - both timestamps known (fail-open on missing git data)
      - the implementation was modified STRICTLY AFTER the last firing
        (every data point predates the current implementation)
    Anything else returns `scored` unchanged.
    """
    if scored.get("recommendation") not in _INERT_CONVERTIBLE:
        return scored
    if recent_total != 0:
        return scored
    if last_firing_epoch is None or modified_epoch is None:
        return scored
    if modified_epoch <= last_firing_epoch:
        return scored
    prior = scored["recommendation"]
    ev = scored.setdefault("evidence", {})
    ev["inert_prior_recommendation"] = prior
    ev["last_firing_ts"] = last_firing_ts
    ev["gate_impl_modified_ts"] = modified_ts
    scored["recommendation"] = "inert_candidate"
    scored["reason"] = (
        f"Prior recommendation '{prior}' rests entirely on pre-modification "
        f"data: zero firings in the recent window, last firing "
        f"{last_firing_ts}, but the gate implementation was modified after it "
        f"({modified_ts}). Prescribing '{prior}' from stale telemetry re-flags "
        f"an already-changed gate (g-115-2108; canonical: stale-read-gate). "
        f"Verify live behavior — honest disposition is usually retirement or "
        f"telemetry re-enable, not '{prior}'.")
    return scored


def _score_gate(gate, counts, min_fires,
                recent_fail_open=None, latest_fail_open_ts=None,
                recent_counts=None, recent_counts_widen=None):
    """Apply recommendation rules to one gate's count summary.

    Returns dict with `recommendation`, `reason`, and the raw evidence.

    `min_fires` is the single CLI knob, applied inside the function as two
    named thresholds — one per distinct sample set.

    `recent_fail_open` windows the investigate-on-fail_open rule on recency
    (rb-1531). It is the count of fail_open firings within the last
    FAIL_OPEN_RECENCY_DAYS, computed by the aggregation loop:
      - None  → caller did not supply a recency split (self-test / legacy
                callers). Preserve the original behavior: any fail_open in
                the --days window → investigate. This keeps the existing
                self-test contract intact.
      - >0    → a fail_open fired recently → investigate (current bug).
      - ==0   → all fail_opens are HISTORICAL (older than the recency
                window). The gate raised exceptions in the past but not
                lately — a long-resolved burst, not a current bug. Fall
                through to the normal asymmetry rules instead of re-flagging
                every eval run (the g-115-1368 failure mode).
    `latest_fail_open_ts` is the ISO timestamp of the most recent fail_open
    (for evidence/audit only); None when there are no fail_opens.

    `recent_counts` is a Counter of decisions within TIGHTEN_RECENCY_DAYS,
    driving the tighten recency-improvement suppression (g-115-1509):
      - None    → caller supplied no recency split (self-test / legacy).
                  Tighten fires on the full-window rate alone — the original
                  behavior, preserved so the existing self-test contract holds.
      - Counter → suppress tighten ONLY when recent (block + override) >=
                  MIN_RATE_SAMPLES AND the recent override rate has dropped to
                  <= TIGHTEN_THRESHOLD (positive evidence the over-firing was
                  fixed). Insufficient recent samples, or a still-high recent
                  rate, → tighten preserved (no false suppression).

    `recent_counts_widen` is a Counter of ALL decisions within
    WIDEN_RECENCY_DAYS, driving the widen recency-improvement suppression
    (g-115-1510) — the mirror image of `recent_counts` for the widen path:
      - None    → caller supplied no recency split (self-test / legacy).
                  Widen fires on the full-window noop rate alone — the original
                  behavior, preserved so the existing self-test contract holds.
      - Counter → suppress widen ONLY when recent total >= MIN_TOTAL_FIRINGS
                  AND the recent noop rate has dropped below WIDEN_THRESHOLD
                  (positive evidence the gate now catches cases). Insufficient
                  recent samples, or a still-high recent noop rate, → widen
                  preserved (no false suppression).
    """
    # Two volume guards — named for the sample set each gates, NOT the
    # threshold value. Do not collapse them: MIN_RATE_SAMPLES gates
    # `block + override` (the rate-stability sample), which is a proper
    # subset of the `total` that MIN_TOTAL_FIRINGS gates. A future
    # `--min-rate-samples` override can diverge the rate guard without
    # touching the volume guard — that is the point of the split.
    MIN_TOTAL_FIRINGS = min_fires   # gates `total`: retire + widen + insufficient_data
    MIN_RATE_SAMPLES = min_fires    # gates `block + override`: tighten only

    gid = gate["id"]
    instrumented = gate.get("instrumented", False)
    retirement_eligible = gate.get("retirement_eligible", False)
    keyword_bias = gate.get("keyword_bias", "balanced")
    dominant_risk = gate.get("dominant_risk")

    total = sum(counts.values())
    noop = counts.get("noop", 0)
    pass_ = counts.get("pass", 0)
    block = counts.get("block", 0)
    override = counts.get("override", 0)
    fail_open = counts.get("fail_open", 0)
    meaningful = total - noop  # decision != noop

    evidence = {
        "total_firings": total,
        "noop": noop,
        "pass": pass_,
        "block": block,
        "override": override,
        "fail_open": fail_open,
        "meaningful_firings": meaningful,
        "keyword_bias": keyword_bias,
        "retirement_eligible": retirement_eligible,
        "asymmetry_magnitude": gate.get("asymmetry_magnitude"),
        "dominant_risk": dominant_risk,
    }

    if not instrumented:
        return {"recommendation": "uninstrumented",
                "reason": "Gate has instrumented: false in gates.yaml; "
                          "no telemetry to evaluate.",
                "evidence": evidence}

    if total == 0:
        return {"recommendation": "insufficient_data",
                "reason": f"Zero firings in window — gate may not be on any "
                          f"hot path, or telemetry was just enabled.",
                "evidence": evidence}

    # Fail-open is a hard signal — gate raised an exception. But "raised an
    # exception once, 4 weeks ago, since resolved" is NOT a current bug.
    # Window the investigate trigger on recency (rb-1531): a fail_open inside
    # the recency window is a live bug; a fail_open only OUTSIDE it is history.
    if fail_open > 0:
        evidence["fail_open_recent"] = recent_fail_open
        evidence["fail_open_recency_days"] = FAIL_OPEN_RECENCY_DAYS
        evidence["latest_fail_open_ts"] = latest_fail_open_ts
        if recent_fail_open is None:
            # Legacy / self-test caller did not supply a recency split.
            # Preserve original behavior: any fail_open → investigate.
            return {"recommendation": "investigate",
                    "reason": f"Gate fail_open count = {fail_open}. The gate "
                              f"raised an exception at least once. Inspect the "
                              f"`gate_error` field on those firings and fix.",
                    "evidence": evidence}
        if recent_fail_open > 0:
            return {"recommendation": "investigate",
                    "reason": f"Gate fail_open count = {fail_open} "
                              f"({recent_fail_open} in the last "
                              f"{FAIL_OPEN_RECENCY_DAYS}d; latest "
                              f"{latest_fail_open_ts}). The gate raised an "
                              f"exception recently. Inspect the `gate_error` "
                              f"field on those firings and fix.",
                    "evidence": evidence}
        # recent_fail_open == 0: every fail_open is older than the recency
        # window. Historical burst, not a current bug — do NOT early-return.
        # Fall through to the normal asymmetry rules. The cumulative count
        # is preserved in evidence["fail_open"] for the audit trail.

    # Total-volume insufficient_data check uses MIN_TOTAL_FIRINGS (the retire
    # rule's guard). This uses `total`, not `meaningful` — a gate with 100
    # noop firings is exactly the case widen wants to surface; gating widen
    # behind meaningful-firing count is self-defeating.
    if total < MIN_TOTAL_FIRINGS:
        return {"recommendation": "insufficient_data",
                "reason": f"Only {total} total firing(s) "
                          f"(MIN_TOTAL_FIRINGS={MIN_TOTAL_FIRINGS}). "
                          f"Need more data before scoring.",
                "evidence": evidence}

    # Retire path: meaningful firings == 0 AND enough total firings.
    # The MIN_TOTAL_FIRINGS guard above prevents a single smoke-test noop
    # on an under-exercised gate from triggering false retirement.
    if meaningful == 0 and retirement_eligible:
        # FN-dominant guard (rb 2026-05-28, canonical gate prose-verification-drift):
        # an all-noop gate with dominant_risk=FN is typically a WORKING preventive
        # guard whose rare, costly guarded condition simply didn't occur in-window
        # -- retiring it removes the guard exactly when it's quiet. Route to
        # investigate, not retire: decide whether it's correctly scoped for a rare
        # event (keep) or mis-wired (widen/fix). Without this, the retire rule
        # preempts the widen path (unreachable when meaningful==0) and deletes a
        # guard that never had a chance to fire.
        #
        # Cost-asymmetry key (): dominant_risk is a LIKELIHOOD label
        # (which error mode is more probable), not a cost label — gates.yaml
        # registers e.g. removal-intent-verbs as FP because the strict matcher
        # over-matches, while its COST shape (fn_cost=medium > fp_cost=low) is
        # the same as the canonical FN gate. Keying the downgrade only on
        # dominant_risk==FN retire-recommended that gate (). Fire the
        # downgrade when EITHER key says the quiet side is the expensive side:
        # dominant_risk==FN OR fn_cost outranks fp_cost (rank low<medium<high;
        # unknown/missing ranks → no cost-side downgrade, taxonomy-drift-proof).
        fp_rank = _COST_RANK.get(str(gate.get("fp_cost", "")).lower())
        fn_rank = _COST_RANK.get(str(gate.get("fn_cost", "")).lower())
        fn_outranks_fp = (fp_rank is not None and fn_rank is not None
                          and fn_rank > fp_rank)
        evidence["fp_cost"] = gate.get("fp_cost")
        evidence["fn_cost"] = gate.get("fn_cost")
        if dominant_risk == "FN" or fn_outranks_fp:
            key_desc = ("dominant_risk=FN" if dominant_risk == "FN"
                        else f"fn_cost={gate.get('fn_cost')} outranks "
                             f"fp_cost={gate.get('fp_cost')}")
            return {"recommendation": "investigate",
                    "reason": f"Gate fired {total} times over window, all noops, "
                              f"but {key_desc}. An all-noop gate whose false-"
                              f"negative side is the expensive side is typically "
                              f"a working preventive guard whose rare guarded "
                              f"condition didn't occur in-window, NOT a dead "
                              f"gate. Investigate whether it is correctly scoped "
                              f"for a rare event (keep) or mis-wired (widen/fix) "
                              f"before retiring.",
                    "evidence": evidence}
        return {"recommendation": "retire",
                "reason": f"Gate fired {total} times over window, all noops "
                          f"(no trigger ever matched). Has never produced a "
                          f"pass/block/override decision. Delete it.",
                "evidence": evidence}
    if meaningful == 0 and not retirement_eligible:
        return {"recommendation": "keep",
                "reason": f"Gate fired {total}× (all noops) but is marked "
                          f"retirement_eligible: false (structural). Keep.",
                "evidence": evidence}

    # Tighten path: FP signal — override rate is high. Gated by
    # MIN_RATE_SAMPLES on (block + override), since the rate needs enough
    # samples to be stable independent of total firings.
    bo_total = block + override
    if bo_total > 0:
        override_rate = override / bo_total
        evidence["override_rate"] = round(override_rate, 3)
        if override_rate > TIGHTEN_THRESHOLD and bo_total >= MIN_RATE_SAMPLES:
            # Recency-improvement suppression (). The full-window
            # override rate lags a fix by ~`--days`: a gate fixed inside the
            # window keeps its pre-fix high-override firings, so the cumulative
            # rate re-recommends tighten long after the fix landed. If the
            # RECENT window (TIGHTEN_RECENCY_DAYS) holds enough samples AND
            # shows the rate dropped to/below threshold, the over-firing was
            # addressed — the cumulative rate is lag, not a current bug.
            # Symmetric to the fail_open recency window above (rb-1531).
            # Suppress ONLY on positive evidence of improvement: recent_counts
            # is None (self-test/legacy) or recent samples insufficient →
            # tighten preserved, so a low-volume steadily-over-firing gate
            # stays flagged.
            suppress_tighten = False
            if recent_counts is not None:
                r_block = recent_counts.get("block", 0)
                r_override = recent_counts.get("override", 0)
                r_bo = r_block + r_override
                if r_bo >= MIN_RATE_SAMPLES:
                    r_rate = r_override / r_bo
                    evidence["override_rate_recent"] = round(r_rate, 3)
                    evidence["bo_total_recent"] = r_bo
                    evidence["tighten_recency_days"] = TIGHTEN_RECENCY_DAYS
                    if r_rate <= TIGHTEN_THRESHOLD:
                        suppress_tighten = True
                        evidence["tighten_suppressed_reason"] = (
                            f"Full-window override rate {override_rate:.0%} "
                            f"exceeds threshold, but the recent "
                            f"{TIGHTEN_RECENCY_DAYS}d window "
                            f"({r_block} block + {r_override} override = "
                            f"{r_bo}) shows {r_rate:.0%} <= "
                            f"{TIGHTEN_THRESHOLD:.0%}. Over-firing has been "
                            f"addressed; the cumulative rate is lag. "
                            f"Suppressing tighten (g-115-1509).")
            if not suppress_tighten:
                return {"recommendation": "tighten",
                        "reason": (
                            f"Override rate {override_rate:.0%} of "
                            f"({block} block + {override} override) = "
                            f"{bo_total} firings (MIN_RATE_SAMPLES="
                            f"{MIN_RATE_SAMPLES}) exceeds tighten threshold "
                            f"{TIGHTEN_THRESHOLD:.0%}. Caller bypasses more often "
                            f"than the gate blocks — trigger pattern is too "
                            f"generous. Tighten or remove patterns that fire "
                            f"on legitimate work."),
                        "evidence": evidence}
            # suppressed → fall through to widen / keep below

    # Widen path: gate almost never triggers, AND it's FN-dominant. Volume
    # guard is MIN_TOTAL_FIRINGS, enforced by the early return above —
    # widen and retire share the same `total` sample set.
    noop_rate = noop / total
    evidence["noop_rate"] = round(noop_rate, 3)
    if (noop_rate >= WIDEN_THRESHOLD
            and retirement_eligible
            and keyword_bias == "generous"):
        # Recency-improvement suppression (). The full-window noop
        # rate lags a widen fix by ~`--days`: a gate widened inside the window
        # keeps its pre-widen high-noop firings, so the cumulative rate
        # re-recommends widen long after the patterns were added. If the RECENT
        # window (WIDEN_RECENCY_DAYS) holds enough total firings AND shows the
        # noop rate dropped below threshold, the gate now catches cases — the
        # cumulative rate is lag, not a current FN gap. Mirror image of the
        # tighten recency suppression above (rb-1931). Suppress ONLY on positive
        # evidence the gate now fires: recent_counts_widen is None
        # (self-test/legacy) or recent samples insufficient → widen preserved,
        # so a low-volume steadily-under-firing gate stays flagged.
        suppress_widen = False
        if recent_counts_widen is not None:
            r_total = sum(recent_counts_widen.values())
            if r_total >= MIN_TOTAL_FIRINGS:
                r_noop = recent_counts_widen.get("noop", 0)
                r_noop_rate = r_noop / r_total
                evidence["noop_rate_recent"] = round(r_noop_rate, 3)
                evidence["total_recent"] = r_total
                evidence["widen_recency_days"] = WIDEN_RECENCY_DAYS
                if r_noop_rate < WIDEN_THRESHOLD:
                    suppress_widen = True
                    evidence["widen_suppressed_reason"] = (
                        f"Full-window noop rate {noop_rate:.0%} meets the widen "
                        f"threshold, but the recent {WIDEN_RECENCY_DAYS}d window "
                        f"({r_noop} noop / {r_total} total) shows "
                        f"{r_noop_rate:.0%} < {WIDEN_THRESHOLD:.0%}. The gate now "
                        f"catches cases; the cumulative rate is lag. "
                        f"Suppressing widen (g-115-1510).")
        if not suppress_widen:
            return {"recommendation": "widen",
                    "reason": (
                        f"Noop rate {noop_rate:.0%} ≥ widen threshold "
                        f"{WIDEN_THRESHOLD:.0%} over {total} firings "
                        f"(MIN_TOTAL_FIRINGS={MIN_TOTAL_FIRINGS}) and gate is "
                        f"FN-dominant (keyword_bias=generous). Gate is missing "
                        f"real cases — add trigger patterns. Compare to "
                        f"gates.yaml fn_description for hints on what's escaping."),
                    "evidence": evidence}
        # suppressed → fall through to keep below

    return {"recommendation": "keep",
            "reason": "Gate behaving within expected envelope — neither "
                      "over-firing nor under-firing.",
            "evidence": evidence}


def _human_table(rows):
    """Compact human-readable summary."""
    lines = []
    by_rec = Counter(r["recommendation"] for r in rows)
    lines.append("=== Gate Retirement Recommendations ===")
    lines.append(f"Total gates evaluated: {len(rows)}")
    lines.append("Distribution:")
    for rec in ("retire", "tighten", "widen", "investigate", "inert_candidate",
                "keep", "insufficient_data", "uninstrumented"):
        if by_rec.get(rec):
            lines.append(f"  {by_rec[rec]:3d}  {rec}")
    lines.append("")
    lines.append(f"{'GATE ID':<32s} {'REC':<18s} {'TOTAL':>6s} {'NOOP':>6s} "
                 f"{'BLK':>5s} {'OVR':>5s} {'FAIL':>5s}")
    lines.append("-" * 84)
    for r in rows:
        ev = r["evidence"]
        lines.append(
            f"{r['gate_id']:<32s} {r['recommendation']:<18s} "
            f"{ev['total_firings']:>6d} {ev['noop']:>6d} "
            f"{ev['block']:>5d} {ev['override']:>5d} {ev['fail_open']:>5d}")
    lines.append("")
    # Surface the action items first.
    lines.append("Action items (anything not 'keep' / 'insufficient_data' / 'uninstrumented'):")
    actions = [r for r in rows if r["recommendation"] in
               ("retire", "tighten", "widen", "investigate", "inert_candidate")]
    if not actions:
        lines.append("  (none — all gates within expected envelope)")
    for r in actions:
        lines.append(f"  • {r['gate_id']} → {r['recommendation']}")
        lines.append(f"      {r['reason']}")
    return "\n".join(lines)


def _self_test():
    """Synthetic-input regression test for the recommendation rules.

    Covers every recommendation value (retire, tighten, widen, investigate,
    keep, insufficient_data, uninstrumented) plus the suppression paths that
    can flip one into another — the case count grows as new rules or gates
    land. Every branch in `_score_gate` must have at least one case that
    exercises it; see below for the exact branch-to-case mapping.

    Historical failure modes guarded:
      - retire false-positive on under-exercised gates (total < MIN_TOTAL_FIRINGS)
      - retire false-positive on FN-dominant all-noop preventive guards
        (dominant_risk=FN must route to investigate, not retire)
      - widen rule eaten by a meaningful-firings volume guard
      - MIN_RATE_SAMPLES collapsed into MIN_TOTAL_FIRINGS (tighten fires on
        total-volume instead of rate-sample-volume)
      - any future rule-ordering regression that flips a recommendation

    Exits 0 on all-pass, 1 on any failure (machine-readable for CI use).
    Prints a one-line summary per case so failures are diagnosable from
    stdout alone.
    """
    min_fires = 5
    cases = [
        # (label, gate dict, counts dict, expected_recommendation)
        ("retire-pure-noise",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 15}),
         "retire"),
        # FN-dominant retire false-positive guard (rb 2026-05-28): identical
        # counts to retire-pure-noise; only dominant_risk=FN flips the outcome
        # to investigate. An all-noop FN-dominant gate is a working preventive
        # guard (canonical: prose-verification-drift), not a dead gate. Without
        # the dominant_risk guard in _score_gate, the retire rule would delete it.
        ("fn-dominant-all-noop-investigate-not-retire",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced", "dominant_risk": "FN"},
         Counter({"noop": 15}),
         "investigate"),
        # Cost-asymmetry key (, canonical gate removal-intent-verbs):
        # dominant_risk=FP is a LIKELIHOOD label; the COST shape
        # (fn_cost=medium > fp_cost=low) says the quiet side is the expensive
        # side. The downgrade must fire on cost asymmetry independent of the
        # dominant_risk label — this pins the exact  miss.
        ("fp-labeled-fn-costly-all-noop-investigate-not-retire",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict", "dominant_risk": "FP",
          "fp_cost": "low", "fn_cost": "medium"},
         Counter({"noop": 15}),
         "investigate"),
        # Equal costs + FP label → the cost key must NOT over-fire; plain
        # retire behavior preserved for genuinely-cheap-both-ways gates.
        ("fp-labeled-equal-costs-all-noop-still-retire",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced", "dominant_risk": "FP",
          "fp_cost": "low", "fn_cost": "low"},
         Counter({"noop": 15}),
         "retire"),
        # Unknown cost vocab (taxonomy drift) → rank None → no cost-side
        # downgrade; original retire stands (fail toward existing behavior).
        ("unknown-cost-values-no-downgrade",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced", "dominant_risk": "FP",
          "fp_cost": "trivial", "fn_cost": "catastrophic"},
         Counter({"noop": 15}),
         "retire"),
        ("retire-suppressed-by-low-volume",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 1}),
         "insufficient_data"),
        ("retire-suppressed-by-not-eligible",
         {"id": "_test", "instrumented": True, "retirement_eligible": False,
          "keyword_bias": "balanced"},
         Counter({"noop": 15}),
         "keep"),
        ("tighten-high-override-rate",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict"},
         Counter({"block": 5, "override": 12, "noop": 1}),
         "tighten"),
        # Regression guard: MIN_RATE_SAMPLES gates `bo_total` (block+override),
        # NOT `total`. If anyone collapses it back into MIN_TOTAL_FIRINGS
        # (e.g. checks `total >= min_fires` instead of `bo_total >= min_fires`
        # on the tighten path), this case flips from "keep" → "tighten"
        # because total=10 is above min_fires while bo_total=4 is not.
        ("tighten-needs-rate-samples-distinct-from-total",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict"},
         Counter({"noop": 6, "block": 1, "override": 3}),
         "keep"),
        # ── Tighten recency-improvement suppression () ──────────
        # Full-window override rate is high enough to tighten, but the RECENT
        # window (recent_counts, position 5) shows the rate dropped below
        # threshold with enough samples → the over-firing was fixed, the
        # cumulative rate is lag → suppress tighten, fall through to keep.
        # Mirrors the real origin-signal-gate case: full window 50%+, recent 7d
        # 30.6% (11 override / 36) after  widened the whitelist.
        ("tighten-suppressed-by-recent-improvement",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict"},
         Counter({"block": 30, "override": 40, "noop": 1}),   # full 40/70 = 57%
         "keep",
         None,                                                 # recent_fail_open
         Counter({"block": 25, "override": 11})),              # recent 11/36 = 31% <= 50%
        # Insufficient recent samples → NO suppression: a low-volume steadily-
        # over-firing gate must stay flagged. recent bo=3 < MIN_RATE_SAMPLES(5).
        ("tighten-preserved-insufficient-recent-samples",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict"},
         Counter({"block": 30, "override": 40, "noop": 1}),   # full 57%
         "tighten",
         None,
         Counter({"block": 2, "override": 1})),                # recent bo=3 < 5
        # Recent rate ALSO high → genuine current over-firing → tighten stands.
        ("tighten-recent-also-high-still-tightens",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "strict"},
         Counter({"block": 30, "override": 40, "noop": 1}),   # full 57%
         "tighten",
         None,
         Counter({"block": 5, "override": 12})),               # recent 12/17 = 71% > 50%
        ("widen-high-noop-rate-fn-dominant",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "generous"},
         Counter({"noop": 62, "block": 3}),
         "widen"),
        ("widen-suppressed-by-balanced-bias",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 62, "block": 3}),
         "keep"),
        # ── Widen recency-improvement suppression () ─────────────
        # Full-window noop rate is high enough to widen, but the RECENT window
        # (recent_counts_widen, position 6) shows the noop rate dropped below
        # threshold with enough total firings → the gate now catches cases, the
        # cumulative rate is lag → suppress widen, fall through to keep. Mirror
        # image of tighten-suppressed-by-recent-improvement. Positions [4]/[5]
        # are None so the recent_counts_widen lands at [6].
        ("widen-suppressed-by-recent-improvement",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "generous"},
         Counter({"noop": 60, "block": 3}),                  # full 60/63 = 95.2%
         "keep",
         None,                                                # recent_fail_open
         None,                                                # recent_counts (tighten)
         Counter({"noop": 5, "block": 5})),                   # recent 5/10 = 50% < 95%
        # Insufficient recent samples → NO suppression: a low-volume steadily-
        # under-firing gate must stay flagged. recent total=3 < MIN_TOTAL_FIRINGS(5).
        ("widen-preserved-insufficient-recent-samples",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "generous"},
         Counter({"noop": 60, "block": 3}),                  # full 95.2%
         "widen",
         None,
         None,
         Counter({"noop": 2, "block": 1})),                   # recent total=3 < 5
        # Recent noop rate ALSO high → genuine current under-firing → widen
        # stands. recent 19/20 = 95% is NOT < 95% (boundary: suppression needs
        # strictly below threshold), so widen is preserved.
        ("widen-recent-also-high-still-widens",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "generous"},
         Counter({"noop": 60, "block": 3}),                  # full 95.2%
         "widen",
         None,
         None,
         Counter({"noop": 19, "block": 1})),                  # recent 19/20 = 95% (not < 95%)
        ("investigate-on-fail-open",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"block": 1, "fail_open": 1}),
         "investigate"),
        # Recency-windowed fail_open (rb-1531 / ). 5-tuple: the
        # trailing int is recent_fail_open (count within FAIL_OPEN_RECENCY_DAYS).
        # recent>0 → live bug → investigate (same verdict as the legacy case
        # above, but now driven by the recency split, not the cumulative count).
        ("investigate-on-recent-fail-open",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"block": 1, "fail_open": 1}),
         "investigate", 1),
        # recent==0 → every fail_open is HISTORICAL (older than the window).
        # The cumulative fail_open count is non-zero, but the gate has not
        # raised an exception lately — fall through to the normal asymmetry
        # rules instead of re-flagging. This is the exact  fix: a
        # gate carrying a long-resolved May fail_open burst must NOT investigate
        # forever. Counts mirror keep-balanced-firings + 5 historical fail_opens,
        # so the expected verdict is the same "keep".
        ("keep-historical-fail-open-only",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 22, "block": 23, "override": 1, "fail_open": 5}),
         "keep", 0),
        ("keep-balanced-firings",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 22, "block": 23, "override": 1}),
         "keep"),
        ("insufficient-data-zero-firings",
         {"id": "_test", "instrumented": True, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter(),
         "insufficient_data"),
        ("uninstrumented-skipped",
         {"id": "_test", "instrumented": False, "retirement_eligible": True,
          "keyword_bias": "balanced"},
         Counter({"noop": 99}),
         "uninstrumented"),
    ]
    # ── Inert-transform cases () ────────────────────────────────
    # Pure-function tests for _apply_inert_transform — no git, no telemetry.
    # Tuples: (label, scored-dict, recent_total, last_epoch, mod_epoch, expected).
    T0, T1 = 1000.0, 2000.0  # T1 strictly after T0
    inert_cases = [
        # Action rec + zero recent + impl modified AFTER last firing → convert.
        ("inert-converts-stale-tighten",
         {"recommendation": "tighten", "reason": "x", "evidence": {}},
         0, T0, T1, "inert_candidate"),
        ("inert-converts-stale-retire",
         {"recommendation": "retire", "reason": "x", "evidence": {}},
         0, T0, T1, "inert_candidate"),
        # Impl modified BEFORE last firing → data post-dates the change →
        # the recommendation reflects the CURRENT implementation. Preserved.
        ("inert-preserved-modified-before-last-firing",
         {"recommendation": "tighten", "reason": "x", "evidence": {}},
         0, T1, T0, "tighten"),
        # Recent firings exist → gate is live → recency suppressions own the
        # staleness question. Preserved.
        ("inert-preserved-recent-firings-exist",
         {"recommendation": "tighten", "reason": "x", "evidence": {}},
         3, T0, T1, "tighten"),
        # Missing git data → fail-open, preserved.
        ("inert-preserved-no-git-data",
         {"recommendation": "widen", "reason": "x", "evidence": {}},
         0, T0, None, "widen"),
        # Non-action recommendations never convert.
        ("inert-ignores-keep",
         {"recommendation": "keep", "reason": "x", "evidence": {}},
         0, T0, T1, "keep"),
        ("inert-ignores-investigate",
         {"recommendation": "investigate", "reason": "x", "evidence": {}},
         0, T0, T1, "investigate"),
    ]

    failures = 0
    for case in cases:
        # Cases are positional tuples:
        #   [0] label  [1] gate  [2] counts  [3] expected
        #   [4] recent_fail_open (optional; None default — exercises the legacy
        #       "any fail_open → investigate" branch, preserving the original
        #       self-test contract)
        #   [5] recent_counts (optional Counter; None default — exercises the
        #       legacy "tighten fires on the full-window rate alone" branch,
        #       )
        #   [6] recent_counts_widen (optional Counter; None default — exercises
        #       the legacy "widen fires on the full-window noop rate alone"
        #       branch, )
        label, gate, counts, expected = case[0], case[1], case[2], case[3]
        recent = case[4] if len(case) > 4 else None
        recent_counts = case[5] if len(case) > 5 else None
        recent_counts_widen = case[6] if len(case) > 6 else None
        result = _score_gate(gate, counts, min_fires,
                             recent_fail_open=recent,
                             recent_counts=recent_counts,
                             recent_counts_widen=recent_counts_widen)
        actual = result["recommendation"]
        ok = actual == expected
        if not ok:
            failures += 1
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: expected={expected!r} actual={actual!r}")
    for label, scored, recent_total, last_epoch, mod_epoch, expected in inert_cases:
        result = _apply_inert_transform(
            dict(scored, evidence=dict(scored.get("evidence", {}))),
            recent_total, last_epoch, "2026-01-01T00:00:00",
            mod_epoch, "2026-01-02T00:00:00+00:00")
        actual = result["recommendation"]
        ok = actual == expected
        # Converted rows must preserve the prior recommendation in evidence.
        if ok and expected == "inert_candidate":
            ok = (result["evidence"].get("inert_prior_recommendation")
                  == scored["recommendation"])
        if not ok:
            failures += 1
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: expected={expected!r} actual={actual!r}")
    # Min-age transform cases (): pure-function checks mirroring the
    # inert_cases pattern. (label, scored, gate_age_days, window_days, expected)
    min_age_cases = [
        # 2.1d-old retire (the exact  shape) → insufficient_data.
        ("min-age-young-retire-to-insufficient",
         {"recommendation": "retire", "reason": "r", "evidence": {}},
         2.1, 30, "insufficient_data"),
        # Old gate → action recommendation untouched.
        ("min-age-old-gate-retire-stands",
         {"recommendation": "retire", "reason": "r", "evidence": {}},
         45.0, 30, "retire"),
        # Age unknown (git + ledger both failed) → fail-open, untouched.
        ("min-age-unknown-age-fail-open",
         {"recommendation": "retire", "reason": "r", "evidence": {}},
         None, 30, "retire"),
        # investigate is NOT in the convertible set — young fail_open'ing
        # gates still get investigated now.
        ("min-age-investigate-not-converted",
         {"recommendation": "investigate", "reason": "r", "evidence": {}},
         2.1, 30, "investigate"),
        # Short --days window lowers the threshold: 5d-old gate with a 3d
        # window is PAST min(3, 14) → recommendation stands.
        ("min-age-short-window-lowers-threshold",
         {"recommendation": "tighten", "reason": "r", "evidence": {}},
         5.0, 3, "tighten"),
    ]
    for label, scored, age_days, window_days, expected in min_age_cases:
        result = _apply_min_age_transform(
            dict(scored, evidence=dict(scored.get("evidence", {}))),
            age_days, window_days)
        actual = result["recommendation"]
        ok = actual == expected
        # Converted rows must preserve the prior recommendation in evidence.
        if ok and expected == "insufficient_data":
            ok = (result["evidence"].get("pre_min_age_recommendation")
                  == scored["recommendation"])
        if not ok:
            failures += 1
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: expected={expected!r} actual={actual!r}")
    total_cases = len(cases) + len(inert_cases) + len(min_age_cases)
    print()
    if failures:
        print(f"FAILED: {failures} of {total_cases} self-test case(s) failed.")
        return 1
    print(f"OK: all {total_cases} self-test case(s) passed.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Evaluate gate firings and recommend retirement / "
                    "tightening / widening per gate.")
    ap.add_argument("--days", type=int, default=30,
                    help="Look-back window in days (default 30).")
    ap.add_argument("--min-fires", type=int, default=10,
                    help="Volume threshold applied to both named guards "
                         "(MIN_TOTAL_FIRINGS for retire/widen/insufficient_data, "
                         "MIN_RATE_SAMPLES for tighten). Default 10.")
    ap.add_argument("--gate", default=None,
                    help="Restrict output to a single gate id.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format (default json).")
    ap.add_argument("--self-test", action="store_true",
                    help="Run synthetic-input regression tests against the "
                         "recommendation rules and exit. No telemetry read, "
                         "no gates.yaml read. Exit 0 on all-pass, 1 on any "
                         "failure. Wired into /verify-learning.")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    gates = _load_gates()
    if args.gate:
        gates = [g for g in gates if g.get("id") == args.gate]
        if not gates:
            print(f"[gate-retirement-eval] no gate '{args.gate}' in gates.yaml",
                  file=sys.stderr)
            return 2

    since = datetime.now() - timedelta(days=args.days)
    # Recency window for the fail_open trigger (rb-1531). A fail_open whose
    # ts is older than this is HISTORICAL (a long-resolved exception burst),
    # not a current bug — the investigate trigger windows on it so the eval
    # stops re-flagging the same May burst every run ().
    recency_cutoff = datetime.now() - timedelta(days=FAIL_OPEN_RECENCY_DAYS)
    # Recency window for the tighten trigger (). block/override
    # firings older than this are HISTORICAL for the override-rate signal — a
    # gate fixed inside the --days window keeps its pre-fix high-override
    # firings, so the cumulative rate re-recommends tighten for ~--days after
    # the fix landed. recent_counts_per_gate measures the CURRENT rate (rb-1531).
    tighten_recency_cutoff = datetime.now() - timedelta(days=TIGHTEN_RECENCY_DAYS)
    # Recency window for the widen trigger (). noop/total firings
    # older than this are HISTORICAL for the under-firing signal — a gate
    # widened inside the --days window keeps its pre-widen high-noop firings,
    # so the cumulative noop rate re-recommends widen for ~--days after the
    # patterns were added. recent_counts_widen_per_gate measures the CURRENT
    # noop rate (rb-1931), the mirror of the tighten window above.
    widen_recency_cutoff = datetime.now() - timedelta(days=WIDEN_RECENCY_DAYS)
    counts_per_gate = {}
    recent_fail_open_per_gate = {}      # fail_opens within FAIL_OPEN_RECENCY_DAYS
    latest_fail_open_ts_per_gate = {}   # ISO ts of the most recent fail_open
    recent_counts_per_gate = {}         # block/override within TIGHTEN_RECENCY_DAYS
    recent_counts_widen_per_gate = {}   # ALL decisions within WIDEN_RECENCY_DAYS
    latest_ts_per_gate = {}             # ISO ts of the most recent firing (any
                                        # decision) — inert transform input
                                        # (); ISO sorts lexically
    first_ts_per_gate = {}              # ISO ts of the EARLIEST in-window firing
                                        # — min-age transform's ledger fallback
                                        # bound (); a LOWER bound on
                                        # true age (window +  month-
                                        # hole both truncate history)
    for rec in _load_firings(since):
        gid = rec.get("gate_id")
        if not gid:
            continue
        if args.gate and gid != args.gate:
            continue
        # decision is guaranteed present and valid by _load_firings (see
        # _VALID_DECISIONS gate there). Any drift surfaces as a WARN skip,
        # not a silent "noop" miscount.
        dec = rec["decision"]
        counts_per_gate.setdefault(gid, Counter())[dec] += 1
        # ts is guaranteed parseable by _load_firings (it strptime'd the same
        # value before yielding). ISO 'YYYY-MM-DDTHH:MM:SS' sorts lexically,
        # so a string max IS the latest fail_open.
        ts_raw = rec["ts"]
        ts = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S")
        prev_latest = latest_ts_per_gate.get(gid)
        if prev_latest is None or ts_raw > prev_latest:
            latest_ts_per_gate[gid] = ts_raw
        prev_first = first_ts_per_gate.get(gid)
        if prev_first is None or ts_raw < prev_first:
            first_ts_per_gate[gid] = ts_raw
        # Tighten recency bucketing (): count block/override within
        # the tighten recency window. Only those two decisions feed the
        # override-rate suppression; noop/pass/fail_open are irrelevant to it.
        if ts >= tighten_recency_cutoff and dec in ("block", "override"):
            recent_counts_per_gate.setdefault(gid, Counter())[dec] += 1
        # Widen recency bucketing (): count ALL decisions within the
        # widen recency window. The widen noop-rate suppression needs recent
        # noop AND recent total, so unlike the tighten bucket (block/override
        # only) this captures every decision.
        if ts >= widen_recency_cutoff:
            recent_counts_widen_per_gate.setdefault(gid, Counter())[dec] += 1
        if dec == "fail_open":
            prev = latest_fail_open_ts_per_gate.get(gid)
            if prev is None or ts_raw > prev:
                latest_fail_open_ts_per_gate[gid] = ts_raw
            if ts >= recency_cutoff:
                recent_fail_open_per_gate[gid] = \
                    recent_fail_open_per_gate.get(gid, 0) + 1

    rows = []
    for gate in gates:
        gid = gate["id"]
        counts = counts_per_gate.get(gid, Counter())
        # Production path always passes an int (0 when no recent fail_opens),
        # never None — so it never hits _score_gate's legacy None branch.
        # Only the self-test exercises the None default.
        scored = _score_gate(
            gate, counts, args.min_fires,
            recent_fail_open=recent_fail_open_per_gate.get(gid, 0),
            latest_fail_open_ts=latest_fail_open_ts_per_gate.get(gid),
            recent_counts=recent_counts_per_gate.get(gid, Counter()),
            recent_counts_widen=recent_counts_widen_per_gate.get(gid, Counter()))
        scored["gate_id"] = gid
        # Min-age transform (): lazy — the registry git probe runs
        # ONLY for gates that scored an action recommendation (typically 0-3
        # per run), and BEFORE the inert transform (a too-young gate should
        # not reach inert conversion either). Age source preference: registry
        # introduction commit (git -S on gates.yaml) — the firings ledger
        # carries the  month-hole, so its first ts understates age
        # exactly when this rule needs it. Ledger first-in-window ts is the
        # fail-open fallback bound when git is unavailable.
        if scored["recommendation"] in _MIN_AGE_CONVERTIBLE:
            age_days = None
            reg_epoch = _gate_registry_first_epoch(gid)
            if reg_epoch is not None:
                age_days = (datetime.now().timestamp() - reg_epoch) / 86400.0
                scored.setdefault("evidence", {})["gate_age_source"] = \
                    "registry-introduction-commit"
            else:
                first_ts = first_ts_per_gate.get(gid)
                if first_ts:
                    age_days = (datetime.now()
                                - datetime.strptime(
                                    first_ts, "%Y-%m-%dT%H:%M:%S")
                                ).total_seconds() / 86400.0
                    scored.setdefault("evidence", {})["gate_age_source"] = \
                        "ledger-first-in-window-firing (lower bound; " \
                        "g-115-2294 month-hole caveat)"
            scored = _apply_min_age_transform(scored, age_days, args.days)
        # Inert transform (): lazy — the git probe runs ONLY for
        # gates that scored an action recommendation with zero recent-window
        # firings (typically 0-2 per run). recent_counts_widen already counts
        # ALL decisions in the recent window, so its sum is the recent total.
        if scored["recommendation"] in _INERT_CONVERTIBLE:
            recent_total = sum(recent_counts_widen_per_gate.get(gid, Counter()).values())
            if recent_total == 0:
                last_ts = latest_ts_per_gate.get(gid)
                last_epoch = None
                if last_ts:
                    last_epoch = datetime.strptime(
                        last_ts, "%Y-%m-%dT%H:%M:%S").timestamp()
                mod_epoch, mod_iso = _gate_modified_epoch(gate.get("script", ""))
                scored = _apply_inert_transform(
                    scored, recent_total, last_epoch, last_ts,
                    mod_epoch, mod_iso)
        rows.append(scored)

    # Sort: action items first, then keep / insufficient / uninstrumented.
    rec_order = {"retire": 0, "tighten": 1, "widen": 2, "investigate": 3,
                 "inert_candidate": 4, "keep": 5, "insufficient_data": 6,
                 "uninstrumented": 7}
    rows.sort(key=lambda r: (rec_order.get(r["recommendation"], 99),
                             -r["evidence"]["total_firings"]))

    if args.output == "human":
        print(_human_table(rows))
    else:
        print(json.dumps({
            "window_days": args.days,
            "min_fires": args.min_fires,
            "evaluated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "gates_evaluated": len(rows),
            "recommendations": rows,
        }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
