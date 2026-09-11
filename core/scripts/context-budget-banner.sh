#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Emit a single-line, LLM-quotable banner with authoritative context-pressure data.
# Required before any defer/skip/abbreviate decision gated on context pressure.
# Read-only — does not update the budget file; the statusLine hook does that.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh"
f="$AGENT_DIR/session/context-budget.json"
if [ ! -f "$f" ]; then
  echo "CTX: unavailable (no budget file; statusLine hook may not have fired yet)"
  exit 0
fi
python3 - "$f" <<'PY'
# Schema is the contract between this script and context-budget-status.py.
# Direct subscripts (no .get fallbacks): if the JSON is stale/legacy, KeyError
# exits non-zero and bash's pipefail surfaces it. That's the single-source-of-
# truth invariant — we never silently degrade to partial data.
#
# Banner shape is ALSO the contract with context-citation-audit.sh — its
# banner_re expects "CTX: raw N% | of-autocompact N% | zone WORD |" in this
# exact order. Change here = update audit regex.
#
# THE FOURTH FIELD IS NAMED `to-compact`, AND THE WORD IS THE
# POINT (). The number is `headroom_tokens` = tokens until AUTOCOMPACT,
# not tokens until the model window is full; on a 600000/80 box those differ by
# up to 520,000. guard-301 makes this line the sole sanctioned authority for
# every defer/skip/abbreviate decision in the fleet AND records that the felt
# sense already errs toward over-reporting depth, so a noun reading as remaining
# CAPACITY compounded that bias — agents narrated a routine compaction as
# "context is full" while half a window sat unused. The JSON key stays
# `headroom_tokens`: it is a machine key with a test fixture and registry checks
# on it, and renaming it would break readers for no gain. Only the SPOKEN label
# changed. Do not "restore" the old word.
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    d = json.load(fh)
used_pct = d["used_pct"]
pct_ac = d["pct_to_autocompact"]
zone = d["zone"]
to_compact = d["headroom_tokens"]   # tokens until AUTOCOMPACT — see the contract note above
updated = d["updated_at"]
es = d["env_seen"]
w = es["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
p = es["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]
env_tag = "env defaults" if (w is None and p is None) else f"env {w or 'unset'}/{p or 'unset'}"
print(f"CTX: raw {used_pct:.0f}% | of-autocompact {pct_ac:.0f}% | zone {zone} | to-compact {to_compact:,} tokens | {env_tag} | updated {updated}")
PY
