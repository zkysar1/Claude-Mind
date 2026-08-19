#!/usr/bin/env bash
# journal-append.sh — Templated journal entry writer (, rb-428 family).
#
# Single source of truth for the per-iteration journal append: writes a
# stable-format markdown entry to <agent>/journal/YYYY/MM/YYYY-MM-DD.md AND
# updates the journal-index.jsonl session record. Replaces the inline block
# in iteration-close.sh do_state_update() (lines 376-430 prior to extraction)
# so future call sites (manual reflections, /reflect, ad-hoc encoding) can
# share one writer instead of duplicating the templating + index logic.
#
# Why a wrapper (rb-428 pattern): LLM-side templating drifts under context
# pressure — seven sessions of journal entries had inconsistent section
# headers before the iteration-close.sh extraction. Bash-enforce the format
# and the drift surface narrows to one file (this one).
#
# Usage:
#   journal-append.sh --goal <goal-id> --outcome-class <routine|deep> \
#                     [--summary "<one-line>"] [--session <N>] \
#                     [--retrieval-influence "<one-line>"]
#
# Required:
#   --goal           Goal id this entry attributes to (e.g., )
#   --outcome-class  routine | deep
#
# Optional:
#   --summary        One-line summary; falls through to GOAL_ID if absent
#   --session        Session number for the journal index merge/add. If
#                    absent, reads active_context.session_id from working
#                    memory (same source iteration-close.sh used inline).
#   --retrieval-influence
#                    One-line articulation of how the retrieval snapshot
#                    influenced execution (G10 / R12 — closes the "Retrieval
#                    influence: ..." gap from Phase 4 Step 5c that never
#                    reached the journal). Suggested form:
#                      "rb-NNN guided <decision>" or
#                      "guard-NNN blocked <alternative>" or
#                      "tree:<key> shaped <choice>" or
#                      "no influence (retrieval was orthogonal)".
#                    Cited rb-/guard- IDs increment times_cited just like
#                    --summary citations. Empty / unset → no line emitted.
#
# Behavior:
#   - Appends a stable-format `## HH:MM — Goal: ...` block to today's .md
#   - Citation scan: rb-NNN / guard-NNN mentions in --summary increment
#     utilization.times_cited on the matching reasoning-bank / guardrail
#   - Journal index: tries journal-merge.sh first; on failure falls back to
#     journal-add.sh (creates a new session record)
#   - Fail-open: writes are best-effort; iteration close MUST NOT die here
#
# Single-writer rule: this script is the AUTHORITATIVE owner of the journal
# .md content + index pair. Other writers (e.g., /stop step 3.e emergency
# cleanup) are explicit exceptions documented in the SKILL.md callers.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"

# Defaults
GOAL_ID=""
OUTCOME=""
SUMMARY=""
SESSION_NUM=""
RETRIEVAL_INFLUENCE=""
WORK_CLASS=""

# Argument parsing — mirrors iteration-close.sh's flag style for consistency
while [[ $# -gt 0 ]]; do
    case "$1" in
        --goal) GOAL_ID="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --outcome-class) OUTCOME="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --summary) SUMMARY="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --session) SESSION_NUM="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --retrieval-influence) RETRIEVAL_INFLUENCE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --work-class) WORK_CLASS="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        *)
            echo "[journal-append] unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

if [[ -z "$GOAL_ID" || -z "$OUTCOME" ]]; then
    echo "[journal-append] --goal and --outcome-class are required" >&2
    exit 2
fi

#  (G3 worker rail) — DEFENSIVE, and deliberately so. A worker Body
# should never reach this writer at all: journal append is a reducer-only phase
# and the work-partition contract says workers do not run reducer phases. This
# guard exists to convert a FUTURE phase-drift bug from a silent shared-store
# corruption into a LOGGED skip. If it ever actually fires, that is a bug
# report about the phase partition, not routine operation — the stderr line is
# the point.
#
# Derived locally rather than from BODY_ROLE per guard-2445. Here the env var
# WOULD arrive (this runs from iteration-close.sh inside a Bash-tool chain that
# bash-agent-inject rewrote), but the body-WM predicate is the same one
# bash-agent-inject itself uses, so the two cannot drift, and this wrapper is
# reachable from call sites that are not that chain (the header names manual
# reflections and ad-hoc encoding as intended future callers).
#
# MIND_SID is THIS process's own session — NOT session/running-session-id,
# which is the REDUCER's SID and would test the wrong session dir on a worker
# box (the trap found at the health-ledger writer, same goal).
#
# Fail-open in every direction: unset vars, an agent_dir failure, or a missing
# file all fall through to the normal append. This writer fires on every
# iteration close fleet-wide, so the guard must never be able to block it.
if [ -n "${MIND_SID:-}" ] && [ -n "${MIND_AGENT:-}" ]; then
    _jr_agent_dir="$(agent_dir "$MIND_AGENT" 2>/dev/null || echo "")"
    if [ -n "$_jr_agent_dir" ] && \
       [ -f "$_jr_agent_dir/sessions/$MIND_SID/working-memory.yaml" ]; then
        echo "[journal-append] BODY=worker — SKIPPED agent-wide journal append for $GOAL_ID. A worker should not reach this reducer-only writer; investigate the phase partition (g-306-125)." >&2
        exit 0
    fi
fi

# Resolve target file path
year="$(date +%Y)"
month="$(date +%m)"
day="$(date +%Y-%m-%d)"
journal_dir="$AGENT_DIR/journal/$year/$month"
journal_file="$journal_dir/$day.md"
mkdir -p "$journal_dir"
timestamp="$(date +%H:%M)"

# Value framing (FW-5 / R2, ): a DERIVED, presentation-only affirming
# label for the (outcome_class, work_class) pair, so a recorded `routine`
# outcome carries its structural value instead of reading as "did not count"
# (.claude/rules/learning-philosophy.md Recognition half). Never stored, no
# enum touched — derived here at write time. work_class is optional (passed by
# iteration-close.sh; other callers omit it -> the outcome_class's
# `unclassified` framing). Fail-open: any helper error -> empty -> no Value line.
VALUE_FRAMING="$(python3 "$CORE_ROOT/scripts/_value_framing.py" "$OUTCOME" "$WORK_CLASS" 2>/dev/null || true)"

# Stable-format markdown append. Section headers MUST stay in this order
# (## title, Outcome:, Summary:) — /verify-learning Section BE checks the
# shape, and downstream readers (boot, consolidation) parse it. The Value:
# line is APPENDED after the existing optional lines (same position class as
# Retrieval influence) so the Section BE order check is unaffected.
{
    echo ""
    echo "## $timestamp — Goal: ${SUMMARY:-$GOAL_ID} ($GOAL_ID)"
    echo "Outcome: $OUTCOME"
    [[ -n "$SUMMARY" ]] && echo "Summary: $SUMMARY"
    [[ -n "$RETRIEVAL_INFLUENCE" ]] && echo "Retrieval influence: $RETRIEVAL_INFLUENCE"
    [[ -n "$VALUE_FRAMING" ]] && echo "Value: $VALUE_FRAMING"
} >> "$journal_file"

# Citation scan (/): grep SUMMARY for rb-NNN / guard-NNN
# and increment utilization.times_cited on each unique hit. Tracks passive
# citation during encoding distinct from explicit times_helpful attestation.
# rb-family 'pipefail + set -e kills citation-less goals' ():
# grep exits 1 on no-match; under set -o pipefail the whole substitution
# exits 1; under set -e that TERMINATES this script before the index update.
# The `|| true` suffix tolerates empty grep output without masking real
# failures elsewhere in the pipe.
if [[ -n "$SUMMARY" || -n "$RETRIEVAL_INFLUENCE" ]]; then
    # Scan BOTH summary and retrieval-influence lines — citations may appear
    # only in the influence articulation (R12 / G10 closure).
    # [0-9]{3,}: IDs crossed into 4 digits (rb-3742, guard-1151) — the old
    # {3} exact-width silently dropped every modern cite (, same
    # defect class as the board.py / board_write.py _CITE_RE fix).
    citations="$(printf '%s\n%s' "$SUMMARY" "$RETRIEVAL_INFLUENCE" | grep -oE '\b(rb|guard)-[0-9]{3,}\b' | sort -u || true)"
    for cite in $citations; do
        if [[ "$cite" == rb-* ]]; then
            bash "$CORE_ROOT/scripts/reasoning-bank-increment.sh" "$cite" utilization.times_cited >/dev/null 2>&1 || true
        elif [[ "$cite" == guard-* ]]; then
            bash "$CORE_ROOT/scripts/guardrails-increment.sh" "$cite" utilization.times_cited >/dev/null 2>&1 || true
        fi
    done
fi

# Tree-node citation scan: extract kebab-case tokens from SUMMARY, validate
# against existing _tree.yaml node keys, and increment times_inferred_helpful
# on each match. This closes the helpful-feedback gap that left 0/655 leaves
# carrying times_helpful>0 across the live tree (audit 2026-05-07): the
# helpful signal authoritative attestation path was never invoked, so
# utility_ratio stayed pinned at 0 and the utility-weighted retrieval ranker
# permanently downweighted every retrieved-but-uncited node to floor 0.5x.
#
# This scan is the citation analog for tree nodes — same pattern as the
# rb/guard scan above, but the bump target is times_inferred_helpful (the
# half-weight counter, per `(times_helpful + 0.5*times_inferred_helpful) /
# max(retrieval_count, 1)` in tree.py:_recompute_utility_ratio). Citation
# is a weaker signal than explicit times_helpful attestation, so we mirror
# `--infer`'s half-weight semantics — a node mentioned in the journal
# summary likely informed the iteration but was not explicitly attested.
#
# Token shape: `\b[a-z][a-z0-9]*(-[a-z0-9]+)+\b` matches kebab keys with at
# least one hyphen. Single-word tokens are excluded — too many English-prose
# false positives. Validation against _tree.yaml keys eliminates remaining
# false positives (e.g., "fail-open" or "post-execution" shaped phrases).
# Fail-open: a missing tree path, parse error, or missing PyYAML all
# silently skip the scan — this is auxiliary instrumentation, not a gate.
if [[ -n "$SUMMARY" ]]; then
    tree_path="$WORLD_DIR/knowledge/tree/_tree.yaml"
    if [[ -f "$tree_path" ]]; then
        # CRITICAL — DO NOT REMOVE the `tr -d '\r'` filter at the end of the
        # subshell. Python on Windows writes its `print()` newline as CRLF
        # to text-mode stdout. After bash $(...) command substitution and
        # word-splitting on default IFS, exactly one of the multi-line
        # tokens ends up carrying a literal '\r' that tree-update.sh then
        # passes as the node-key argument — it never matches and the cite
        # is silently dropped. tr strips every '\r' before capture so all
        # tokens are clean. Verified by core/scripts/tests/test_journal_tree_cite_scan.py.
        tree_cites="$(printf '%s' "$SUMMARY" | python3 -c '
import os, re, sys
summary = sys.stdin.buffer.read().decode("utf-8", errors="replace")
tree_path = sys.argv[1]
candidates = set(re.findall(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b", summary))
if not candidates:
    sys.exit(0)
try:
    import yaml
    with open(tree_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
except Exception:
    # Missing PyYAML, parse error, or unreadable file — skip silently.
    # The downstream tree-update --increment loop is the actual write path;
    # this scan is best-effort detection.
    sys.exit(0)
keys = set((data.get("nodes") or {}).keys())
for k in sorted(candidates & keys):
    print(k)
' "$tree_path" 2>/dev/null | tr -d '\r' || true)"
        # tree-update --increment auto-recomputes utility_ratio when the
        # field is in _UTILITY_RATIO_FIELDS (tree.py:104). times_inferred_helpful
        # is in that set, so each match here updates utility_ratio under the
        # write_tree() lock — no race with concurrent retrieve.py bumps.
        for tk in $tree_cites; do
            bash "$CORE_ROOT/scripts/tree-update.sh" --increment "$tk" times_inferred_helpful >/dev/null 2>&1 || true
        done
    fi
fi

# Resolve session_num if not supplied. session_id is seeded into
# active_context by the orchestrator (aspirations/SKILL.md Phase -1
# initialization). Reading the wrong slot silently fell back to session-1
# every iteration, which corrupted the journal index — preserved comment
# from the iteration-close.sh extraction so the lesson stays attached.
if [[ -z "$SESSION_NUM" ]]; then
    SESSION_NUM="$(bash "$CORE_ROOT/scripts/wm-read.sh" active_context | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    sid = data.get("session_id", "") if isinstance(data, dict) else ""
    print(sid.replace("session-", "") if sid else "1")
except Exception:
    print("1")
' || { echo "[journal-append] WARN: active_context read/parse failed — journal index defaulting to session 1" >&2; echo "1"; })"
fi

# Journal index update (merge into today's session or create new entry).
# Try merge first; if no entry exists, add a new one. Stderr surfaces on
# failure but the iteration MUST continue — fail-open is mandatory.
if ! echo "{\"goals_completed\":[\"$GOAL_ID\"]}" | bash "$CORE_ROOT/scripts/journal-merge.sh" "$SESSION_NUM" >/dev/null 2>&1; then
    echo "{\"session\":$SESSION_NUM,\"date\":\"$day\",\"journal_file\":\"$AGENT_NAME/journal/$year/$month/$day.md\"}" | \
        bash "$CORE_ROOT/scripts/journal-add.sh" >/dev/null 2>&1 || true
fi

echo "[journal-append] ok: goal=$GOAL_ID outcome=$OUTCOME session=$SESSION_NUM file=$AGENT_NAME/journal/$year/$month/$day.md"
