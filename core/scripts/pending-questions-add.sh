#!/usr/bin/env bash
# pending-questions-add.sh — append a new pending question to the bound
# agent's session/pending-questions.yaml.
#
# Created 2026-05-17 (Phase 1.1 packaging cleanup). Multiple consumers
# (aspirations-evolve/SKILL.md, l1-taxonomy-changes.md convention) call
# this script but it didn't exist on disk — so every l1-taxonomy proposal
# and structural-modification surface was silently failing at runtime.
#
# CLI:
#   bash pending-questions-add.sh \
#       --id <question-id> \
#       --question <text> \
#       --default-action <text> \
#       [--priority HIGH|MEDIUM|LOW] \
#       [--type <category-tag>] \
#       [--context <text>]
#
# Behavior:
#   - Resolves the bound agent via MIND_AGENT env var (injected by the
#     PreToolUse[Bash] hook). Refuses to run with no binding.
#   - Reads <agent>/session/pending-questions.yaml. Three on-disk shapes
#     are tolerated, kept in lock-step with the reader
#     pending-questions-sweep.py::_load_questions (rb-1786 — a paired
#     reader/writer over one store MUST tolerate the same shapes):
#       (A) top-level dict with `questions:` list             (canonical / newer)
#       (B) list carrying a dict with a `questions:` key       (legacy wrapper)
#       (C) bare top-level list of entry dicts                 (shape C)
#       (mixed B+C) a list carrying BOTH a wrapper element and bare entry
#                   dicts (alpha's real on-disk shape, 2026-06-14 audit)
#     New entries are appended preserving the existing shape; the script does
#     not normalize between shapes (out of scope; would surprise readers
#     mid-session).
#   - Refuses duplicate IDs.
#   - Sets `status: pending` and `created: <local ISO timestamp>`.
#
# Known limitation: yaml.safe_dump() does not preserve YAML comments
# embedded in the source file (PyYAML lacks comment-preservation; that
# requires ruamel.yaml). Currently safe because pending-questions.yaml
# in this repo is data-only — no semantic comments observed. Re-evaluate
# if agents start writing # comments into pending-questions entries.
#
# Exit codes:
#   0  — appended successfully
#   2  — input error / no agent binding / duplicate ID
#   3  — write error
#
# Used by:
#   - .claude/skills/aspirations-evolve/SKILL.md (l1-taxonomy proposals)
#   - core/config/conventions/l1-taxonomy-changes.md (canonical add API)

set -euo pipefail

ID=""
QUESTION=""
DEFAULT_ACTION=""
PRIORITY=""
TYPE=""
CONTEXT=""

_require_value() {
    [[ $# -ge 2 ]] || { echo "pending-questions-add: $1 requires a value" >&2; exit 2; }
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)              _require_value "$@"; ID="$2"; shift 2 ;;
        --question)        _require_value "$@"; QUESTION="$2"; shift 2 ;;
        --default-action)  _require_value "$@"; DEFAULT_ACTION="$2"; shift 2 ;;
        --priority)        _require_value "$@"; PRIORITY="$2"; shift 2 ;;
        --type)            _require_value "$@"; TYPE="$2"; shift 2 ;;
        --context)         _require_value "$@"; CONTEXT="$2"; shift 2 ;;
        --help|-h)         sed -n '2,30p' "$0"; exit 0 ;;
        *)                 echo "pending-questions-add: unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$ID" || -z "$QUESTION" || -z "$DEFAULT_ACTION" ]]; then
    echo "pending-questions-add: --id, --question, --default-action are required" >&2
    exit 2
fi

AGENT="${MIND_AGENT:-}"
if [[ -z "$AGENT" ]]; then
    echo "pending-questions-add: MIND_AGENT not set — no agent binding" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/_paths.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/_paths.sh"
fi
PY="${PY_CMD:-python3}"

# Resolve agent dir relative to PROJECT_ROOT (set by _paths.sh; fall back to cwd)
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
PQ_FILE="$(agent_dir "$AGENT")/session/pending-questions.yaml"

if [[ ! -f "$PQ_FILE" ]]; then
    echo "pending-questions-add: $PQ_FILE not found — agent dir not initialized?" >&2
    exit 2
fi

# Hand off to Python for YAML manipulation. Pass values via env so we don't
# have to quote-escape user text through bash. (rb-774-class lesson:
# never interpolate bash variables into a python heredoc.)
export PQ_FILE ID QUESTION DEFAULT_ACTION PRIORITY TYPE CONTEXT

"$PY" - <<'PYEOF'
import os
import sys
from datetime import datetime

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "pending-questions-add: PyYAML required (pip install pyyaml)\n"
    )
    sys.exit(3)

pq_file = os.environ["PQ_FILE"]
new_id = os.environ["ID"]
question = os.environ["QUESTION"]
default_action = os.environ["DEFAULT_ACTION"]
priority = os.environ.get("PRIORITY", "")
qtype = os.environ.get("TYPE", "")
context = os.environ.get("CONTEXT", "")

with open(pq_file, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

# Detect on-disk shape. Three variants tolerated, kept in lock-step with the
# reader pending-questions-sweep.py::_load_questions (rb-1786: a paired
# reader/writer over one store MUST tolerate the same shapes):
#   shape A:   {"questions": [ {...}, ... ]}            — canonical / delta
#   shape B:   [ {"questions": [ {...}, ... ]}, ... ]   — list with wrapper element
#   shape C:   [ {"id": ...}, {"id": ...}, ... ]        — bare list of entry dicts
#   mixed B+C: a list carrying BOTH a wrapper element and bare entry dicts
#              (alpha's real on-disk shape, 2026-06-14 audit)
# resolve() returns (append_target, all_entries, ok):
#   append_target — the list to .append() the new entry into (a wrapper's
#                   "questions" list for A/B, or the top-level list itself for C)
#   all_entries   — every existing entry dict, flattened across the whole
#                   container, so the duplicate-id check sees bare entries too
#   ok            — False when the container shape is unrecognized
def resolve(d):
    if isinstance(d, dict) and isinstance(d.get("questions"), list):
        ql = d["questions"]
        return ql, [e for e in ql if isinstance(e, dict)], True
    if isinstance(d, list):
        all_entries, wrapper = [], None
        for item in d:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("questions"), list):
                if wrapper is None:
                    wrapper = item
                all_entries.extend(
                    e for e in item["questions"] if isinstance(e, dict)
                )
            elif "id" in item or "question" in item:
                all_entries.append(item)
        if wrapper is not None:
            return wrapper["questions"], all_entries, True
        # pure bare list (shape C): the top-level list IS the entry list
        return d, all_entries, True
    return None, None, False

if data is None:
    # Empty file — start as shape C (bare list). Every reader tolerates it, AND
    # the naive user-facing top-level-status filters (agent-completion-report
    # Phase 2 step 8, open-questions, respond) see ONLY bare top-level entries;
    # a dict-wrapper (former shape A) would be invisible to them from day one
    # ( / : alpha's wrapped file hid 19/21 questions from
    # every user-facing surface). Both writer classes preserve the bare-list
    # shape on append (rb-1786).
    data = []
    append_target, existing = data, []
else:
    append_target, existing, ok = resolve(data)
    if not ok:
        sys.stderr.write(
            f"pending-questions-add: {pq_file} schema not recognized\n"
        )
        sys.exit(2)

# Refuse duplicate IDs (across ALL entries, flattened — matches the reader).
for q in existing:
    if isinstance(q, dict) and q.get("id") == new_id:
        sys.stderr.write(
            f"pending-questions-add: duplicate id '{new_id}' "
            f"already exists in {pq_file}\n"
        )
        sys.exit(2)

# Build the entry. Use local-system time per CLAUDE.md convention.
entry = {
    "id": new_id,
    "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    "question": question,
    "default_action": default_action,
    "status": "pending",
}
if priority:
    entry["priority"] = priority
if qtype:
    entry["type"] = qtype
if context:
    entry["context"] = context

append_target.append(entry)

# In-place mutation above preserves the original container shape on write:
# A/B append into the wrapper's "questions" list, C appends as a new
# top-level list element.
out = data

try:
    with open(pq_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            out,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )
except Exception as exc:
    sys.stderr.write(f"pending-questions-add: write failed: {exc}\n")
    sys.exit(3)

print(f"pending-questions-add: appended {new_id} to {pq_file}")
PYEOF
