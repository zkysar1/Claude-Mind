#!/usr/bin/env bash
# domain-leak-exempt: WORLD_MAP holds literal sibling-world directory names required
# for M2 cross-world goal injection routing — operational values, not pedagogical examples.
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
#   --target <name>       Target world name (required). Known: ayoai
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

# ── Name-to-path map for known sibling worlds ────────────────────────────────
# Add entries here as new sibling worlds become reachable.
declare -A WORLD_MAP
WORLD_MAP[ayoai]="C:/ZakNoCloud/AyoaiCache/Ayoai-World"

# ── Constants ────────────────────────────────────────────────────────────────
ORIGIN="omni@zds-mind"
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
  --target <name>       Target world name (required). Known: ayoai
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
                            Layer 1 fix: the asp-xw-* record stores goals in a nested goals[]
                            array that the selector counts as (0/0). This option injects a
                            SECOND goal record in g-{N}-{seq} format under a real aspiration,
                            making it visible and scoreable. Pass the relevant aspiration ID.

NOTES:
  - Each injection creates TWO records in the target's aspirations.jsonl:
      1. asp-xw-* audit record (provenance trail; nested goals[] not selector-visible)
      2. g-{asp_num}-{seq} goal record under --target-aspiration (selector-visible; Layer 1)
  - Layer 2: post-injection verification confirms the selector-visible goal is on disk.
  - The target world's agents see participants:[agent,user] and must wait for human approval
    before the goal transitions from pending to in-progress.
  - Rate-limit window is 24h from the oldest un-expired injection; check is per-(origin, target).
USAGE
    exit 0
}

die() { echo "ERROR: $*" >&2; exit 1; }

resolve_target() {
    local name="$1"
    local path="${WORLD_MAP[$name]:-}"
    [ -z "$path" ] && die "Unknown target world '$name'. Known targets: ${!WORLD_MAP[*]}"
    [ -d "$path" ] || die "Target world directory does not exist: $path"
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
        --target)       TARGET="$2";      shift 2 ;;
        --title)        TITLE="$2";       shift 2 ;;
        --description)  DESCRIPTION="$2"; shift 2 ;;
        --priority)     PRIORITY="$2";    shift 2 ;;
        --category)     CATEGORY="$2";    shift 2 ;;
        --reason)       REASON="$2";      shift 2 ;;
        --shared)       SHARED=true;      shift   ;;
        --target-aspiration) TARGET_ASPIRATION="$2"; shift 2 ;;
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
TARGET_DIR="${WORLD_MAP[$TARGET]}"
ASP_FILE="$TARGET_DIR/aspirations.jsonl"

# G4: rate limit check
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
    'created_at': timestamp,
}

print(json.dumps(aspiration, ensure_ascii=True))
" "$ASP_ID" "$GOAL_ID" "$TITLE" "$DESCRIPTION" "$PRIORITY" "$CATEGORY" \
  "$ORIGIN" "$REASON" "$TIMESTAMP")

# ── Dry-run or live write ────────────────────────────────────────────────────
if [ "$DRY_RUN" = "true" ]; then
    echo "DRY-RUN: Would append TWO records to $ASP_FILE:"
    echo ""
    echo "[1] Audit record (asp-xw-* — provenance trail; goals[] NOT visible to selector):"
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
echo "$RECORD" >> "$ASP_FILE"

# Layer 1: find next goal sequence number for TARGET_ASPIRATION (inside lock — race-safe)
SECONDARY_GOAL_ID=$(py -3 -c "
import json, sys, re
asp_num = sys.argv[1]
asp_file = sys.argv[2]
max_seq = 0
pat = re.compile(r'^g-' + re.escape(asp_num) + r'-(\d+)\$')
try:
    with open(asp_file, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            m = pat.match(rec.get('id', ''))
            if m:
                sq = int(m.group(1))
                if sq > max_seq:
                    max_seq = sq
except FileNotFoundError:
    pass
print(f'g-{asp_num}-{max_seq + 1}')
" "$_ASP_NUM" "$ASP_FILE")

# Write [2]: standard goal record (g-{asp_num}-{seq} — visible to selector)
py -3 -c "
import json, sys
goal = {
    'id':                    sys.argv[1],
    'title':                 sys.argv[2],
    'description':           sys.argv[3],
    'status':                'pending',
    'priority':              sys.argv[4],
    'category':              sys.argv[5],
    'participants':          ['agent', 'user'],
    'sandbox':               True,
    'injected_by':           sys.argv[6],
    'cross_world_origin':    sys.argv[6],
    'cross_world_reason':    sys.argv[7],
    'cross_world_timestamp': sys.argv[8],
    'cross_world_audit_ref': sys.argv[9],
    'origin_signal':         'user_directive',
    'tags':                  list(dict.fromkeys([sys.argv[5], 'cross-world-signal', 'sandbox', 'xw-injected'])),
    'work_class':            'unclassified',
    'goal_source':           'user',
    'intended_agent':        'either',
    'filed_by_agent':        'omni',
    'created_at':            sys.argv[8],
}
print(json.dumps(goal, ensure_ascii=True))
" "$SECONDARY_GOAL_ID" "$TITLE" "$DESCRIPTION" "$PRIORITY" "$CATEGORY" \
  "$ORIGIN" "$REASON" "$TIMESTAMP" "$ASP_ID" >> "$ASP_FILE"

release_lock "$LOCK_FILE"
trap - EXIT

# Layer 2: post-injection verification — confirm selector-visible goal is on disk
VERIFY=$(py -3 -c "
import json, sys
target = sys.argv[1]
with open(sys.argv[2], 'r', encoding='utf-8') as fh:
    for line in fh:
        try:
            if json.loads(line.strip()).get('id') == target:
                print('ok')
                sys.exit(0)
        except Exception:
            pass
print('not_found')
" "$SECONDARY_GOAL_ID" "$ASP_FILE" 2>/dev/null || echo "error")

if [ "$VERIFY" = "ok" ]; then
    echo "Injected: audit=$ASP_ID | selector-visible=$SECONDARY_GOAL_ID in $TARGET_ASPIRATION"
    echo "G2 sandbox: true | G3 participants: [agent,user] | Layer 2 verified: present"
else
    echo "ERROR: Layer 2 verify FAILED — $SECONDARY_GOAL_ID not confirmed in $ASP_FILE" >&2
    echo "Audit record $ASP_ID was written but selector-visible goal is missing." >&2
    echo "RECOVERY: Re-run injection or manually file goal into $TARGET_ASPIRATION" >&2
    exit 1
fi
