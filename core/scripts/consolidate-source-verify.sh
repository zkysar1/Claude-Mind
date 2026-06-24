#!/usr/bin/env bash
# consolidate-source-verify.sh — Gap-16 guard (never-summarize-summaries).
#
# Fails loud (exit 1) when <source-path> is a prior SUMMARY artifact, so
# aspirations-consolidate never regenerates a summary level FROM a summary.
# Invariant (GSD 2.0 / BRD Gap 16): each summary level MUST regenerate from the
# RAW level below — journal + actual state — never from a previously-written
# summary file. Summarizing a summary compounds lossy re-compression across
# session boundaries (the telephone-game / summary-of-summary degradation).
#
# Usage: consolidate-source-verify.sh <source-path>
#   exit 0 — raw source (journal / experience / session state), OK to summarize from
#   exit 1 — summary artifact (loud stderr), MUST NOT summarize from
#   exit 2 — usage error
#
# Pure path/name classifier — no daemon, no state, no side effects. Both
# aspirations-consolidate (Step 0.6 source-integrity verifier) and
# verify-learning (Gap-16 smoke test) call it; the two real call sites justify
# the standalone script per .claude/rules/implementation-discipline.md.
#
# Origin:  (BRD Gap 16, Tier 4, S, very-low risk). See
# world/docs/research-to-framework-gap-brd.md Gap 16.

set -uo pipefail

if [ $# -lt 1 ]; then
    echo "usage: consolidate-source-verify.sh <source-path>" >&2
    exit 2
fi

SRC="$1"
base="$(basename -- "$SRC")"
low_base="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')"
low_full="$(printf '%s' "$SRC" | tr '[:upper:]' '[:lower:]')"

is_summary=0

# Summary-artifact name patterns — a prior summary LEVEL, never a valid source.
case "$low_base" in
    handoff.yaml|handoff.yml)                       is_summary=1 ;;
    session-summary*)                               is_summary=1 ;;
    *-summary.yaml|*-summary.yml|*-summary.md|*-summary.json) is_summary=1 ;;
    *_summary.*)                                    is_summary=1 ;;
    consolidation-*.yaml|consolidation-*.md)        is_summary=1 ;;
esac

# A "/summaries/" path segment marks a summary artifact regardless of basename.
case "$low_full" in
    */summaries/*)                                  is_summary=1 ;;
esac

if [ "$is_summary" -eq 1 ]; then
    echo "[consolidate-source-verify] FAIL-LOUD: '$SRC' is a prior SUMMARY artifact." >&2
    echo "[consolidate-source-verify] Gap-16 invariant: each summary level MUST regenerate from the RAW level below (journal + state), never from a prior summary. Source from agents/<agent>/journal/, experience.jsonl, or session state instead." >&2
    exit 1
fi

exit 0
