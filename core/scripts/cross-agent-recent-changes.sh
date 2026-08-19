#!/usr/bin/env bash
# cross-agent-recent-changes.sh
# List files git-changed since <timestamp>, optionally narrowed to files
# attributed to a named agent via that agent's experience.jsonl.
#
# Shared-git-identity setups (both agents commit under the same git author)
# make `git log --author=<agent>` a no-op. This script supplies the missing
# attribution layer via experience.jsonl entries, which ARE agent-private.
#
# Usage:
#   cross-agent-recent-changes.sh (--since <iso> | --since-goal <goal-id>) [--agent <name>] [--scope <prefix>]
#
# --since-goal resolves the timestamp from a goal record:
#   - lastAchievedAt if set (recurring goal that has fired before)
#   - created if lastAchievedAt is null (first fire — well-defined spec,
#     NOT a fallback: "the review window starts at the last successful
#     fire, or at goal creation if never fired.")
# This makes first-fire behavior deterministic. Single source of truth for
# "what is this goal's review window start" lives in this script, not in
# goal-description prose that re-interprets every 48h.
#
# Output: newline-separated, deduplicated, sorted paths on stdout.
# Empty output = "no matching changes" — NOT an error. Exit 0 on success,
# 2 on argument error. Fail-open on git/python failure (empty output, exit 0).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_paths.sh"
PROJECT_ROOT_EARLY="$(cd "$SCRIPT_DIR/../.." && pwd)"

SINCE=""
SINCE_GOAL=""
AGENT=""
SCOPE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --since-goal) SINCE_GOAL="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --agent) AGENT="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --scope) SCOPE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    -h|--help)
      sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0 ;;
    *)
      echo "cross-agent-recent-changes.sh: unknown arg: $1" >&2
      exit 2 ;;
  esac
done

# Resolve --since-goal to --since. Script owns the spec; the LLM doesn't
# improv on null. Explicit --since wins if both are given (power-user path).
if [ -z "$SINCE" ] && [ -n "$SINCE_GOAL" ]; then
  # `|| true` lets us inspect stdout/exit_code ourselves below, because
  # set -e would otherwise abort on Python exit 2 (goal-not-found).
  SINCE=$(SINCE_GOAL="$SINCE_GOAL" \
          PROJECT_ROOT_EARLY="$PROJECT_ROOT_EARLY" \
          WORLD_PATH="${WORLD_PATH:-}" \
          AGENT_NAME="${MIND_AGENT:-}" \
          python3 - <<'PYEOF' 2>/dev/null || true
import json, os, sys
goal_id = os.environ["SINCE_GOAL"]
project_root = os.environ.get("PROJECT_ROOT_EARLY", ".")
world_path = os.environ.get("WORLD_PATH", "")
agent_name = os.environ.get("AGENT_NAME", "")
paths = []
if world_path:
    paths.append(os.path.join(world_path, "aspirations.jsonl"))
if agent_name:
    # Phase 2.5.D: agent dirs under agents/ parent.
    paths.append(os.path.join(project_root, "agents", agent_name, "aspirations.jsonl"))
for p in paths:
    if not os.path.isfile(p):
        continue
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except Exception:
                    continue
                goals = asp.get("goals") if isinstance(asp, dict) else None
                if not isinstance(goals, list):
                    continue
                for g in goals:
                    if isinstance(g, dict) and g.get("id") == goal_id:
                        ts = g.get("lastAchievedAt") or g.get("created")
                        if ts:
                            print(ts)
                            sys.exit(0)
    except Exception:
        continue
sys.exit(2)
PYEOF
  )
  if [ -z "$SINCE" ]; then
    echo "cross-agent-recent-changes.sh: --since-goal '$SINCE_GOAL' not found or has no lastAchievedAt/created" >&2
    exit 2
  fi
fi

[ -n "$SINCE" ] || { echo "cross-agent-recent-changes.sh: --since <iso> or --since-goal <goal-id> required" >&2; exit 2; }

# ── Step 1: git-changed superset ─────────────────────────────────────────────
# Fail-open: if git exits non-zero (not a repo, bad timestamp), we emit
# empty and exit 0. The caller fresh-eyes-code Phase 1 already handles
# empty target lists by falling through to Phase 4 "no target files" finding.
GIT_FILES=$(git log --since="$SINCE" --name-only --pretty=format: 2>/dev/null | sed '/^$/d' | sort -u || true)

# Optional scope filter. SCOPE is a plain prefix (e.g. "core/"), not a regex.
if [ -n "$SCOPE" ]; then
  GIT_FILES=$(printf '%s\n' "$GIT_FILES" | grep "^${SCOPE}" || true)
fi

# ── Step 2: no --agent → emit the git-changed superset ───────────────────────
if [ -z "$AGENT" ]; then
  printf '%s\n' "$GIT_FILES" | sed '/^$/d'
  exit 0
fi

# ── Step 3: agent mode → intersect with experience-attributed paths ──────────
# Project root is parent of core/scripts.
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
EXP_FILE="$(agent_dir "$AGENT")/experience.jsonl"
if [ ! -f "$EXP_FILE" ]; then
  # Agent has no experience file → no attributed work. Emit empty.
  exit 0
fi

# Build the attributed set from experience.jsonl entries >= SINCE.
# Path-like tokens are extracted from content_path, summary, description,
# outcome_note, AND verbatim_anchors[*].content. The verbatim_anchors source
# was added  (bravo session 55 iter 47) after observation showed the
# narrative fields rarely contain core/ paths — structured anchor content is
# where modern experience entries put file references (e.g. "core/scripts/foo.py
# line 42" or "execute-protocol-digest.md:175"). Without it, attribution
# returns empty even when experience.jsonl IS fresh. This is best-effort;
# false positives are caught by the intersection with GIT_FILES below.
ATTRIBUTED=$(AGENT="$AGENT" SINCE="$SINCE" EXP_FILE="$EXP_FILE" python3 - <<'PYEOF' 2>/dev/null || true
import os, json, re
agent = os.environ["AGENT"]
since = os.environ["SINCE"]
path = os.environ["EXP_FILE"]
path_re = re.compile(r"[A-Za-z0-9_./\-]+\.[A-Za-z0-9]+")
out = set()
try:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = e.get("timestamp") or e.get("created") or ""
            if ts and ts < since:
                continue
            cp = e.get("content_path")
            if isinstance(cp, str) and cp.strip():
                out.add(cp.strip().replace("\\", "/"))
            for key in ("summary", "description", "outcome_note"):
                v = e.get(key) or ""
                if not isinstance(v, str):
                    continue
                for m in path_re.findall(v):
                    m = m.replace("\\", "/").strip(" ,'\"")
                    if "/" in m:
                        out.add(m)
            # Structured verbatim_anchors — list of {key, content} dicts.
            # Modern entries put concrete file refs here (see schema in
            # core/config/conventions/experience.md). Critical for attribution
            # because the narrative fields above rarely include file paths.
            va = e.get("verbatim_anchors") or []
            if isinstance(va, list):
                for anchor in va:
                    if not isinstance(anchor, dict):
                        continue
                    content = anchor.get("content", "")
                    if not isinstance(content, str):
                        continue
                    for m in path_re.findall(content):
                        m = m.replace("\\", "/").strip(" ,'\"")
                        if "/" in m:
                            out.add(m)
except FileNotFoundError:
    pass
for p in sorted(out):
    print(p)
PYEOF
)

if [ -z "$ATTRIBUTED" ]; then
  exit 0
fi

# ── Step 4: intersect git-changed with attributed paths ──────────────────────
# grep -Fx matches the whole line as a fixed string (no regex). Process
# substitution gives grep the attributed list as -f input. Fail-open.
printf '%s\n' "$GIT_FILES" | grep -Fx -f <(printf '%s\n' "$ATTRIBUTED") 2>/dev/null || true
