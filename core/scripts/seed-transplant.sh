#!/usr/bin/env bash
# /seed plant <destination> — orchestrate full plant (publish) flow.
# (Implements the renamed '/seed plant' verb, formerly '/seed transplant'.
#  Filename keeps 'transplant' to avoid churning the CLAUDE.md AGENTS_PARENT_DIR
#  audit table + tests. The word 'transplant' now means relocating a LIVING
#  mind WITH its state to another machine — see the /transplant skill.)
#
# Usage:
#   seed-transplant.sh <destination> [--dry-run] [--diff] [--no-backup]
#                       [--force] [--manifest <path>] [--fresh-git] [--commit]
#                       [--no-clean-cruft]
#
# Exits non-zero on failure. See .claude/skills/seed/SKILL.md for the spec.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

DEST=""
DRY_RUN=0
DO_DIFF=0
NO_BACKUP=0
FORCE=0
MANIFEST="$CONFIG_DIR/seed-manifest.yaml"
FRESH_GIT=0
DO_COMMIT=0
NO_CLEAN_CRUFT=0
SKIP_PREFLIGHT_JUSTIFICATION=""

# Parse args
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --diff) DO_DIFF=1 ;;
        --no-backup) NO_BACKUP=1 ;;
        --backup) NO_BACKUP=0 ;;
        --force) FORCE=1 ;;
        --manifest) MANIFEST="$2"; shift ;;
        --fresh-git) FRESH_GIT=1 ;;
        --commit) DO_COMMIT=1 ;;
        --no-clean-cruft) NO_CLEAN_CRUFT=1 ;;
        --clean-cruft) NO_CLEAN_CRUFT=0 ;;
        --skip-preflight)
            SKIP_PREFLIGHT_JUSTIFICATION="$2"
            if [ -z "$SKIP_PREFLIGHT_JUSTIFICATION" ] || [[ "$SKIP_PREFLIGHT_JUSTIFICATION" == --* ]]; then
                echo "ERROR: --skip-preflight requires a justification string" >&2
                echo '  Example: --skip-preflight "emergency hotfix, publishability gates already verified manually"' >&2
                exit 2
            fi
            shift
            ;;
        -*) echo "Unknown flag: $1" >&2; exit 2 ;;
        *) if [ -z "$DEST" ]; then DEST="$1"; else echo "Extra arg: $1" >&2; exit 2; fi ;;
    esac
    shift
done

if [ -z "$DEST" ]; then
    echo "Usage: seed-transplant.sh <destination> [flags]" >&2
    exit 2
fi

# Resolve dest to absolute path (allow it to not exist yet)
if [ -d "$DEST" ]; then
    DEST="$(cd "$DEST" && pwd)"
fi

echo "[seed-transplant] source=$PROJECT_ROOT"
echo "[seed-transplant] dest=$DEST"
echo "[seed-transplant] manifest=$MANIFEST"

# Step 3: Pre-flight checks
echo "[seed-transplant] Pre-flight checks..."

# 3a. Destination exists or can be created
if [ ! -d "$DEST" ]; then
    if [ $FORCE -eq 1 ]; then
        mkdir -p "$DEST"
        DEST="$(cd "$DEST" && pwd)"
        echo "  Created destination: $DEST"
    else
        echo "  Destination does not exist: $DEST"
        echo "  Run with --force to create it, or create the dir first." >&2
        exit 2
    fi
fi

# 3b. Destination git status
if [ -d "$DEST/.git" ]; then
    if [ -n "$(git -C "$DEST" status --porcelain 2>/dev/null)" ]; then
        if [ $FORCE -eq 1 ]; then
            echo "  WARN: destination has uncommitted changes (--force overriding)"
        else
            echo "  REFUSE: destination has uncommitted changes." >&2
            echo "  Commit or stash, or run with --force." >&2
            exit 3
        fi
    fi
fi

# 3c. Destination session state (running agent at dest)
RUNNING_AT_DEST=0
if [ -d "$DEST/agents" ]; then
    for state_file in "$DEST"/agents/*/session/agent-state; do
        if [ -f "$state_file" ] && grep -q "RUNNING" "$state_file" 2>/dev/null; then
            RUNNING_AT_DEST=1
            echo "  REFUSE: agent RUNNING at $state_file" >&2
        fi
    done
fi
if ls "$DEST"/.active-agent-* >/dev/null 2>&1; then
    RUNNING_AT_DEST=1
    echo "  REFUSE: active-agent binding present at destination" >&2
fi
if [ $RUNNING_AT_DEST -eq 1 ]; then
    echo "  /stop the destination agent before planting (NO --force override)." >&2
    exit 4
fi

# 3d. Domain dirs at destination (info-only)
for d in agents world meta; do
    if [ -d "$DEST/$d" ]; then
        echo "  INFO: $DEST/$d/ exists and will NOT be touched."
    fi
done

# 3e. Source cleanliness
if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null)" ]; then
    echo "  WARN: source has uncommitted changes — seed will include uncommitted modifications."
fi

# Step 3.5: Source publishability gate (preflight)
# Six publishability invariants run as a single aggregated check. Any FAIL
# refuses the plant — the bad source state must be fixed first, not
# carried into the destination. The --skip-preflight flag is an emergency
# override that requires a written justification (audited to stderr).
# See world/knowledge/tree/system/publication-pipeline.md for the gate
# inventory and rb-1108/1109/1110 for the lessons that motivated each gate.
if [ -n "$SKIP_PREFLIGHT_JUSTIFICATION" ]; then
    echo "[seed-transplant] Source publishability gate SKIPPED via --skip-preflight" >&2
    echo "  Justification: $SKIP_PREFLIGHT_JUSTIFICATION" >&2
    echo "  Timestamp: $(date -Iseconds)" >&2
    echo "  source=$PROJECT_ROOT  dest=$DEST" >&2
    echo "  (override is a last resort, never routine — preflight protects the public repo from broken-source promotions)" >&2
else
    echo "[seed-transplant] Running source publishability gate..."
    set +e
    bash "$SCRIPT_DIR/seed-preflight.sh" --quiet
    PREFLIGHT_RC=$?
    set -e
    if [ $PREFLIGHT_RC -ne 0 ]; then
        echo "" >&2
        echo "[seed-transplant] REFUSE: source publishability gate FAILED" >&2
        echo "  Re-run \`bash $SCRIPT_DIR/seed-preflight.sh\` for full per-check output." >&2
        echo "  Fix the failing checks at source, then re-attempt promotion." >&2
        echo "  Emergency override: re-run with --skip-preflight \"<justification>\" (audited to stderr)." >&2
        exit 7
    fi
    echo "  PASS: 6/6 publishability checks green"
fi

# Step 4: Build copy plan
# Don't merge stderr into PLAN (would break JSON parse). Capture stderr to a
# tempfile for diagnostic display on failure. set +e around the substitution
# is defensive against bash's varied set -e behavior with command substitution.
echo "[seed-transplant] Building copy plan..."
ERR_LOG="$(mktemp 2>/dev/null || echo "/tmp/seed-transplant-err.$$")"
set +e
PLAN="$(py -3 "$SCRIPT_DIR/_seed_engine.py" build-plan --manifest "$MANIFEST" --source "$PROJECT_ROOT" 2>"$ERR_LOG")"
RC=$?
set -e
if [ $RC -ne 0 ]; then
    echo "[seed-transplant] build-plan failed (rc=$RC):" >&2
    cat "$ERR_LOG" >&2
    rm -f "$ERR_LOG"
    exit 5
fi
rm -f "$ERR_LOG"
N_FILES="$(echo "$PLAN" | py -3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['files']))")"
N_TRANSFORMED="$(echo "$PLAN" | py -3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for f in d['files'] if f['transformations']))")"
N_PENDING="$(echo "$PLAN" | py -3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for f in d['files'] if f['pending_template_skip']))")"

echo "  $N_FILES files to copy"
echo "  $N_TRANSFORMED files with transformations"
if [ "$N_PENDING" -gt 0 ]; then
    echo "  WARN: $N_PENDING files have pending_template — will use inline+global transforms only"
fi

# Step 5: Dry-run exit
if [ $DRY_RUN -eq 1 ]; then
    echo "[seed-transplant] Dry run complete. Re-run without --dry-run to apply."
    if [ $DO_DIFF -eq 1 ] && [ -d "$DEST/.git" -o -d "$DEST/CLAUDE.md" -o -d "$DEST/core" ]; then
        py -3 "$SCRIPT_DIR/_seed_engine.py" diff --manifest "$MANIFEST" --source "$PROJECT_ROOT" --dest "$DEST"
    fi
    exit 0
fi

# Step 6: User confirmation (skip if --force)
if [ $FORCE -eq 0 ]; then
    echo ""
    echo "Proceed with seed-plant to $DEST ? [y/N]"
    read -r ANSWER
    case "$ANSWER" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
fi

# Step 7: Backup
if [ $NO_BACKUP -eq 0 ]; then
    echo "[seed-transplant] Backing up destination framework files..."
    BACKUP_JSON="$(py -3 "$SCRIPT_DIR/_seed_engine.py" backup --manifest "$MANIFEST" --source "$PROJECT_ROOT" --dest "$DEST")"
    BACKUP_DIR="$(echo "$BACKUP_JSON" | py -3 -c "import sys,json; print(json.load(sys.stdin)['backup_dir'])")"
    echo "  Backup: $BACKUP_DIR"
fi

# Step 8: Staged copy
echo "[seed-transplant] Copying + transforming to staging..."
COPY_JSON="$(py -3 "$SCRIPT_DIR/_seed_engine.py" copy-staged --manifest "$MANIFEST" --source "$PROJECT_ROOT" --dest "$DEST")"
echo "$COPY_JSON" | py -3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"  staged: {d['staged']}, transformed: {d['transformed']}, binary: {d['binary']}\")
if d['pending_skip']:
    print(f\"  pending_template (used chain fallback): {len(d['pending_skip'])} files\")"

# Step 9: Atomic swap
echo "[seed-transplant] Swapping staged files into place..."
SWAP_JSON="$(py -3 "$SCRIPT_DIR/_seed_engine.py" swap --dest "$DEST")"
MOVED="$(echo "$SWAP_JSON" | py -3 -c "import sys,json; print(json.load(sys.stdin)['moved'])")"
echo "  moved: $MOVED"
N_FAIL="$(echo "$SWAP_JSON" | py -3 -c "import sys,json; print(len(json.load(sys.stdin)['failures']))")"
if [ "$N_FAIL" -gt 0 ]; then
    echo "  FAILURES during swap:"
    echo "$SWAP_JSON" | py -3 -c "import sys,json; [print('   ', f['rel_path'], ':', f['error']) for f in json.load(sys.stdin)['failures']]"
    exit 6
fi

# Step 10: Clean cruft
if [ $NO_CLEAN_CRUFT -eq 0 ]; then
    echo "[seed-transplant] Cleaning cruft at destination..."
    CRUFT_JSON="$(py -3 "$SCRIPT_DIR/_seed_engine.py" clean-cruft --manifest "$MANIFEST" --dest "$DEST")"
    echo "$CRUFT_JSON" | py -3 -c "
import sys, json
d = json.load(sys.stdin)
if d['removed']:
    print(f\"  removed: {len(d['removed'])}\")
    for r in d['removed'][:10]:
        print(f\"    - {r}\")
    if len(d['removed']) > 10:
        print(f\"    ... and {len(d['removed'])-10} more\")
else:
    print('  no cruft found')"
fi

# Step 10.5: Remove orphans — files at destination that are NOT in the
# manifest-resolved include set (i.e. removed from source since last plant).
# This gives the destination true mirror semantics: source = dev, destination = prod.
echo "[seed-transplant] Removing orphans at destination..."
ORPHAN_JSON="$(py -3 "$SCRIPT_DIR/_seed_engine.py" remove-orphans --manifest "$MANIFEST" --source "$PROJECT_ROOT" --dest "$DEST")"
echo "$ORPHAN_JSON" | py -3 -c "
import sys, json
d = json.load(sys.stdin)
if d['removed']:
    print(f\"  removed: {len(d['removed'])} orphan(s)\")
    for r in d['removed'][:10]:
        print(f\"    - {r}\")
    if len(d['removed']) > 10:
        print(f\"    ... and {len(d['removed'])-10} more\")
else:
    print('  no orphans found')"

# Step 11: Handle git (init only — commit happens AFTER post-actions so
# their outputs land in the same commit as the planted files)
if [ $FRESH_GIT -eq 1 ]; then
    echo "[seed-transplant] Re-initializing git at destination..."
    rm -rf "$DEST/.git"
    git -C "$DEST" init -q
fi

# Step 12: Post-copy actions (MUST run before auto-commit — post-action A1
# regenerates _tree.yaml at destination, and previously its output landed as
# an uncommitted change after the commit had already fired. rb-1109-derived
# fix 2026-05-20.)
echo "[seed-transplant] Running post-copy actions..."
py -3 "$SCRIPT_DIR/_seed_postactions.py" --manifest "$MANIFEST" --dest "$DEST" --source "$PROJECT_ROOT" || true

# Step 12.5: Auto-commit (runs AFTER post-actions so regenerated files are
# captured in the commit)
if [ $DO_COMMIT -eq 1 ] && [ -d "$DEST/.git" ]; then
    echo "[seed-transplant] Committing planted files at destination..."
    git -C "$DEST" add -A
    TS="$(date +%Y-%m-%d)"
    # Generic, public-safe commit message — source path / repo name MUST NOT
    # appear here. The destination is a publication target; commits show on
    # the public GitHub home page.
    git -C "$DEST" commit -m "chore: sync framework ($TS)" -q || echo "  (nothing to commit)"
fi

# Step 13: Verification
echo "[seed-transplant] Running verification..."
bash "$SCRIPT_DIR/seed-verify.sh" "$DEST" || echo "  (verification reported issues — see above)"

echo "[seed-transplant] DONE. Planted $MOVED files to $DEST"
