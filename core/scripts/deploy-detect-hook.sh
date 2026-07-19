#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here.
# deploy-detect-hook.sh — PostToolUse[Bash] hook: register a deploy-verification
# obligation when an agent pushes to a CI repo (part of the pending-deploys hard
# gate, 8 / SG-a).
#
# On EVERY Bash tool call this hook runs. The HOT PATH is a single raw-string
# pre-filter: if the stdin payload does not even contain "push", exit 0 before
# any python3 or git work (the vast majority of calls). Only on a candidate does
# it parse the command, do a PRECISE `git push` match, capture {repo, sha,
# goal_id, dir, ts}, and append via pending-deploys.py. The closure gate
# (iteration-close.sh, SG-b) then refuses clean-success goal closure while the
# obligation is unresolved; deploy-verify.sh resolves it.
#
# WHY command-detection, not success-detection: tool_response is NOT reliably in
# the PostToolUse payload, so a `git push` COMMAND is detected and deploy-verify.sh
# is the resolution-time truth. Detection BIASES TOWARD PRECISION — a missed
# exotic push form is safe (the goal closes ungated; the agent can run
# deploy-verify manually), but a FALSE POSITIVE is not: an obligation for a sha
# that was never pushed produces no CI runs, so deploy-verify returns
# "unverified" forever and the goal can never close. So: match `git push` and
# `git -C <dir> push` only (NOT `git stash push`); exclude --dry-run/-n/--help.
#
# Mirrors bash-edit-record.sh: set -u only (NO set -e / pipefail), every probe
# guarded, agent resolved from the payload session_id (PostToolUse[Bash] gets no
# MIND_AGENT in env). Fail-open EVERYWHERE (guard-141): a hook failure must
# NEVER block the LLM's command.

set -u

input=$(cat 2>/dev/null) || exit 0

# ── HOT PATH: cheap raw pre-filter. No push substring -> exit before any
#    python3/git. This is what keeps the hook off the per-Bash-call critical path.
case "$input" in
    *push*) : ;;
    *) exit 0 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || exit 0
# _paths.sh puts core/scripts/.python-shim on PATH (python3 -> py -3, not the
# Windows Store stub) AND exports PROJECT_ROOT, AGENTS_PARENT_DIR, agent_dir().
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0

command=$(printf '%s' "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")
[ -n "$command" ] || exit 0

# ── PRECISE detection: `git push` or `git -C <dir> push`, as the git subcommand
#    (excludes `git stash push`, `array.push`, `echo push`, etc.).
printf '%s' "$command" | grep -qE '(^|[;&|[:space:]])git[[:space:]]+(-C[[:space:]]+[^[:space:]]+[[:space:]]+)?push([[:space:]]|;|&|\||$)' 2>/dev/null || exit 0
# Exclude non-deploying push forms.
case "$command" in
    *--dry-run*|*" -n "*|*" -n"|*--help*) exit 0 ;;
esac

# ── Resolve the agent from session_id (no MIND_AGENT in PostToolUse env). ──
session_id=$(printf '%s' "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
agent="${MIND_AGENT:-}"
if [ -z "$agent" ] && [ -n "$session_id" ]; then
    for _bf in "$PROJECT_ROOT/${AGENTS_PARENT_DIR}"/*/sessions/"$session_id"/binding.yaml; do
        [ -f "$_bf" ] || continue
        _bd="${_bf%/sessions/*}"; agent="${_bd##*/}"; break
    done
    if [ -z "$agent" ]; then
        _binding="$PROJECT_ROOT/.active-agent-$session_id"
        [ -f "$_binding" ] && { read -r agent < "$_binding" 2>/dev/null || true; agent="${agent//[[:space:]]/}"; }
    fi
fi
[ -n "$agent" ] || exit 0

# ── Determine the git dir the push ran in: `git -C <dir>` > `cd <dir> &&` >
#    payload cwd > PROJECT_ROOT. ──
gitdir=""
_gc=$(printf '%s' "$command" | sed -nE 's/.*git[[:space:]]+-C[[:space:]]+([^[:space:]]+).*/\1/p' | head -1)
[ -n "$_gc" ] && gitdir="$_gc"
if [ -z "$gitdir" ]; then
    _cd=$(printf '%s' "$command" | sed -nE 's/.*cd[[:space:]]+([^[:space:]&;|]+).*/\1/p' | head -1)
    [ -n "$_cd" ] && gitdir="$_cd"
fi
if [ -z "$gitdir" ]; then
    gitdir=$(printf '%s' "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")
fi
[ -n "$gitdir" ] || gitdir="$PROJECT_ROOT"

# ── Capture repo + sha AT PUSH TIME (sha MUST be captured now — HEAD moves by
#    resolution time). Guarded single git calls. ──
sha=$(git -C "$gitdir" rev-parse HEAD 2>/dev/null || echo "")
[ -n "$sha" ] || exit 0   # not a git repo / cannot read HEAD -> nothing trackable
url=$(git -C "$gitdir" remote get-url origin 2>/dev/null || echo "")
repo=$(printf '%s' "$url" | sed -E 's#^(git@|https://)([^/:]+)[:/]##; s#\.git$##' 2>/dev/null || echo "")
[ -n "$repo" ] || exit 0  # no origin remote -> nothing to deploy-verify

# ── Resolve the in-flight goal_id from the execution diary (last phase-4-execute
#    phase_start). Empty is acceptable — the stop-hook gate (SG-c) still catches
#    an untagged obligation; only the per-goal verify gate (SG-b) needs it. ──
_asd="$(agent_dir "$agent" 2>/dev/null)/session"
goal_id=""
_diary="$_asd/execution-diary.jsonl"
if [ -f "$_diary" ]; then
    goal_id=$(DIARY="$_diary" python3 - <<'PYEOF' 2>/dev/null || echo ""
import os, json
last = ""
try:
    with open(os.environ["DIARY"], encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("phase") == "phase-4-execute" and e.get("entry_type") == "phase_start":
                last = e.get("goal_id", "") or ""
except Exception:
    pass
print(last)
PYEOF
)
fi

# ── Append the obligation (fail-open; pending-deploys.py never raises). ──
python3 "$SCRIPT_DIR/pending-deploys.py" --agent "$agent" add \
    --repo "$repo" --sha "$sha" --goal-id "$goal_id" --dir "$gitdir" >/dev/null 2>&1 || true

exit 0
