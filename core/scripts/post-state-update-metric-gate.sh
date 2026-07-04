#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# post-state-update-metric-gate.sh
# Content-gate sibling to post-state-update-gate.sh (rb-917, ).
# Fires when a deep-outcome closure carries 2+ distinct numeric findings
# in outcome_note prose but no tree edit has happened since the goal was
# selected. Closes the Step 8a/c LLM-residue gap surfaced by 
# (alpha closed  with measurable production metrics in
# outcome_note prose, but verification:null and no tree edit -- the
# counter-gate family could not detect this "encoded the wrong content"
# failure mode).
#
# Usage:
#   bash post-state-update-metric-gate.sh <outcome_class> <goal_id> <category> <slug>
#   outcome_note (multi-line prose) passed via stdin.
#
# Output: single-line JSON on stdout. Always exits 0 (fail-open).
#   FIRED: {"fired":true,"distinct_count":N,"reason":"...","candidates":[...],
#           "candidate_node_key":"...","candidate_node_file":"..."}
#   NOT-FIRED: {"fired":false,"distinct_count":N,"reason":"..."}
#   ERROR (fail-open): {"fired":false,"reason":"gate-error:<detail>"}
#
# The caller (iteration-close.sh state-update Step 8.79) reads the JSON.
# If .fired == true, the full JSON is written to WM as the signal payload
# for force_metric_encoding_pending. aspirations-precheck Phase 0-pre4
# consumes the sentinel and prompts the LLM to encode the candidates.
#
# Cross-refs:
#   - sibling: core/scripts/post-state-update-gate.sh (counter-gate)
#   - decision rule: rb-917 (Step 8a/c precision extraction is LLM-residue)
#   - canonical incident:  ( closed with metrics in prose)
#   - recovered contract:  (bravo bytecode-strings extraction)
#   - this Apply: 

# CRITICAL -- DO NOT add `set -e` here. Per guard-141, framework gates
# MUST fail open on every error path. State-update is not a hook, but the
# gate is invoked from inside iteration-close.sh do_state_update and
# blocking on a gate error would crash the obligation chain.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/_paths.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/_platform.sh"

OUTCOME_CLASS="${1:-}"
GOAL_ID="${2:-}"
CATEGORY="${3:-}"
SLUG="${4:--}"

# Threshold knob -- change here, single source of truth per
# .claude/rules/communication-clarity.md rule 5.
DISTINCT_COUNT_THRESHOLD=2

emit_json() {
  # Use python for clean JSON encoding -- avoids bash quoting hell around
  # multi-line context strings and special chars in extracted findings.
  FIRED="${FIRED:-false}" \
  DISTINCT_COUNT="${DISTINCT_COUNT:-0}" \
  REASON="${REASON:-}" \
  CANDIDATES="${CANDIDATES:-}" \
  CANDIDATE_NODE_KEY="${CANDIDATE_NODE_KEY:-}" \
  CANDIDATE_NODE_FILE="${CANDIDATE_NODE_FILE:-}" \
  py -3 - <<'PYEOF'
import json, os
fired = os.environ.get("FIRED") == "true"
dc = int(os.environ.get("DISTINCT_COUNT", "0") or "0")
reason = os.environ.get("REASON") or ""
out = {"fired": fired, "distinct_count": dc, "reason": reason}
if fired:
    cands_raw = os.environ.get("CANDIDATES") or ""
    out["candidates"] = [l.strip() for l in cands_raw.split("\x1f") if l.strip()][:20]
    out["candidate_node_key"] = os.environ.get("CANDIDATE_NODE_KEY") or ""
    out["candidate_node_file"] = os.environ.get("CANDIDATE_NODE_FILE") or ""
print(json.dumps(out))
PYEOF
  exit 0
}

# Test case 2: outcome != "deep" -> no-op
if [ "$OUTCOME_CLASS" != "deep" ]; then
  FIRED=false REASON="outcome_class=${OUTCOME_CLASS:-<unset>} (gate fires only on deep)" emit_json
fi

#  option (d): meta-work category -> no-op.
# LOC-counts and other numeric findings in framework-* / *hygiene* goals
# describe implementation size of the work itself, not production metrics
# worth encoding into the tree. Canonical incident ( self-test):
# the gate fired with 3 distinct findings (250loc, 44loc, 47loc) on a
# bravo framework-hygiene deep close -- none were production metrics.
# The gate is intended for production-domain categories (npc-*, ayoai-*,
# roblox-*, *perf*, research-*) where numeric findings genuinely reflect
# verified runtime/production behavior. Cheapest + most targeted fix per
# the goal description (over options a/b/c which would lose legitimate
# production-LOC cases or raise the threshold blindly).
case "$CATEGORY" in
  framework-*|*hygiene*)
    FIRED=false REASON="meta-work category=$CATEGORY (LOC-counts and numeric findings on framework/hygiene work are not production metrics worth encoding; gate intended for production-domain findings -- g-115-726 option d)" emit_json
    ;;
esac

# Read outcome_note from stdin
OUTCOME_NOTE=$(cat 2>/dev/null || true)

# Test case 3 (partial): empty outcome_note -> no-op
if [ -z "$OUTCOME_NOTE" ]; then
  FIRED=false REASON="empty outcome_note (gate has no content to scan)" emit_json
fi

# Extract distinct numeric findings via regex. Uses ASCII unit separator
# (0x1f) between candidates to avoid newline-in-context confusion when
# bash re-emits via env var to emit_json's heredoc.
# NOTE: bash heredoc closer ends the command before `||` can attach, so
# the fallback assignment lives on the next line.
# NOTE: outcome_note is passed via env var (OUTCOME_NOTE), NOT stdin --
# `<<PYEOF` redirects stdin to the heredoc content, so a pipe in would be
# silently swallowed. Env vars are the only safe channel for multi-line
# bash-to-Python-heredoc data.
EXTRACT_JSON=$(OUTCOME_NOTE="$OUTCOME_NOTE" py -3 - <<'PYEOF' 2>/dev/null
import json, os, re
text = os.environ.get("OUTCOME_NOTE", "").encode("utf-8", errors="replace").decode("utf-8")

findings = []
seen_values = set()


def _add(val, ctx_start, ctx_end):
    key = val.lower().replace(" ", "")
    if key in seen_values:
        return
    seen_values.add(key)
    cs = max(0, ctx_start - 40)
    ce = min(len(text), ctx_end + 40)
    ctx = text[cs:ce].strip().replace("\n", " ").replace("\r", " ")
    # Collapse runs of whitespace
    ctx = re.sub(r"\s+", " ", ctx)
    # Truncate context so emit_json env doesn't explode
    if len(ctx) > 160:
        ctx = ctx[:157] + "..."
    findings.append(f"{val} :: {ctx}")


# Pattern A: multiplier like "1.8x", "2x", "0.5X". Trailing boundary
# enforced by lookahead to avoid eating into the next token.
for m in re.finditer(r"\b(\d+(?:\.\d+)?[xX])\b", text):
    _add(m.group(1), m.start(), m.end())

# Pattern B: baseline comparison "X vs Y" (both numeric)
for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s+vs\s+(\d+(?:\.\d+)?)\b", text):
    val = f"{m.group(1)} vs {m.group(2)}"
    _add(val, m.start(), m.end())

# Pattern C: delta "X -> Y" or "X --> Y" (ASCII arrow). Permits surrounding
# space but normalizes to a single arrow.
for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s*-+>\s*(\d+(?:\.\d+)?)\b", text):
    val = f"{m.group(1)} -> {m.group(2)}"
    _add(val, m.start(), m.end())

# Pattern D: percentage "N%" or "N.N%"
for m in re.finditer(r"\b(\d+(?:\.\d+)?%)", text):
    _add(m.group(1), m.start(), m.end())

# Pattern E: "N baseline" or "N base" -- token after the number identifies
# the kind of measurement.
for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s+(baseline|base)\b", text, flags=re.IGNORECASE):
    val = f"{m.group(1)} {m.group(2).lower()}"
    _add(val, m.start(), m.end())

# Emit using ASCII unit separator -- safe to round-trip through env vars.
import sys as _sys
sep = "\x1f"
out = {"count": len(findings), "candidates_sep": sep.join(findings)}
_sys.stdout.write(json.dumps(out))
PYEOF
)
# Fallback in case the heredoc errored out -- separated from heredoc close
# because bash cannot attach `||` to a heredoc-closing token.
if [ -z "${EXTRACT_JSON:-}" ]; then
  EXTRACT_JSON='{"count":0,"candidates_sep":""}'
fi

DISTINCT_COUNT=$(echo "$EXTRACT_JSON" | py -3 -c "import json,sys; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo 0)
CANDIDATES=$(echo "$EXTRACT_JSON" | py -3 -c "import json,sys; print(json.load(sys.stdin).get('candidates_sep', ''))" 2>/dev/null || echo "")

# Test case 3 (full): below threshold -> no-op
if [ "$DISTINCT_COUNT" -lt "$DISTINCT_COUNT_THRESHOLD" ]; then
  FIRED=false DISTINCT_COUNT="$DISTINCT_COUNT" REASON="below threshold ($DISTINCT_COUNT distinct finding(s), need >=$DISTINCT_COUNT_THRESHOLD)" emit_json
fi

# Test case 5: tree edited since selected_at -> no-op (LLM already encoded)
CHECKPOINT_FILE="$AGENT_DIR/session/iteration-checkpoint.json"
if [ -f "$CHECKPOINT_FILE" ]; then
  SELECTED_AT=$(py -3 -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(d.get('selected_at',''))" "$CHECKPOINT_FILE" 2>/dev/null || true)
  if [ -n "$SELECTED_AT" ]; then
    if py -3 "$SCRIPT_DIR/tree-edit-since.py" "$SELECTED_AT" >/dev/null 2>&1; then
      FIRED=false DISTINCT_COUNT="$DISTINCT_COUNT" REASON="tree already edited since selected_at=$SELECTED_AT (LLM already encoded; no encoding-drift to signal)" emit_json
    fi
  fi
fi

# Test case 6: sentinel idempotency -- compare candidate fingerprint vs
# previously stored signal. If unchanged, dedup to no-op so re-running
# state-update on the same content doesn't multiply the sentinel.
# Fingerprint uses ONLY the values (left of " :: "), not the context, so
# unrelated whitespace drift in the prose doesn't fool the dedup.
PREVIOUS_SIGNAL=$(bash "$SCRIPT_DIR/wm-read.sh" force_metric_encoding_pending --json 2>/dev/null || echo "null")
if [ "$PREVIOUS_SIGNAL" != "null" ] && [ -n "$PREVIOUS_SIGNAL" ]; then
  DEDUP_CHECK=$(CANDIDATES_RAW="$CANDIDATES" PREVIOUS_SIGNAL="$PREVIOUS_SIGNAL" py -3 - <<'PYEOF' 2>/dev/null
import json, os, hashlib
cands_raw = os.environ.get("CANDIDATES_RAW", "")
cur_vals = [c.split(" :: ", 1)[0].strip() for c in cands_raw.split("\x1f") if c.strip()]
cur_fp = hashlib.sha256("|".join(sorted(cur_vals)).encode()).hexdigest()[:16]
try:
    prev = json.loads(os.environ.get("PREVIOUS_SIGNAL", "null") or "null")
except Exception:
    prev = None
if not isinstance(prev, dict):
    print("different")
else:
    prev_cands = prev.get("candidates") or []
    prev_vals = [c.split(" :: ", 1)[0].strip() for c in prev_cands]
    prev_fp = hashlib.sha256("|".join(sorted(prev_vals)).encode()).hexdigest()[:16]
    if cur_fp == prev_fp and cur_fp:
        print(f"same:{cur_fp}")
    else:
        print("different")
PYEOF
)
  # Fallback when the dedup heredoc errored.
  if [ -z "${DEDUP_CHECK:-}" ]; then
    DEDUP_CHECK="different"
  fi
  if [[ "$DEDUP_CHECK" == same:* ]]; then
    FIRED=false DISTINCT_COUNT="$DISTINCT_COUNT" REASON="sentinel dedup (fingerprint=${DEDUP_CHECK#same:} unchanged from previous fire)" emit_json
  fi
fi

# Compute candidate node key/file via category heuristic. Best-effort --
# the LLM consumer (Phase 0-pre4) treats this as advisory and may pick a
# different target. Mapping is intentionally coarse: top-level world tree
# directories are execution / intelligence / performance / system.
CANDIDATE_NODE_KEY=""
CANDIDATE_NODE_FILE=""
case "$CATEGORY" in
  framework-architecture|*framework*|*infra*|*ops*)
    CANDIDATE_NODE_KEY="system/${CATEGORY}"
    ;;
  npc-cognition|npc-intelligence|*npc*|*intel*|*reasoning*)
    CANDIDATE_NODE_KEY="intelligence/${CATEGORY}"
    ;;
  *perf*|*latency*|*memory*)
    CANDIDATE_NODE_KEY="performance/${CATEGORY}"
    ;;
  *execution*|*pipeline*|*deploy*)
    CANDIDATE_NODE_KEY="execution/${CATEGORY}"
    ;;
  *)
    CANDIDATE_NODE_KEY="${CATEGORY}"
    ;;
esac
if [ -n "${WORLD_DIR:-}" ] && [ -n "$CANDIDATE_NODE_KEY" ]; then
  CANDIDATE_NODE_FILE="${WORLD_DIR}/knowledge/tree/${CANDIDATE_NODE_KEY}.md"
  # 6: the category heuristic can name a node that does NOT exist
  # (e.g. category "agent-health" -> agent-health.md, never created). A phantom
  # candidate keeps the Phase 0-pre4 consumer stuck -- it cannot Edit a
  # non-existent node, so encoding is skipped and the sentinel never clears
  # (observed stuck 3 iterations). Verify existence: fall back to the L1 parent
  # when the specific node is absent but its top-level ancestor exists, else
  # blank the suggestion so the (advisory) consumer explicitly picks a real node.
  if [ ! -f "$CANDIDATE_NODE_FILE" ]; then
    PARENT_KEY="${CANDIDATE_NODE_KEY%/*}"
    if [ "$PARENT_KEY" != "$CANDIDATE_NODE_KEY" ] && [ -f "${WORLD_DIR}/knowledge/tree/${PARENT_KEY}.md" ]; then
      CANDIDATE_NODE_KEY="$PARENT_KEY"
      CANDIDATE_NODE_FILE="${WORLD_DIR}/knowledge/tree/${PARENT_KEY}.md"
    else
      CANDIDATE_NODE_KEY=""
      CANDIDATE_NODE_FILE=""
    fi
  fi
fi

# Fire -- sentinel will be written by caller (iteration-close.sh).
FIRED=true \
DISTINCT_COUNT="$DISTINCT_COUNT" \
REASON="$DISTINCT_COUNT distinct numeric findings in outcome_note, no tree edit since selected_at" \
CANDIDATES="$CANDIDATES" \
CANDIDATE_NODE_KEY="$CANDIDATE_NODE_KEY" \
CANDIDATE_NODE_FILE="$CANDIDATE_NODE_FILE" \
emit_json
