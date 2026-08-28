#!/usr/bin/env bash
# domain-leak-exempt: WORLD_ALIAS holds literal sibling-world environment-ids required
# for M2 cross-world goal injection routing — operational values, not pedagogical examples.
# (Was "WORLD_MAP holds literal ... directory names" until 2026-07-31; the hardcoded
# directory map is gone — paths now resolve per-machine. Only the env-id aliases remain.)
#
# Cross-world M2: inject a sandboxed, human-approval-gated goal into a sibling world.
#
# Guardrails enforced automatically (no caller opt-in required for G2/G3):
#   G1 (guard-64) default-Vault: requires --shared flag
#   G2 (guard-65) sandboxing: stamps injected_by + sandbox:true on goal record
#   G3 (guard-66) human approval: forces participants:[agent,user] on goal
#   G4 (guard-67) rate-limit: max 3 injections per source per 24h per target
#   G5 (guard-68) provenance: stamps cross_world_origin, cross_world_reason, cross_world_timestamp
#
# Usage:
#   bash core/scripts/cross-world-inject-goal.sh \
#     --target ayoai \
#     --title "Investigate: something seen from zds-mind" \
#     --description "Why the target world should look at this" \
#     --priority MEDIUM \
#     --reason "Observed X in session Y, relevant to target's domain" \
#     --shared [--dry-run]
#
# Flags:
#   --target <name>       Target world (required). Alias or env-id: ayoai|claude|zds,
#                         or any env-id under core/config/environments/ directly.
#                         The DIRECTORY is resolved per-machine, never hardcoded:
#                         $PEER_WORLD_<ENV_ID>, else peer_world_path: in the
#                         registry entry. Exit 3 = not hosted on this box (normal).
#   --title <text>        Goal title (required; include intent prefix like "Investigate:")
#   --description <text>  Goal description / motivation (required)
#   --priority <P>        HIGH | MEDIUM | LOW (default: MEDIUM)
#   --category <cat>      Goal category (default: cross-world-signal)
#   --reason <text>       Why this injection is happening (required; G5 provenance)
#   --shared              Mark artifact as Shared (required; G1 default-Vault)
#   --target-aspiration <id>  Standard aspiration for selector-visible goal (default: asp-115)
#                             Layer 1 fix: injects goal in g-{N}-{seq} format, visible to selector.
#   --dry-run             Print what would be written; do not write
#   --help                Show this help
#
# G2/G3 are applied unconditionally — callers cannot opt out. Every injected goal
# carries sandbox:true + injected_by (G2) and participants:[agent,user] (G3), ensuring
# the target world's human always reviews the injected work before it executes.
#
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# ── Target-world resolution (PER-MACHINE — never a hardcoded literal) ────────
# A sibling world's directory is a filesystem path that exists on SOME machines
# and not others (peer_board_post.py records boxes where no peer world is present
# at all). A literal absolute path here is therefore correct on at most ONE box
# and dead on every other: until 2026-07-31 this held a single Windows path, so
# on every Linux box this script died before writing anything — and because the
# SANCTIONED transport for promotion-cycle rule 2 runs through here, that rule
# stayed technically obeyed and practically unexecutable fleet-wide. Nothing
# surfaced it, because from inside the loop a refusal to build framework code
# locally and a successful routing to the dev world look identical (g-115-4191).
#
# Resolution reuses the ESTABLISHED cross-deployment convention rather than
# inventing a second one — core/config/conventions/cross-deployment-channel.md:
#   1. $PEER_WORLD_<ENV_ID>  — env-id upper-cased, hyphens → underscores
#   2. peer_world_path:      — in core/config/environments/<env-id>.yaml
# There is deliberately NO built-in default and NO fallback. An ABSENT path is
# diagnosable; a WRONG one writes into the wrong world, which is the
# guard-955 / rb-2983 hazard. Not hosting a peer is NORMAL and exits 3, the same
# contract peer-board-post.sh already uses.
declare -A WORLD_ALIAS
WORLD_ALIAS[ayoai]="ayoai-mind"
WORLD_ALIAS[claude]="claude-mind"
WORLD_ALIAS[zds]="zds-mind"
RESOLVED_WORLD_DIR=""

# ── Origin identity (G5 provenance) ──────────────────────────────────────────
# DERIVED from ENVIRONMENT_ID — never hardcoded. This block MIRRORS
# cross-world-post.sh:49-78, which was fixed 2026-07-30; this file was the
# generalization remainder (guard-2078) and still read "omni@zds-mind" until
# 2026-07-31. That literal is correct in at most one promotion tier and silently
# forges a peer identity in every other — running from ayoai-mind stamped both
# injected_by (G2) and cross_world_origin (G5) as zds-mind's agent.
#
# This mattered MORE once the WORLD_MAP fix above revived the transport: a dead
# script stamps nothing, so reviving it without this would have converted a
# silent no-op into silent misattribution — strictly worse.
#
# DIE rather than default: a record with a WRONG provenance stamp is worse than
# no record, and G5 makes provenance mandatory (guard-68).
ORIGIN_AGENT="${MIND_AGENT:-}"
ORIGIN_ENV="${ENVIRONMENT_ID:-}"
# The AGENT half gets the SAME treatment as the env half below. It used to read
# `${MIND_AGENT:-omni}`, which is the very forgery the block above forbids:
# a literal agent identity, correct in at most one deployment and wrong in every
# other — and it sat two lines under a comment saying "DIE rather than default".
# A default here is worse than the env one it mirrors, because an unset
# MIND_AGENT is the NORMAL shape for a cron or a hand-run, i.e. exactly the
# caller least likely to notice the stamp is a lie.
if [ -z "$ORIGIN_AGENT" ]; then
    echo "cross-world-inject-goal.sh: cannot resolve MIND_AGENT — refusing to inject" >&2
    echo "  G5 (guard-68) requires a provenance stamp; defaulting the agent half to a" >&2
    echo "  literal identity would forge a peer agent on every other deployment." >&2
    echo "  Run from an agent session, or set MIND_AGENT explicitly." >&2
    exit 2
fi
if [ -z "$ORIGIN_ENV" ] && [ -f "${PROJECT_ROOT:-.}/.env.local" ]; then
    ORIGIN_ENV=$(grep -m1 '^ENVIRONMENT_ID=' "${PROJECT_ROOT:-.}/.env.local" 2>/dev/null \
                 | cut -d= -f2- | tr -d '"'"'"' \r')
fi
if [ -z "$ORIGIN_ENV" ]; then
    echo "cross-world-inject-goal.sh: cannot resolve ENVIRONMENT_ID — refusing to inject" >&2
    echo "  G5 (guard-68) requires a provenance stamp; an unstamped or guessed" >&2
    echo "  origin would misattribute this injection to another deployment." >&2
    echo "  Set ENVIRONMENT_ID in .env.local." >&2
    exit 2
fi
# `<agent>@<env-id>` — '@' not '-': every registry env-id contains a hyphen, so
# the hyphen form cannot be split back into (agent, env). See
# core/config/conventions/cross-deployment-channel.md.
ORIGIN="${ORIGIN_AGENT}@${ORIGIN_ENV}"

# ── Constants ────────────────────────────────────────────────────────────────
M2_RATE_LIMIT=3          # G4: max goal injections per source per 24h per target
LOCK_TIMEOUT_TRIES=50    # 50 × 100ms = 5s max wait for file lock
TARGET_ASPIRATION="asp-115"  # Layer 1: default standard aspiration for selector-visible goal

# ── Helpers ──────────────────────────────────────────────────────────────────
usage() {
    cat <<'USAGE'
cross-world-inject-goal.sh — M2 cross-world aspiration injection (sandboxed + human-gated)

USAGE:
  bash core/scripts/cross-world-inject-goal.sh \
    --target <name> --title "<text>" --description "<text>" \
    --reason "<why>" --shared [--priority MEDIUM] [--category <cat>] [--dry-run]

FLAGS:
  --target <name>       Target world (required). Alias or env-id: ayoai|claude|zds,
                        or any env-id under core/config/environments/ directly.
                        The DIRECTORY is resolved per-machine, never hardcoded:
                        $PEER_WORLD_<ENV_ID>, else peer_world_path: in the
                        registry entry. Exit 3 = not hosted on this box (normal).
  --title <text>        Goal title (required; e.g. "Investigate: X" or "Idea: Y")
  --description <text>  Goal description / motivation (required)
  --priority <P>        HIGH | MEDIUM | LOW (default: MEDIUM)
  --category <cat>      Goal category tag (default: cross-world-signal)
  --reason <text>       Why this injection is happening (required; G5 provenance)
  --shared              Mark as Shared, enabling cross-world write (required; G1)
  --dry-run             Preview what would be written; do not write
  --help                Show this help

GUARDRAILS (automatic — no opt-in required):
  G1: Refuses without --shared (default-Vault policy)
  G2: Stamps injected_by + sandbox:true on the injected goal record
  G3: Forces participants:[agent,user] so a human reviews before the goal executes
  G4: Rate-limits to 3 injections per 24h from this origin per target
  G5: Stamps cross_world_origin, cross_world_reason, cross_world_timestamp

OPTIONS:
  --target-aspiration <id>  Standard aspiration for the selector-visible goal (default: asp-115).
                            Defense-in-depth (Layer 1): the asp-xw-* record's goals[]
                            is ALREADY selector-visible (goal-selector collect_candidates
                            iterates it + scores via live effective_counts), but the goal
                            carries participants:[agent,user] + a non-standard g-xw-* ID, so
                            this option ALSO injects a SECOND goal in standard g-{N}-{seq}
                            format under a real aspiration. Pass the relevant aspiration ID.
                            (g-115-1641: the asp-xw STORED-progress cache historically
                            displayed (0/0) in --summary until the writer was fixed to
                            populate it -- a count-display gap, never a selection gap.)

NOTES:
  - Each injection creates TWO records in the target's aspirations.jsonl:
      1. asp-xw-* audit record (provenance trail; goals[] IS selector-visible —
         g-115-1641 fixed the stored-progress cache that historically showed (0/0))
      2. g-{asp_num}-{seq} goal record under --target-aspiration (selector-visible; Layer 1)
  - Layer 2: post-injection verification confirms the selector-visible goal is on disk.
  - The target world's agents see participants:[agent,user] and must wait for human approval
    before the goal transitions from pending to in-progress.
  - Rate-limit window is 24h from the oldest un-expired injection; check is per-(origin, target).
USAGE
    exit 0
}

die() { echo "ERROR: $*" >&2; exit 1; }

# Exit 3 = "peer not reachable FROM THIS BOX". Deliberately distinct from die()'s
# exit 1: not hosting a peer world is the COMMON, EXPECTED case on a fleet box and
# must be branchable by callers, exactly as peer-board-post.sh already does.
# UNREACHABLE is not UNDELIVERABLE — the diagnostic says so, because the previous
# message ("Target world directory does not exist") read as a hard dead end and a
# real user decision was once filed as blocked on box topology (g-115-4165).
_peer_unreachable() {
    local name="$1" env_id="$2" var="$3" path="$4"
    {
        if [ -n "$path" ]; then
            echo "ERROR: world directory for '$name' (env-id: $env_id) does not exist: $path"
        else
            echo "ERROR: no world directory configured for '$name' (env-id: $env_id)."
        fi
        echo
        echo "This is NORMAL and often not a bug: a peer world is a per-machine filesystem"
        echo "path, and this box may simply not host that world."
        echo
        echo "If this box DOES host it, point at it (either form):"
        echo "  export ${var}=/path/to/${env_id}/world"
        echo "  or set  peer_world_path:  in core/config/environments/${env_id}.yaml"
        echo
        echo "If this box does NOT host it, the work is still deliverable — use the LOCAL board:"
        echo "  echo \"<the ask>\" | bash core/scripts/board-post.sh --channel coordination \\"
        echo "    --type directive --tags <topic>,requires_action_by:<agent>@${env_id}"
        echo "  (message text comes from STDIN — there is no --message flag; and"
        echo "   requires_action_by is a TAG VALUE, not a flag. Both verified live"
        echo "   2026-08-28, rc=0.)"
        echo "  The peer READS this world's board and acts on it (measured: guard-2082 cites"
        echo "  two answered relays plus a third acked at 28.6h). Request an acknowledging"
        echo "  --reply-to so delivery is CONFIRMED rather than assumed, and do not rely on"
        echo "  this channel alone for anything due inside ~36h — it is LATENT, not"
        echo "  unreliable. This is not the forbidden silent fallback: that prohibition is on"
        echo "  peer-board-post.sh redirecting a peer WRITE while reporting success."
        echo
        echo "  NOT peer-board-post.sh. It resolves the target world through the SAME two"
        echo "  sources this script just failed on (peer_board_post.py::peer_world_path:"
        echo "  \$PEER_WORLD_<ENV_ID>, then registry peer_world_path:), so it exits 3 for the"
        echo "  same reason every time it is reached from here — measured 2026-08-28, rc=3"
        echo "  for all four registered peers. It cannot be a fallback for THIS failure."
        echo "  See core/config/conventions/cross-deployment-channel.md."
        echo
        echo "Do NOT hand-edit a literal path into this script: a path that is right on one"
        echo "box is wrong on every other, and a WRONG path writes into the WRONG world"
        echo "(guard-955 / rb-2983). That is how this script came to be dead fleet-wide."
    } >&2
    exit 3
}

# Resolves the target world dir per-machine into RESOLVED_WORLD_DIR, or exits 3.
resolve_target() {
    local name="$1"
    local env_id="${WORLD_ALIAS[$name]:-$name}"
    local var
    var="PEER_WORLD_$(printf '%s' "$env_id" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
    # 1. env-var override (indirect expansion; ':-' keeps `set -u` happy)
    local path="${!var:-}"
    # 2. registry entry. Parse with sed, NOT `cut -d:` — a peer_world_path may be
    #    a Windows path whose drive colon `cut` would split on, silently yielding
    #    a truncated path that then fails the -d test for the wrong reason.
    if [ -z "$path" ]; then
        local reg="${PROJECT_ROOT:-.}/core/config/environments/${env_id}.yaml"
        if [ -f "$reg" ]; then
            path=$(sed -n 's/^[[:space:]]*peer_world_path:[[:space:]]*//p' "$reg" \
                   | head -1 | tr -d '"' | tr -d "'" | sed 's/[[:space:]]*$//')
        fi
    fi
    if [ -z "$path" ] || [ ! -d "$path" ]; then
        _peer_unreachable "$name" "$env_id" "$var" "$path"
    fi
    RESOLVED_WORLD_DIR="$path"
}

# G4: count injections from ORIGIN into target's aspirations.jsonl in the last 24h.
check_rate_limit() {
    local asp_file="$1"
    [ -f "$asp_file" ] || return 0   # file doesn't exist yet — no prior injections

    local count
    count=$(py -3 -c "
import json, sys
from datetime import datetime, timedelta

origin = sys.argv[1]
asp_file = sys.argv[2]
now = datetime.now()
window_start = now - timedelta(hours=24)
count = 0
with open(asp_file, 'r', encoding='utf-8') as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get('cross_world_origin') != origin:
            continue
        ts = rec.get('cross_world_timestamp', '')
        try:
            dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S')
            if dt >= window_start:
                count += 1
        except ValueError:
            pass
print(count)
" "$ORIGIN" "$asp_file" 2>/dev/null || echo 0)

    if [ "$count" -ge "$M2_RATE_LIMIT" ]; then
        die "G4 rate-limit exceeded: $count injections from $ORIGIN in the last 24h to this target (cap: $M2_RATE_LIMIT). Wait before injecting again."
    fi
}

# Simple file-lock (noclobber spinlock)
acquire_lock() {
    local lockf="$1"
    local tries=0
    while ! ( set -o noclobber; echo $$ > "$lockf" ) 2>/dev/null; do
        tries=$((tries + 1))
        [ $tries -ge $LOCK_TIMEOUT_TRIES ] && die "Could not acquire lock on $lockf after $LOCK_TIMEOUT_TRIES attempts"
        sleep 0.1
    done
}
release_lock() { rm -f "$1"; }

# ── Argument parsing ─────────────────────────────────────────────────────────
TARGET=""
TITLE=""
DESCRIPTION=""
PRIORITY="MEDIUM"
CATEGORY="cross-world-signal"
REASON=""
SHARED=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)       TARGET="$2";      shift $(( $# >= 2 ? 2 : 1 )) ;;
        --title)        TITLE="$2";       shift $(( $# >= 2 ? 2 : 1 )) ;;
        --description)  DESCRIPTION="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --priority)     PRIORITY="$2";    shift $(( $# >= 2 ? 2 : 1 )) ;;
        --category)     CATEGORY="$2";    shift $(( $# >= 2 ? 2 : 1 )) ;;
        --reason)       REASON="$2";      shift $(( $# >= 2 ? 2 : 1 )) ;;
        --shared)       SHARED=true;      shift   ;;
        --target-aspiration) TARGET_ASPIRATION="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --dry-run)      DRY_RUN=true;     shift   ;;
        --help|-h)      usage ;;
        *)              die "Unknown argument: $1. Use --help for usage." ;;
    esac
done

# ── Validation ───────────────────────────────────────────────────────────────
[ -z "$TARGET" ]      && die "Missing --target. Use --help for usage."
[ -z "$TITLE" ]       && die "Missing --title."
[ -z "$DESCRIPTION" ] && die "Missing --description."
[ -z "$REASON" ]      && die "Missing --reason (G5 provenance requires a reason)."

# G1: default-Vault — refuse unless explicitly Shared
[ "$SHARED" != "true" ] && die "G1 (guard-64): Cross-world influence default is VAULT. Pass --shared to explicitly mark this injection as Shared."

# Validate priority
case "$PRIORITY" in
    HIGH|MEDIUM|LOW) ;;
    *) die "Invalid --priority '$PRIORITY'. Must be HIGH, MEDIUM, or LOW." ;;
esac

# Resolve target world
resolve_target "$TARGET"
TARGET_DIR="$RESOLVED_WORLD_DIR"
ASP_FILE="$TARGET_DIR/aspirations.jsonl"

# G4: rate limit check. BEST-EFFORT, not a guarantee: this reads the target file
# BEFORE the lock is acquired further down, so two concurrent injections can both
# observe "one slot left" and both write. Recorded so nobody cites G4 as an
# enforced invariant; tightening it means moving this call inside the lock.
check_rate_limit "$ASP_FILE"

# ── Build the injected records ────────────────────────────────────────────────
TIMESTAMP="$(date +%Y-%m-%dT%H:%M:%S)"
ID_SLUG="$(date +%Y%m%dT%H%M%S)"
ASP_ID="asp-xw-${ID_SLUG}"
GOAL_ID="g-xw-${ID_SLUG}-01"

# Build the full aspiration + embedded goal record via Python.
# G2: sandbox:true + injected_by on both aspiration and goal
# G3: participants:[agent,user] on the goal — human must approve before goal executes
# G5: cross_world_origin, cross_world_reason, cross_world_timestamp on aspiration
RECORD=$(py -3 -c "
import json, sys

asp_id       = sys.argv[1]
goal_id      = sys.argv[2]
title        = sys.argv[3]
description  = sys.argv[4]
priority     = sys.argv[5]
category     = sys.argv[6]
origin       = sys.argv[7]
reason       = sys.argv[8]
timestamp    = sys.argv[9]

# G2 + G3: the injected goal — sandbox-flagged, human-gated
goal = {
    'id': goal_id,
    'title': title,
    'status': 'pending',
    'priority': priority,
    'category': category,
    'participants': ['agent', 'user'],   # G3: human approval required
    'sandbox': True,                      # G2: sandboxed
    'injected_by': origin,               # G2: provenance on the goal
    'created_at': timestamp,
    'origin_signal': 'cross-world-injection',
}

# G5: the aspiration wrapper — provenance-stamped
aspiration = {
    'id': asp_id,
    'title': f'[xw] {title}',
    'motivation': description,
    'priority': priority,
    'status': 'active',
    'source': 'cross-world-injection',
    'tags': list(dict.fromkeys([category, 'cross-world-signal', 'sandbox'])),
    'scope': 'project',
    'archived': False,
    'sandbox': True,                      # G2: sandbox at aspiration level
    'injected_by': origin,               # G2
    'cross_world_origin': origin,        # G5
    'cross_world_reason': reason,        # G5
    'cross_world_timestamp': timestamp,  # G5
    'goals': [goal],
    # g-115-1641: populate the denormalized progress cache + initial_goal_count
    # at injection. cross-world-inject-goal.sh appends the record RAW (bypassing
    # cmd_add_goal, the path that normally calls recompute_progress), so without
    # this the record carries NO progress field and the two STORED-progress
    # readers (aspirations --summary endpoint + consolidation-health.py) display
    # it as (0/0 goals) until a goal status-change first triggers recompute.
    # NOTE: this fixes the COUNT display only -- the selector already surfaces
    # these goals correctly (goal-selector collect_candidates iterates goals[]
    # and scores via live _goal_census.effective_counts, never the stored cache).
    # The injected goal is always a single pending non-recurring goal -> 0/1.
    'initial_goal_count': 1,
    'progress': {
        'completed_goals': 0,
        'total_goals': 1,
        'recurring_goals': 0,
        'fan_out_ratio': 1.0,
    },
    'created_at': timestamp,
}

print(json.dumps(aspiration, ensure_ascii=True))
" "$ASP_ID" "$GOAL_ID" "$TITLE" "$DESCRIPTION" "$PRIORITY" "$CATEGORY" \
  "$ORIGIN" "$REASON" "$TIMESTAMP")

# ── Dry-run or live write ────────────────────────────────────────────────────
if [ "$DRY_RUN" = "true" ]; then
    echo "DRY-RUN: Would append TWO records to $ASP_FILE:"
    echo ""
    echo "[1] Audit record (asp-xw-* -- provenance trail; goals[] IS selector-visible; progress cache now populated, g-115-1641):"
    echo "$RECORD" | py -3 -c "import json,sys; print(json.dumps(json.loads(sys.stdin.read()),indent=2))"
    echo ""
    echo "[2] Selector-visible goal (Layer 1): g-${TARGET_ASPIRATION#asp-}-<next_seq> in $TARGET_ASPIRATION"
    echo "    origin_signal: user_directive | participants: [agent,user] | sandbox: true"
    echo "    cross_world_audit_ref: $ASP_ID"
    echo ""
    echo "Audit asp ID  : $ASP_ID"
    echo "Target asp    : $TARGET_ASPIRATION (Layer 1 selector-visible goal)"
    echo "G2 sandbox    : true + injected_by=$ORIGIN"
    echo "G3 approval   : participants=[agent,user]"
    echo "G4 rate-limit : checked (cap $M2_RATE_LIMIT/24h)"
    echo "G5 provenance : origin=$ORIGIN reason='$REASON' ts=$TIMESTAMP"
    echo "Layer 2       : post-injection verification will run after write"
    exit 0
fi

# ── Dual write: audit record + Layer 1 selector-visible goal ────────────────
_ASP_NUM="${TARGET_ASPIRATION#asp-}"

LOCK_FILE="$ASP_FILE.lock"
acquire_lock "$LOCK_FILE"
# shellcheck disable=SC2064
trap "release_lock '$LOCK_FILE'" EXIT

# Write [1]: audit record (asp-xw-* with embedded goals[] — for provenance only)
#
# Normalize the trailing newline FIRST. `>>` concatenates onto the final line
# when the peer's store does not end in one, silently merging the last existing
# record and this audit record into a single unparseable line. Write [2] then
# hits that line, its `except Exception: continue` swallows it, and the target
# aspiration is reported NOT FOUND — so a store that was merely missing a final
# newline loses a record AND fails the injection, with the root cause nowhere in
# the error. A JSONL file with no trailing newline is ordinary: any writer that
# emits records without a final separator produces one.
# Found 2026-07-31 by the false-abort regression test, on the pre-existing
# `>>` (g-115-4204). It was invisible until the Write [2] diagnostic in this
# same change became reachable — before that it died bare, with no message.
if [ -s "$ASP_FILE" ] && [ -n "$(tail -c 1 "$ASP_FILE")" ]; then
    printf '\n' >> "$ASP_FILE"
fi
echo "$RECORD" >> "$ASP_FILE"

# Layer 1: find next goal sequence number -- scans embedded goals in TARGET_ASPIRATION
# (g-115-1 fix: previously scanned only top-level record IDs, missing all embedded goals)
SECONDARY_GOAL_ID=$(py -3 -c "
import json, sys, re
asp_num  = sys.argv[1]
asp_id   = sys.argv[2]
asp_file = sys.argv[3]
max_seq = 0
pat = re.compile(r'^g-' + re.escape(asp_num) + r'-(\d+)\$')
try:
    with open(asp_file, 'r', encoding='utf-8') as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except Exception:
                continue
            if rec.get('id') == asp_id:
                for goal in rec.get('goals', []):
                    m = pat.match(goal.get('id', ''))
                    if m:
                        sq = int(m.group(1))
                        if sq > max_seq:
                            max_seq = sq
            else:
                m = pat.match(rec.get('id', ''))
                if m:
                    sq = int(m.group(1))
                    if sq > max_seq:
                        max_seq = sq
except FileNotFoundError:
    pass
print(f'g-{asp_num}-{max_seq + 1}')
" "$_ASP_NUM" "$TARGET_ASPIRATION" "$ASP_FILE")

# Write [2]: inject goal into TARGET_ASPIRATION's goals[] array (in-place rewrite)
# (g-115-1 fix: previously appended standalone records invisible to collect_candidates())
set +e   # see the WRITE2 rc handling below -- do NOT let set -e swallow this
WRITE2_RC=$(py -3 -c "
import json, os, sys, tempfile
goal_id=sys.argv[1]; title=sys.argv[2]; description=sys.argv[3]
priority=sys.argv[4]; category=sys.argv[5]; origin=sys.argv[6]
reason=sys.argv[7]; timestamp=sys.argv[8]; audit_ref=sys.argv[9]
target_asp=sys.argv[10]; asp_file=sys.argv[11]; origin_agent=sys.argv[12]
goal = {
    'id':                    goal_id,
    'title':                 title,
    'description':           description,
    'status':                'pending',
    'priority':              priority,
    'category':              category,
    'participants':          ['agent', 'user'],
    'sandbox':               True,
    'injected_by':           origin,
    'cross_world_origin':    origin,
    'cross_world_reason':    reason,
    'cross_world_timestamp': timestamp,
    'cross_world_audit_ref': audit_ref,
    'origin_signal':         'user_directive',
    'tags':                  list(dict.fromkeys([category, 'cross-world-signal', 'sandbox', 'xw-injected'])),
    'work_class':            'unclassified',
    'goal_source':           'user',
    'intended_agent':        'either',
    'filed_by_agent':        origin_agent,
    'created_at':            timestamp,
}
output_lines = []
found = False
with open(asp_file, 'r', encoding='utf-8') as fh:
    for line in fh:
        s = line.strip()
        if not s:
            output_lines.append(line)
            continue
        try:
            rec = json.loads(s)
        except Exception:
            output_lines.append(line)
            continue
        if rec.get('id') == target_asp and 'goals' in rec:
            rec['goals'].append(goal)
            output_lines.append(json.dumps(rec, ensure_ascii=True) + '\n')
            found = True
        else:
            output_lines.append(line)
if not found:
    print(f'ERROR: target aspiration {target_asp} not found', file=sys.stderr)
    sys.exit(1)

# guard-1706: a bulk read-modify-write of a GOVERNED store belonging to ANOTHER
# world. Two hardening steps, and the reason each is shaped this way:
#
# (1) Concurrency. The peer's agents write this file through the PEER's daemon
#     and do not honour this script's spinlock, so a line appended while we were
#     building output_lines would be silently dropped by the rewrite. Re-count
#     immediately before the swap and ABORT rather than clobber.
# (2) Atomicity. The previous form opened the peer's file with mode 'w', which
#     TRUNCATES before writing: a crash in that window leaves the peer's entire
#     goal store empty. That hazard is realized, not theoretical -- on 2026-07-09
#     a world aspirations.jsonl went from 1366 goals to a single fixture and
#     needed .history recovery (guard-955 / rb-2983).
#
# Deliberately NOT _fileops.locked_modify_jsonl, which guard-1706 otherwise
# prescribes: that helper routes through THIS world's storage backend, and under
# own-cloud OwnCloudBackend._s3_key derives the key from the local
# customer_prefix+env_id rather than from the path -- so it would write the
# PEER's content onto THIS world's S3 key. Raw filesystem I/O on the resolved
# peer path is the correct tool here.
with open(asp_file, 'r', encoding='utf-8') as fh:
    lines_now = sum(1 for _ in fh)
if lines_now != len(output_lines):
    print(f'ERROR: {asp_file} changed while the rewrite was being built '
          f'({len(output_lines)} lines read, {lines_now} now) -- refusing to '
          f'clobber a concurrent write. Re-run the injection.', file=sys.stderr)
    sys.exit(2)

target_dir = os.path.dirname(os.path.abspath(asp_file)) or '.'
fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix='.aspirations-inject-', suffix='.tmp')
try:
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
        fh.writelines(output_lines)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, asp_file)
except BaseException:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise
print('ok')
" "$SECONDARY_GOAL_ID" "$TITLE" "$DESCRIPTION" "$PRIORITY" "$CATEGORY" \
  "$ORIGIN" "$REASON" "$TIMESTAMP" "$ASP_ID" "$TARGET_ASPIRATION" "$ASP_FILE" \
  "$ORIGIN_AGENT")
WRITE2_EXIT=$?
set -e

release_lock "$LOCK_FILE"
trap - EXIT

# Under `set -euo pipefail`, VAR=$(cmd) inherits cmd's exit status, so a non-zero
# python exit killed the shell at the assignment ABOVE -- before this check could
# ever run. The diagnostic was unreachable on every failure path it was written
# for. The old `2>&1` compounded it by capturing python's stderr INTO the
# variable, so a failed peer write produced a bare exit 1 with no message at all.
# stderr now flows straight to the terminal and the rc is captured explicitly.
if [ "$WRITE2_EXIT" -ne 0 ] || [ "$WRITE2_RC" != "ok" ]; then
    echo "ERROR: Write [2] FAILED (rc=$WRITE2_EXIT) -- ${WRITE2_RC:-see python diagnostic above}" >&2
    echo "  Target aspiration: $TARGET_ASPIRATION in $ASP_FILE" >&2
    echo "  Audit record $ASP_ID was already written by Write [1] and remains." >&2
    exit 1
fi

# Layer 2: post-injection verification -- check goal embedded in TARGET_ASPIRATION's goals[]
VERIFY=$(py -3 -c "
import json, sys
goal_id=sys.argv[1]; target_asp=sys.argv[2]; asp_file=sys.argv[3]
with open(asp_file, 'r', encoding='utf-8') as fh:
    for line in fh:
        try:
            rec = json.loads(line.strip())
        except Exception:
            continue
        if rec.get('id') == target_asp:
            for goal in rec.get('goals', []):
                if goal.get('id') == goal_id:
                    print('ok')
                    sys.exit(0)
print('not_found')
" "$SECONDARY_GOAL_ID" "$TARGET_ASPIRATION" "$ASP_FILE" 2>/dev/null || echo "error")

if [ "$VERIFY" = "ok" ]; then
    echo "Injected: audit=$ASP_ID | selector-visible=$SECONDARY_GOAL_ID in $TARGET_ASPIRATION"
    echo "G2 sandbox: true | G3 participants: [agent,user] | Layer 2 verified: embedded in goals[]"
else
    echo "ERROR: Layer 2 verify FAILED -- $SECONDARY_GOAL_ID not confirmed embedded in $TARGET_ASPIRATION" >&2
    echo "Audit record $ASP_ID was written but selector-visible goal is missing." >&2
    echo "RECOVERY: Re-run injection or manually file goal into $TARGET_ASPIRATION" >&2
    exit 1
fi
