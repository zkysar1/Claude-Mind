#!/usr/bin/env bash
# domain-leak-exempt: WORLD_ALIAS below holds literal sibling-world environment-ids
# required for board cross-post routing - operational values, not pedagogical examples.
# (Was "WORLD_MAP holds literal ... directory names" until 2026-07-31; the hardcoded
# directory map is gone - paths now resolve per-machine. Only the env-id aliases remain.)
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
# ── PREFER core/scripts/peer-board-post.sh FOR BOARD POSTS ─────────────
# For plain board posts to a peer, use `peer-board-post.sh`: it resolves the
# target from the `core/config/environments/*.yaml` registry and pins the PEER's
# storage backend before writing. As of 2026-07-31 THIS script resolves its target
# from the SAME sources (env var / registry `peer_world_path:`) rather than the
# hand-maintained literal it used to carry — that literal was a Windows path and
# resolved on no Linux box (g-115-4191). The REMAINING reason to prefer
# peer-board-post.sh is unchanged and is the important one: this script still
# inherits the CALLER's storage backend, which is the guard-955 / rb-2983 hazard
# when the two deployments differ — as ayoai-mind (own-cloud) and zds-mind
# (local) do, deliberately and by user directive.
#
# What is NOT yet available elsewhere: `--inject-goal`. Until an equivalent
# exists, this script remains the only aspiration/goal injection path, which is
# why it is fixed in place rather than retired.
#
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# ── Target-world resolution (PER-MACHINE — never a hardcoded literal) ──
# A sibling world's directory exists on SOME machines and not others, so a
# literal absolute path is correct on at most ONE box and dead on every other.
# Until 2026-07-31 this held a single Windows path and died before writing on
# every Linux box. Resolution reuses the ESTABLISHED convention rather than a
# second one — core/config/conventions/cross-deployment-channel.md:
#   1. $PEER_WORLD_<ENV_ID>  — env-id upper-cased, hyphens → underscores
#   2. peer_world_path:      — in core/config/environments/<env-id>.yaml
# NO default and NO fallback: an absent path is diagnosable, a WRONG one writes
# into the wrong world (guard-955 / rb-2983). Unreachable exits 3, matching
# peer-board-post.sh. Kept byte-identical to the block in
# cross-world-inject-goal.sh on purpose — these two drifted once already
# (its ORIGIN fix landed here 2026-07-30 and not there, guard-2078).
declare -A WORLD_ALIAS
WORLD_ALIAS[ayoai]="ayoai-mind"
WORLD_ALIAS[claude]="claude-mind"
WORLD_ALIAS[zds]="zds-mind"
RESOLVED_WORLD_DIR=""

# ── Origin identity (G5 provenance) ────────────────────────────────────
# DERIVED from ENVIRONMENT_ID — never hardcoded. This file is promoted
# ayoai-mind -> claude-mind -> zds-mind, so ANY literal origin is correct in at
# most one tier and silently forges a peer identity in every other: until
# 2026-07-30 this read `omni@zds-mind`, so running it from ayoai-mind stamped
# both the author field (L234) and the message id (L207) as zds-mind's agent.
# That is not a cosmetic mislabel — the receiving world has no other way to
# attribute a post, and `cross-deployment-channel.md` documents that 87% of
# real inbound traffic is already unattributable by author alone.
#
# DIE rather than default: a post with a WRONG provenance stamp is worse than
# no post, and G5 makes provenance mandatory (guard-68).
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
    echo "cross-world-post.sh: cannot resolve MIND_AGENT — refusing to post" >&2
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
    echo "cross-world-post.sh: cannot resolve ENVIRONMENT_ID — refusing to post" >&2
    echo "  G5 (guard-68) requires a provenance stamp; an unstamped or guessed" >&2
    echo "  origin would misattribute this post to another deployment." >&2
    echo "  Set ENVIRONMENT_ID in .env.local, or use core/scripts/peer-board-post.sh," >&2
    echo "  which resolves the peer from core/config/environments/*.yaml." >&2
    exit 2
fi
# `<agent>@<env-id>` — '@' not '-': every registry env-id contains a hyphen, so
# the hyphen form cannot be split back into (agent, env). See
# core/config/conventions/cross-deployment-channel.md.
ORIGIN="${ORIGIN_AGENT}@${ORIGIN_ENV}"
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
  --target <name>       Target world (required). Alias or env-id: ayoai|claude|zds,
                        or any env-id under core/config/environments/ directly.
                        The DIRECTORY is resolved per-machine, never hardcoded:
                        $PEER_WORLD_<ENV_ID>, else peer_world_path: in the
                        registry entry. Exit 3 = not hosted on this box (normal).
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
    # guard-165: pass ORIGIN through the ENVIRONMENT, never interpolate it into
    # the Python source text. This block already used the correct pattern for
    # board_dir (argv) while interpolating origin one line up. Newly relevant as
    # of 2026-07-30: ORIGIN used to be a hardcoded literal (guaranteed
    # quote-free); it is now derived from MIND_AGENT + ENVIRONMENT_ID, i.e. from
    # environment input. No injection path is known — both inputs are controlled
    # — so this is latent-correctness, not a live break.
    count=$(CWP_ORIGIN="$ORIGIN" py -3 -c "
import json, sys, os, glob
from datetime import datetime, timedelta

origin = os.environ['CWP_ORIGIN']
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
        --target)    TARGET="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --channel)   CHANNEL="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --reason)    REASON="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --shared)    SHARED=true; shift ;;
        --type)      MSG_TYPE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --tags)      TAGS="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --inject-goal) INJECT_GOAL="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
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
TARGET_DIR="$RESOLVED_WORLD_DIR"

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
