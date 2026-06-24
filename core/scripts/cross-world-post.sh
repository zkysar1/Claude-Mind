#!/usr/bin/env bash
# domain-leak-exempt: functional cross-world path map; WORLD_MAP below holds literal
# sibling-world directory names required for board cross-post routing - operational
# values, not pedagogical examples.
# Cross-world influence: post a provenance-stamped message to a sibling
# Mind world's board, or inject a sandboxed aspiration/goal.
#
# Enforces guardrails G1-G5 (guard-64..68):
#   G1 (guard-64) default-Vault: requires explicit --shared flag
#   G2 (guard-65) sandboxing: only board/aspiration writes, never arbitrary edits
#   G3 (guard-66) first-influence approval: caller responsibility (script warns)
#   G4 (guard-67) rate-limit: max 20 posts per hour per target
#   G5 (guard-68) provenance: every record stamped with origin, timestamp, reason
#
# Usage:
#   Board post (message on stdin):
#     echo "message" | bash core/scripts/cross-world-post.sh \
#       --target ayoai --channel coordination --reason "why" --shared [--type status] [--tags t1,t2]
#
#   Goal injection (dry-run prints JSONL to stdout; live appends):
#     bash core/scripts/cross-world-post.sh \
#       --target ayoai --inject-goal '<json>' --reason "why" --shared [--dry-run]
#
#   Help:
#     bash core/scripts/cross-world-post.sh --help
#
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# ── Name-to-path map for known sibling worlds ──────────────────────────
# Add entries here as new sibling worlds become reachable.
declare -A WORLD_MAP
WORLD_MAP[ayoai]="C:/ZakNoCloud/AyoaiCache/Ayoai-World"

# ── Constants ──────────────────────────────────────────────────────────
ORIGIN="omni@zds-mind"
RATE_LIMIT_CAP=20       # G4: max posts per hour per target

# ── Helpers ────────────────────────────────────────────────────────────
usage() {
    cat <<'USAGE'
cross-world-post.sh — Post to a sibling Mind world (board or aspiration injection)

MODES:
  Board post (message on stdin):
    echo "msg" | bash core/scripts/cross-world-post.sh \
      --target <name> --channel <ch> --reason "<why>" --shared [--type <t>] [--tags <t1,t2>]

  Goal injection:
    bash core/scripts/cross-world-post.sh \
      --target <name> --inject-goal '<goal-json>' --reason "<why>" --shared [--dry-run]

FLAGS:
  --target <name>       Target world name (required). Known: ayoai
  --channel <name>      Board channel to post to (required for board mode)
  --reason <text>       Why this cross-world influence is happening (required; G5 provenance)
  --shared              Explicitly mark this artifact as Shared (required; G1 default-Vault)
  --type <type>         Message type for board posts (default: status)
  --tags <t1,t2>        Comma-separated tags
  --inject-goal <json>  Goal JSON to inject into target aspirations.jsonl
  --dry-run             Print what would be written without writing (goal injection only)
  --help                Show this help

GUARDRAILS:
  G1 (guard-64): Refuses without --shared (default-Vault)
  G2 (guard-65): Only board and aspiration writes; no arbitrary file edits
  G4 (guard-67): Rate-limited to 20 posts/hour per target
  G5 (guard-68): Every record stamped with origin, timestamp, reason
USAGE
    exit 0
}

die() { echo "ERROR: $*" >&2; exit 1; }

resolve_target() {
    local name="$1"
    local path="${WORLD_MAP[$name]:-}"
    [ -z "$path" ] && die "Unknown target world '$name'. Known targets: ${!WORLD_MAP[*]}"
    [ -d "$path" ] && return 0
    die "Target world directory does not exist: $path"
}

# G4: count today's posts from this origin in the target world's board dir.
# Returns the count of records authored by ORIGIN in the last hour.
check_rate_limit() {
    local target_dir="$1"
    local board_dir="$target_dir/board"
    [ -d "$board_dir" ] || return 0

    local one_hour_ago
    one_hour_ago="$(date -d '1 hour ago' +%Y-%m-%dT%H:%M:%S 2>/dev/null || date +%Y-%m-%dT%H:%M:%S)"

    # Count records from our origin across all board channels in the last hour.
    # Use a simple grep + python count for robustness.
    local count
    count=$(py -3 -c "
import json, sys, os, glob
from datetime import datetime, timedelta

origin = '$ORIGIN'
now = datetime.now()
one_hour_ago = now - timedelta(hours=1)
count = 0
board_dir = sys.argv[1]
for f in glob.glob(os.path.join(board_dir, '*.jsonl')):
    if f.endswith('-reads.jsonl'):
        continue
    try:
        with open(f, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('author') != origin:
                    continue
                ts = rec.get('timestamp', '')
                try:
                    dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S')
                    if dt >= one_hour_ago:
                        count += 1
                except ValueError:
                    pass
    except Exception:
        pass
print(count)
" "$board_dir" 2>/dev/null || echo 0)

    if [ "$count" -ge "$RATE_LIMIT_CAP" ]; then
        die "G4 rate-limit exceeded: $count posts from $ORIGIN in the last hour to '$target_dir' (cap: $RATE_LIMIT_CAP). Wait before posting again."
    fi
}

# ── Argument parsing ──────────────────────────────────────────────────
TARGET=""
CHANNEL=""
REASON=""
SHARED=false
MSG_TYPE="status"
TAGS=""
INJECT_GOAL=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)    TARGET="$2"; shift 2 ;;
        --channel)   CHANNEL="$2"; shift 2 ;;
        --reason)    REASON="$2"; shift 2 ;;
        --shared)    SHARED=true; shift ;;
        --type)      MSG_TYPE="$2"; shift 2 ;;
        --tags)      TAGS="$2"; shift 2 ;;
        --inject-goal) INJECT_GOAL="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=true; shift ;;
        --help|-h)   usage ;;
        *)           die "Unknown argument: $1. Use --help for usage." ;;
    esac
done

# ── Validation ─────────────────────────────────────────────────────────
[ -z "$TARGET" ] && die "Missing --target. Use --help for usage."
[ -z "$REASON" ] && die "Missing --reason (G5 provenance requires a reason)."

# G1: default-Vault — refuse unless explicitly Shared
if [ "$SHARED" != "true" ]; then
    die "G1 (guard-64): Cross-world influence default is VAULT. Pass --shared to explicitly mark this artifact as Shared."
fi

# Determine mode
if [ -n "$INJECT_GOAL" ] && [ -n "$CHANNEL" ]; then
    die "Cannot use both --channel and --inject-goal. Pick one mode."
fi
if [ -z "$INJECT_GOAL" ] && [ -z "$CHANNEL" ]; then
    die "Must specify either --channel <name> (board post) or --inject-goal '<json>' (goal injection)."
fi

# Resolve target
resolve_target "$TARGET"
TARGET_DIR="${WORLD_MAP[$TARGET]}"

# G4: rate-limit check
check_rate_limit "$TARGET_DIR"

TIMESTAMP="$(date +%Y-%m-%dT%H:%M:%S)"

# ── MODE: Board post ──────────────────────────────────────────────────
if [ -n "$CHANNEL" ]; then
    BOARD_DIR="$TARGET_DIR/board"
    CH_FILE="$BOARD_DIR/$CHANNEL.jsonl"

    # Ensure board dir exists
    mkdir -p "$BOARD_DIR"

    # Read message from stdin
    MESSAGE="$(cat)"
    [ -z "$MESSAGE" ] && die "No message text provided on stdin."

    # Generate message ID matching sibling schema:
    # msg-YYYYMMDD-HHMMSS-author-NNN
    LINE_COUNT=0
    if [ -f "$CH_FILE" ]; then
        LINE_COUNT=$(wc -l < "$CH_FILE" | tr -d ' ')
    fi
    NEXT_NUM=$((LINE_COUNT + 1))
    MSG_ID="msg-$(date +%Y%m%d-%H%M%S)-${ORIGIN}-$(printf '%04d' $NEXT_NUM)"

    # Build tags array
    TAGS_JSON="[]"
    if [ -n "$TAGS" ]; then
        TAGS_JSON=$(py -3 -c "import json; print(json.dumps([t.strip() for t in '$TAGS'.split(',')]))")
    fi

    # Build the record matching the sibling world's board schema:
    # {id, author, session_id, timestamp, channel, type, text, reply_to, tags}
    # G5: provenance fields embedded in the record
    RECORD=$(py -3 -c "
import json, sys
rec = {
    'id': sys.argv[1],
    'author': sys.argv[2],
    'session_id': '',
    'timestamp': sys.argv[3],
    'channel': sys.argv[4],
    'type': sys.argv[5],
    'text': sys.argv[6],
    'reply_to': None,
    'tags': json.loads(sys.argv[7]),
    'cross_world_origin': sys.argv[8],
    'cross_world_reason': sys.argv[9]
}
print(json.dumps(rec, ensure_ascii=True))
" "$MSG_ID" "$ORIGIN" "$TIMESTAMP" "$CHANNEL" "$MSG_TYPE" "$MESSAGE" "$TAGS_JSON" "$ORIGIN" "$REASON")

    # File-locked append to the target channel JSONL.
    # Use a simple lockfile approach since _fileops is for this world only.
    LOCK_FILE="$CH_FILE.lock"
    _acquire_lock() {
        local lockf="$1"
        local attempts=0
        while ! ( set -o noclobber; echo $$ > "$lockf" ) 2>/dev/null; do
            attempts=$((attempts + 1))
            if [ $attempts -gt 50 ]; then
                die "Could not acquire lock on $lockf after 50 attempts"
            fi
            sleep 0.1
        done
    }
    _release_lock() { rm -f "$1"; }

    _acquire_lock "$LOCK_FILE"
    # shellcheck disable=SC2064
    trap "_release_lock '$LOCK_FILE'" EXIT
    echo "$RECORD" >> "$CH_FILE"
    _release_lock "$LOCK_FILE"
    trap - EXIT

    echo "$MSG_ID"
    exit 0
fi

# ── MODE: Goal injection ─────────────────────────────────────────────
if [ -n "$INJECT_GOAL" ]; then
    ASP_FILE="$TARGET_DIR/aspirations.jsonl"

    if [ ! -f "$ASP_FILE" ]; then
        die "Target aspirations file does not exist: $ASP_FILE"
    fi

    # Validate the provided JSON and stamp provenance
    STAMPED=$(py -3 -c "
import json, sys

raw = sys.argv[1]
try:
    goal = json.loads(raw)
except json.JSONDecodeError as e:
    print(f'Invalid JSON: {e}', file=sys.stderr)
    sys.exit(1)

# Stamp G5 provenance
goal['cross_world_origin'] = sys.argv[2]
goal['cross_world_reason'] = sys.argv[3]
goal['cross_world_timestamp'] = sys.argv[4]

# G2 sandboxing: we only write to aspirations.jsonl — the goal record
# itself is a data payload the sibling world's agents choose to act on.

print(json.dumps(goal, ensure_ascii=True))
" "$INJECT_GOAL" "$ORIGIN" "$REASON" "$TIMESTAMP")

    if [ $? -ne 0 ]; then
        die "Failed to process goal JSON."
    fi

    if [ "$DRY_RUN" = "true" ]; then
        echo "DRY-RUN: Would append to $ASP_FILE:"
        echo "$STAMPED" | py -3 -c "import json,sys; print(json.dumps(json.loads(sys.stdin.read()),indent=2))"
        exit 0
    fi

    # File-locked append
    LOCK_FILE="$ASP_FILE.lock"
    _acquire_lock_goal() {
        local lockf="$1"
        local attempts=0
        while ! ( set -o noclobber; echo $$ > "$lockf" ) 2>/dev/null; do
            attempts=$((attempts + 1))
            if [ $attempts -gt 50 ]; then
                die "Could not acquire lock on $lockf after 50 attempts"
            fi
            sleep 0.1
        done
    }
    _release_lock_goal() { rm -f "$1"; }

    _acquire_lock_goal "$LOCK_FILE"
    trap "_release_lock_goal '$LOCK_FILE'" EXIT
    echo "$STAMPED" >> "$ASP_FILE"
    _release_lock_goal "$LOCK_FILE"
    trap - EXIT

    echo "Goal injected into $ASP_FILE"
    exit 0
fi
