#!/usr/bin/env bash
# orphan-root-sweep.sh — Advisory detector for cruft directories at
# wrong roots produced by external-path-resolution drift (Modes A, B, and D,
# see world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md).
# Mode D scan added 2026-05-17 (g-115-756) — single source of truth for
# Mode D shape detection: core/scripts/_orphan_root_helpers.py.
# domain-leak-exempt: this script literally uses Ayoai-* directory naming
# variants as detection data — the strings are the script's contract, not
# accidental domain bleed (g-115-432, 2026-05-08).
#
# Scans three locations and warns about entries that should not exist there:
#   1. <world-parent>/  (and <meta-parent>/ if different) — Mode B skeleton
#      dirs from `mkdir -p` running at the wrong base. Allowed entries are
#      basename(WORLD_DIR) and basename(META_DIR); anything else is suspect.
#   2. <dirname PROJECT_ROOT>/  — Mode A duplicates. Walks each sibling of
#      PROJECT_ROOT one level deep and flags any subdir whose basename
#      matches WORLD_DIR/META_DIR/known-cruft naming (Ayoai-World, Ayoai-Meta,
#      My-Meta, knowledge — the four shapes catalogued in the 2026-05-08 audit).
#   3. PROJECT_ROOT/  — Mode D cruft. Walks top-level entries directly under
#      PROJECT_ROOT and flags any whose name contains U+F03A (NTFS-remapped
#      colon) OR matches drive-letter-segment shape (e.g., `C`, `C:`, `D`).
#      Catches stale-daemon residue (rb-939). Triage REQUIRES daemon restart
#      first (guard-554) — do NOT delete cruft before confirming the running
#      daemon's `/v1/admin/health` git_head_sha matches on-disk HEAD.
#
# Advisory only — never deletes. Exit 0 always (does not block the loop on
# findings; surfaces to stdout/journal for human triage). g-115-431.
#
# Cadence: weekly (matches the ~week sliding window the 2026-05-08 audit
# observed cruft lingering before discovery).

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_paths.sh"

# ─── Args ────────────────────────────────────────────────────────────────
# --auto-clean: when Mode D findings are present AND daemon /v1/admin/health
# reports git_head_sha matching the on-disk HEAD, remove the cruft dirs.
# Per rb-939 + guard-554: cleanup is safe ONLY after daemon has been
# restarted with the latest path-resolver fix. If sha mismatch (daemon
# stale) — skip and instruct restart. Mode-D-only by design; other modes
# (A/B/E/F) need different triage and stay advisory.
AUTO_CLEAN=false
for arg in "$@"; do
    case "$arg" in
        --auto-clean) AUTO_CLEAN=true ;;
    esac
done

world_basename="$(basename "$WORLD_DIR")"
meta_basename="$(basename "$META_DIR")"
world_parent="$(dirname "$WORLD_DIR")"
meta_parent="$(dirname "$META_DIR")"
project_root_parent="$(dirname "$PROJECT_ROOT")"
project_root_basename="$(basename "$PROJECT_ROOT")"

findings=0
findings_text=""
declare -a mode_d_paths=()

emit() {
    findings_text+="$1"$'\n'
    findings=$((findings + 1))
}

mtime_of() {
    stat -c %y "$1" 2>/dev/null | cut -d. -f1 || echo "?"
}

# ─── Scan 1: world-parent ───────────────────────────────────────────────
# Mode B detection. Allowed entries: world_basename, meta_basename.
# Anything else (especially agent-name-shaped: alpha, bravo, Ayoai-Alpha,
# Ayoai-Bravo) is suspect.
echo "=== world-parent: $world_parent ==="
if [ -d "$world_parent" ]; then
    for entry in "$world_parent"/*; do
        [ -e "$entry" ] || continue
        name="$(basename "$entry")"
        # Allowed at world-parent: WORLD_DIR basename, META_DIR basename
        # (the latter is allowed only when world-parent and meta-parent share
        # the same path — which is the typical config but not enforced).
        if [ "$name" = "$world_basename" ]; then continue; fi
        if [ "$world_parent" = "$meta_parent" ] && [ "$name" = "$meta_basename" ]; then
            continue
        fi
        # Allowed: upgrade-snapshots/ is the canonical pre-upgrade rollback
        # backup store — _template.sh SNAP_DIR default is
        # "$WORLD_PATH/../upgrade-snapshots/" (= world-parent). A legitimate
        # version-snapshot store, NOT cruft; the location-only heuristic
        # over-flagged it every weekly run (g-115-451 bravo FALSE POSITIVE;
        # rb-1585 preserved this fix; g-115-1525 applied it).
        if [ "$name" = "upgrade-snapshots" ]; then continue; fi
        kind="file"
        [ -d "$entry" ] && kind="dir"
        mtime="$(mtime_of "$entry")"
        emit "  ORPHAN: $name ($kind, mtime: $mtime)"
    done
else
    echo "  (world-parent does not exist — skipping)"
fi

# ─── Scan 2: meta-parent (if different from world-parent) ───────────────
if [ "$world_parent" != "$meta_parent" ]; then
    echo "=== meta-parent: $meta_parent ==="
    if [ -d "$meta_parent" ]; then
        for entry in "$meta_parent"/*; do
            [ -e "$entry" ] || continue
            name="$(basename "$entry")"
            if [ "$name" = "$meta_basename" ]; then continue; fi
            kind="file"
            [ -d "$entry" ] && kind="dir"
            mtime="$(mtime_of "$entry")"
            emit "  ORPHAN: $name ($kind, mtime: $mtime)"
        done
    else
        echo "  (meta-parent does not exist — skipping)"
    fi
else
    echo "=== meta-parent: same as world-parent (skipped) ==="
fi

# ─── Scan 3: project-root-parent ────────────────────────────────────────
# Mode A detection. Walks each sibling of PROJECT_ROOT one level deep,
# flags subdirs whose name matches WORLD_DIR/META_DIR basenames or the
# known stale-naming variants (Ayoai-World, Ayoai-Meta, My-Meta — the
# three basename shapes the 2026-05-08 audit catalogued at the wrong
# root). Excludes basename(PROJECT_ROOT) itself.
#
# Generic basenames like `knowledge` are NOT in the list — too many
# unrelated sibling projects (other AI/research repos) legitimately have
# a `knowledge/` subdir, producing false positives. The cruft signature
# is the world/meta NAMING pattern at the wrong location, not arbitrary
# subdir names.
echo "=== project-root-parent: $project_root_parent ==="
# Dedup the cruft basename list — when WORLD_DIR/META_DIR basenames already
# include the canonical-naming shapes (e.g., $world_basename == "Ayoai-World"),
# the static list adds duplicates that would emit the same finding twice.
declare -A seen_cruft=()
cruft_basenames=()
for c in "$world_basename" "$meta_basename" "Ayoai-World" "Ayoai-Meta" "My-Meta"; do
    [ -z "$c" ] && continue
    if [ -z "${seen_cruft[$c]:-}" ]; then
        cruft_basenames+=("$c")
        seen_cruft[$c]=1
    fi
done
unset seen_cruft

if [ -d "$project_root_parent" ]; then
    for sibling in "$project_root_parent"/*; do
        [ -d "$sibling" ] || continue
        sib_name="$(basename "$sibling")"
        # Skip the project root itself.
        [ "$sib_name" = "$project_root_basename" ] && continue
        # Look one level inside this sibling for cruft basenames.
        for cruft in "${cruft_basenames[@]}"; do
            target="$sibling/$cruft"
            if [ -e "$target" ]; then
                mtime="$(mtime_of "$target")"
                emit "  ORPHAN: $sib_name/$cruft (mtime: $mtime)"
            fi
        done
    done
else
    echo "  (project-root-parent does not exist — skipping)"
fi

# ─── Scan 4: PROJECT_ROOT (Mode D) ──────────────────────────────────────
# g-115-756 (2026-05-17): Mode D cruft at PROJECT_ROOT — stale-daemon
# residue where a long-running daemon caches pre-fix path resolver code
# and writes drive-letter-shaped or NTFS-remapped-colon directories
# (e.g. `C\357\200\272/` in git status). Detection delegated to the
# Python helper `_orphan_root_helpers.is_mode_d_cruft` so the bash
# sweep and a Python unit test share the same predicate logic. The
# helper is the single source of truth — DO NOT inline the regex here.
# Triage: rb-939 (three-probe daemon-staleness diagnosis) +
# guard-554 (mandatory daemon restart before declaring fix done).
# Advisory only — never deletes; salvage may be needed first.
echo "=== project-root: $PROJECT_ROOT (Mode D scan) ==="
if [ -d "$PROJECT_ROOT" ]; then
    # Collect top-level cruft entries as newline-delimited stream.
    #
    # CRITICAL — two pitfalls this form avoids (both caught by
    # test_orphan_root_sweep_mode_d_integration.py, g-115-876, fixing the
    # original broken g-115-756 form):
    #
    # 1. Heredoc displaces stdin: `find ... | py -3 - <<'PY'...PY` discards
    #    the piped find output (the heredoc becomes py's source-stdin), so
    #    sys.stdin.buffer.read() returns b''. Scan 4 silently no-ops on
    #    ALL inputs every run. Fix: python lists the dir itself via
    #    os.listdir — no pipe, no heredoc-stdin conflict.
    #
    # 2. Command substitution strips NULs: `var="$(cmd-emitting-\0)"` drops
    #    every \0 byte (bash limitation, warns once per call). Fix: emit
    #    newline-delimited names. Cruft shapes (`C`, `C:`, `C` + U+F03A,
    #    `D`, etc.) don't contain newlines, and U+F03A's UTF-8 bytes
    #    (ef 80 ba) don't include 0x0a — newlines survive intact.
    #
    # Form matches guard-165: env-var values + single-quoted python
    # source — no shell interpolation into source text.
    cruft_stream="$(SCRIPT_DIR="$SCRIPT_DIR" PROJECT_ROOT="$PROJECT_ROOT" py -3 -c '
import os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
from _orphan_root_helpers import is_mode_d_cruft
proj_root = os.environ["PROJECT_ROOT"]
for name in sorted(os.listdir(proj_root)):
    if is_mode_d_cruft(name):
        print(name)
' 2>/dev/null)"
    if [ -n "$cruft_stream" ]; then
        while IFS= read -r bad_name; do
            [ -z "$bad_name" ] && continue
            entry="$PROJECT_ROOT/$bad_name"
            kind="file"
            [ -d "$entry" ] && kind="dir"
            mtime="$(mtime_of "$entry")"
            # Hex-dump name bytes so log readers can see the U+F03A byte
            # sequence even when terminals render it invisibly.
            name_hex="$(printf '%s' "$bad_name" | od -An -tx1 | tr -d ' \n')"
            emit "  MODE-D ORPHAN: $bad_name ($kind, mtime: $mtime, name-bytes: $name_hex)"
            mode_d_paths+=("$entry")
        done <<< "$cruft_stream"
    fi
else
    echo "  (PROJECT_ROOT does not exist — should be unreachable)"
fi

# ─── Scan 5: PROJECT_ROOT Mode E — meta/world at repo root ──────────────
# 2026-05-19 (plan v1 step 0.11, Lane 1 RC-A/B): both `meta/` and `world/`
# are supposed to live at external paths configured in local-paths.conf
# (typically a OneDrive/NAS path). Their appearance at PROJECT_ROOT is the
# signature of `_paths.{py,sh}` falling back to PROJECT_ROOT/{meta,world}
# when local-paths.conf is missing OR MIND_AGENT resolves to a nonexistent
# agent. Phase 0.1 removes the fallback to refuse the write — this sweep
# is the periodic backstop for stragglers + any subprocess that still
# evades the resolver fix.
#
# High-confidence cruft: presence alone is the finding (no allowlist needed).
echo "=== project-root: $PROJECT_ROOT (Mode E scan — repo-root meta/world) ==="
for bad in "$PROJECT_ROOT/meta" "$PROJECT_ROOT/world"; do
    [ -e "$bad" ] || continue
    bad_basename="$(basename "$bad")"
    # If WORLD_DIR or META_DIR happens to RESOLVE to PROJECT_ROOT/meta or /world
    # (i.e., the user really did configure local paths to repo-root), this is
    # not cruft — skip. The user-configured case is rare but valid.
    if [ "$WORLD_DIR" = "$bad" ] || [ "$META_DIR" = "$bad" ]; then
        continue
    fi
    mtime="$(mtime_of "$bad")"
    emit "  MODE-E ORPHAN: $bad_basename/ (mtime: $mtime) — canonical lives at \$WORLD_DIR/\$META_DIR external path"
done

# ─── Scan 6: PROJECT_ROOT Mode F — known LLM-scratch / test-fixture leftovers ──
# 2026-05-19 (plan v1 step 0.11, Lane 1 RC-C/J): catches the SPECIFIC named
# offenders observed in the 2026-05-19 cleanup audit. Narrower than a full
# allowlist scan to keep FP rate at zero; a future broader allowlist sweep
# can be layered on top once the L1 hook extension (Phase 0.9) lands.
echo "=== project-root: $PROJECT_ROOT (Mode F scan — known scratch leftovers) ==="
for bad_pattern in ".migration-tmp" ".runtime-tmp" "g115873test"; do
    bad="$PROJECT_ROOT/$bad_pattern"
    [ -e "$bad" ] || continue
    kind="file"
    [ -d "$bad" ] && kind="dir"
    mtime="$(mtime_of "$bad")"
    emit "  MODE-F ORPHAN: $bad_pattern ($kind, mtime: $mtime) — LLM-scratch or test-fixture leftover (Phase 1 cleanup target)"
done

# ─── Summary ────────────────────────────────────────────────────────────
echo ""
echo "=== summary ==="
if [ "$findings" -eq 0 ]; then
    echo "orphan-root-sweep: 0 findings — clean"
else
    echo "orphan-root-sweep: $findings finding(s) — advisory only, no auto-delete"
    printf '%s' "$findings_text"
    echo ""
    echo "Triage: per world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md,"
    echo "Mode A duplicates require manual rm + diff against canonical \$WORLD_DIR/\$META_DIR contents."
    echo "Mode B skeleton dirs (empty) under world-parent can be safely removed once \`find -type f\` confirms emptiness."
    echo "Mode D residue (drive-letter / U+F03A names at PROJECT_ROOT): FIRST follow rb-939 three-probe diagnosis"
    echo "  (cruft mtime + git log fix-commit time + daemon start time) AND guard-554 (curl /v1/admin/health,"
    echo "  confirm git_head_sha matches \`git rev-parse HEAD\`, kill+respawn via \`Stop-Process -Id \$(cat mind_api/state/daemon.pid) -Force\`"
    echo "  on Windows or \`kill \$(cat mind_api/state/daemon.pid)\` on POSIX) BEFORE removing the cruft directory."
fi

# ─── Auto-clean (Mode D only, opt-in, daemon-health-gated) ──────────────
# Implements guard-554 / rb-939 cleanup protocol:
#   1. Daemon /v1/admin/health must be reachable
#   2. Returned git_head_sha must match `git rev-parse HEAD`
#   3. Only then is it safe to delete (cruft is stale residue, not active)
# If either condition fails, refuse the delete and explain.
if [ "$AUTO_CLEAN" = "true" ] && [ "${#mode_d_paths[@]}" -gt 0 ]; then
    echo ""
    echo "=== auto-clean (Mode D) ==="
    daemon_sha=""
    daemon_url="${MIND_DAEMON_URL:-http://localhost:8765}/v1/admin/health"
    if command -v curl >/dev/null 2>&1; then
        # Quiet curl; if reachable, extract git_head_sha via py -3 (jq absent on Windows git-bash).
        daemon_response="$(curl -sf --max-time 3 "$daemon_url" 2>/dev/null || echo "")"
        if [ -n "$daemon_response" ]; then
            daemon_sha="$(printf '%s' "$daemon_response" | py -3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("git_head_sha") or "")
except Exception:
    print("")' 2>/dev/null || echo "")"
        fi
    fi
    repo_sha="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
    cleanup_log="$PROJECT_ROOT/core/logs/orphan-root-sweep-cleanups.log"

    if [ -z "$daemon_sha" ]; then
        echo "  SKIP: daemon health unreachable at $daemon_url"
        echo "        — start the daemon or set MIND_DAEMON_URL, then re-run."
    elif [ -z "$repo_sha" ]; then
        echo "  SKIP: git rev-parse HEAD failed (not in a git work tree?)"
    elif [ "$daemon_sha" != "$repo_sha" ]; then
        echo "  SKIP: daemon git_head_sha=$daemon_sha != repo HEAD=$repo_sha"
        echo "        Daemon is running stale code. Restart it first (per guard-554),"
        echo "        then re-run with --auto-clean. Removing cruft now would just let"
        echo "        the stale daemon re-create it."
    else
        echo "  OK: daemon git_head_sha matches HEAD ($repo_sha) — daemon has the fix."
        echo "  Removing ${#mode_d_paths[@]} Mode-D cruft path(s):"
        for cruft in "${mode_d_paths[@]}"; do
            if rm -rf "$cruft" 2>/dev/null; then
                echo "    REMOVED: $cruft"
                printf '%s orphan-root-sweep auto-clean: removed %s\n' \
                    "$(date +%Y-%m-%dT%H:%M:%S)" "$cruft" >> "$cleanup_log"
            else
                echo "    REMOVE FAILED: $cruft (manual cleanup needed)"
            fi
        done
    fi
fi

# Always exit 0 — advisory only.
exit 0
