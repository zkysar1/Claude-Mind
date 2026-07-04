#!/usr/bin/env bash
# self-assess-and-decide.sh — fresh-eyes decision helper.
#
# Reads a JSON signals envelope from stdin and emits a JSON decision to
# stdout. Used by /fresh-eyes-review (per-agent Self review) and
# /fresh-eyes-program (joint world program review) to convert briefing
# signals into one of three actions: act_now, act_later, no_change.
#
# Created 2026-05-17 (Phase 1.1 packaging cleanup) — the two fresh-eyes
# skills had been referencing this script but it did not exist on disk,
# so every cadence firing was failing with "command not found." The
# decision rules below are an honest v0 conservative-by-default heuristic;
# refine with evidence.
#
# CLI:
#   bash self-assess-and-decide.sh --review-type <type>
#
# STDIN (JSON, all fields 0..1 unless noted):
#   portfolio_drift_score        — degree work has drifted from Self/Program
#   completion_health            — average completion ratio across active aspirations
#   self_evolution_signals_count — int — recent sq-012 / ABC-chain / pattern self-evolution signals
#   confirming_signal_fraction   — 0..1 — fraction of the counted self-evolution
#                                  signals that CONFIRM the current lane (team consensus
#                                  the agent is on-lane) vs. indicate drift. Default 0.0 =
#                                  no direction info (legacy raw-count behavior). Down-weights
#                                  evo_count so act_later fires on net-DIVERGENT signal, not
#                                  gross volume (0).
#   self_last_updated_days       — int — days since target md was last touched
#   partner_alignment_score      — 0..1 — only for fresh-eyes-program; cross-agent alignment
#   explicit_user_directive      — bool — true if user has asked about purpose/portfolio
#   signal_actionable_score      — 0..1 — how clearly signals map to a specific edit
#
# STDOUT (JSON):
#   {
#     "decision": "act_now" | "act_later" | "no_change",
#     "rationale": "<short string explaining the trigger>",
#     "recommended_action": "<short string suggesting what to do next>",
#     "review_type": "<echoed from --review-type>",
#     "version": "v0-2026-05-17"
#   }
#
# Exit codes: 0 on success (any decision). 2 on input error.
#
# Decision rules (v0):
#   act_now    — signal_actionable_score >= 0.7 AND
#                  (explicit_user_directive=true OR portfolio_drift_score >= 0.6
#                   OR self_last_updated_days >= 60)
#   act_later  — signal_actionable_score >= 0.4 OR
#                  effective_evo_count (= self_evolution_signals_count *
#                    (1 - confirming_signal_fraction)) >= 2 OR
#                  portfolio_drift_score >= 0.4 OR
#                  (fresh-eyes-program AND partner_alignment_score <= 0.4)
#   no_change  — otherwise
#
# Bias: conservative. Editing world/program.md or <agent>/self.md without
# strong signal is worse than missing a marginal edit. act_later is the
# safe escape valve — it files an Idea goal that the loop scores normally.

set -euo pipefail

REVIEW_TYPE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --review-type)
            [[ $# -ge 2 ]] || { echo "self-assess-and-decide: --review-type requires a value" >&2; exit 2; }
            REVIEW_TYPE="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "self-assess-and-decide: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

if [[ -z "$REVIEW_TYPE" ]]; then
    echo "self-assess-and-decide: --review-type required (fresh-eyes-review | fresh-eyes-program)" >&2
    exit 2
fi

# Locate Python — prefer py launcher on Windows, fall back to python3.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_paths.sh
if [[ -f "$SCRIPT_DIR/_paths.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/_paths.sh"
fi
PY="${PY_CMD:-python3}"

# Read stdin BEFORE the heredoc — otherwise the heredoc body replaces
# Python's stdin and the piped JSON is invisible to the script.
SIGNALS_JSON="$(cat)"

# Hand off to Python via env vars (rb-774-class lesson: never interpolate
# bash variables into a python heredoc; pass via env, single-quote source).
export SIGNALS_JSON REVIEW_TYPE

"$PY" - <<'PYEOF'
import json
import os
import sys

review_type = os.environ.get("REVIEW_TYPE", "")
raw = os.environ.get("SIGNALS_JSON", "")

try:
    if not raw.strip():
        raise ValueError("empty stdin — provide a JSON signals envelope")
    signals = json.loads(raw)
except Exception as exc:
    err = {
        "decision": "no_change",
        "rationale": f"input error: {exc}",
        "recommended_action": "fix the signals envelope and re-run",
        "review_type": review_type,
        "version": "v0-2026-05-17",
        "error": True,
    }
    json.dump(err, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(2)


def num(field, default=0.0):
    v = signals.get(field, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def boolean(field, default=False):
    v = signals.get(field, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "yes", "1")
    return bool(v)


actionable = num("signal_actionable_score")
drift = num("portfolio_drift_score")
health = num("completion_health")
evo_count = int(num("self_evolution_signals_count"))
# 0: confirming-vs-drift direction discriminator. A high count of
# CONFIRMING self-evolution signals (team consensus the agent is on-lane) is
# alignment evidence, NOT drift pressure — weighting evo_count by raw volume
# misreads consensus as a reason to evolve Self (fresh-eyes 2026-06-28: evo=5
# where 4/5 partner beliefs CONFIRMED zeta's lane wrongly read as act_later).
# confirming_signal_fraction (0..1, clamped; default 0.0 = no direction info ->
# legacy raw-count behavior) is the fraction of counted signals that CONFIRM the
# current lane; the effective (net-divergent) count down-weights them so the
# act_later gate fires on divergent signal, not gross volume. fraction=1.0 (all
# confirming) -> 0 pressure.
confirming_fraction = max(0.0, min(1.0, num("confirming_signal_fraction")))
effective_evo_count = evo_count * (1.0 - confirming_fraction)
stale_days = int(num("self_last_updated_days"))
user_says = boolean("explicit_user_directive")
partner_align = num("partner_alignment_score", default=1.0)

# act_now — high confidence, specific edit available
if actionable >= 0.7 and (
    user_says
    or drift >= 0.6
    or stale_days >= 60
):
    decision = "act_now"
    triggers = []
    if user_says:
        triggers.append("explicit user directive")
    if drift >= 0.6:
        triggers.append(f"portfolio drift {drift:.2f}")
    if stale_days >= 60:
        triggers.append(f"target stale {stale_days}d")
    rationale = (
        f"actionable={actionable:.2f}, "
        + "+".join(triggers)
    )
    recommended = "apply the edit inline via Edit tool; finalize via evolution-complete.sh"
# act_later — meaningful signal but not strong enough to auto-edit
elif (
    actionable >= 0.4
    or effective_evo_count >= 2
    or drift >= 0.4
    or (review_type == "fresh-eyes-program" and partner_align <= 0.4)
):
    decision = "act_later"
    triggers = []
    if actionable >= 0.4:
        triggers.append(f"actionable={actionable:.2f}")
    if effective_evo_count >= 2:
        triggers.append(
            f"evo_signals={evo_count}"
            + (f" (net-divergent {effective_evo_count:.1f} after "
               f"{confirming_fraction:.0%} confirming)"
               if confirming_fraction > 0 else "")
        )
    if drift >= 0.4:
        triggers.append(f"drift={drift:.2f}")
    if review_type == "fresh-eyes-program" and partner_align <= 0.4:
        triggers.append(f"partner_align={partner_align:.2f}")
    rationale = "weak-but-present signal: " + ", ".join(triggers)
    recommended = (
        "file an Idea goal under asp-115 with the recommended edit summary"
    )
else:
    decision = "no_change"
    rationale = (
        f"all signals below threshold "
        f"(actionable={actionable:.2f}, drift={drift:.2f}, "
        f"evo={evo_count}"
        + (f" net={effective_evo_count:.1f}@{confirming_fraction:.0%}conf"
           if confirming_fraction > 0 else "")
        + f", stale={stale_days}d)"
    )
    recommended = "silent no-op; cadence will re-fire at next interval"

out = {
    "decision": decision,
    "rationale": rationale,
    "recommended_action": recommended,
    "review_type": review_type,
    "version": "v0-2026-05-17",
}
json.dump(out, sys.stdout)
sys.stdout.write("\n")
PYEOF
