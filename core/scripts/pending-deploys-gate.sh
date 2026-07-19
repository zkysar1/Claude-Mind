#!/usr/bin/env bash
# domain-leak-exempt: orchestrates pending-deploys.py + deploy-verify.sh (guard-119 CI probe); the "GitHub Actions" reference names the CI system the gate verifies, functional not illustrative.
# pending-deploys-gate.sh — ENFORCE half (SG-b, g-115-2688-b) of the
# pending-deploys hard gate (g-115-2688).
#
# CONTEXT. The user directive is: an agent must not move on until the GitHub
# Actions deploy it triggered is verified. SG-a (CAPTURE, g-115-2688-a) wired a
# PostToolUse[Bash] hook (deploy-detect-hook.sh) that records a {repo,sha,
# goal_id,dir} obligation into agents/<agent>/session/pending-deploys.yaml on
# every git-push to a CI repo while a goal is in-flight. THIS script is the
# ENFORCE half: it refuses CLEAN-SUCCESS goal closure while an obligation is
# unresolved. SG-c (STOP+HARDEN, g-115-2688-c) will add the stop-hook Gate 2.5
# that refuses graceful stop while obligations remain.
#
# INVOKED FROM iteration-close.sh at two seams:
#   1. do_verify (per-goal, at completed closure):  --goal <id>
#   2. do_productivity_check (all-sweep, once/iter): no --goal  (re-probes the
#      entries an earlier closure left unverified once CI has since concluded)
#
# MECHANISM. For each matching pending entry, run `pending-deploys.py resolve`
# (which wraps deploy-verify.sh — the canonical guard-119 CI probe — and mirrors
# its exit code) with a BOUNDED timeout so the loop never blocks on a slow CI:
#   rc 0 (ok / no_ci)   -> resolve already cleared the entry; deploy verified.
#   rc 1 (failed)       -> CI concluded FAILED. File a HIGH Unblock (dedup by
#                          repo@sha) so the failure surfaces as actionable queue
#                          work, keep the entry (SG-c backstop + dedup makes the
#                          re-probe idempotent), and flag the closure not-clean.
#   rc 2 (unverified)   -> CI not concluded / gh unavailable / API error. KEEP
#                          the entry (a later sweep or SG-c re-probes) and flag
#                          not-clean. No Unblock — an in-progress run is normal,
#                          not a failure (rb-611 three-way verdict discipline).
#
# FAIL-OPEN EVERYWHERE (guard-141 family): every probe guarded, every error ->
# warn + continue, script ALWAYS exits 0. A gate failure must NEVER abort
# iteration close. The has-pending fast-exit makes the overwhelmingly common
# case (no pending deploys) a single cheap python call.
#
# OUTPUT. Human diagnostics -> stderr. A single machine-readable summary JSON ->
# stdout (consumed by tests; callers may ignore or log it):
#   {"checked":N,"cleared":C,"failed":F,"unverified":U,"unblocks_filed":X,"not_clean":true|false}

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || { echo '{"checked":0,"error":"scriptdir"}'; exit 0; }
# _paths.sh: python3 shim on PATH + PROJECT_ROOT/AGENTS_PARENT_DIR/agent_dir().
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || { echo '{"checked":0,"error":"paths"}'; exit 0; }

# ── Args ────────────────────────────────────────────────────────────────────
AGENT="${MIND_AGENT:-}"
GOAL=""
TIMEOUT_MINS=3
ASPIRATION="asp-115"          # world framework aspiration — home for infra Unblocks (asp-001 retired)
SOURCE="world"
while [ $# -gt 0 ]; do
    case "$1" in
        --agent)        AGENT="${2:-}"; shift 2 ;;
        --goal)         GOAL="${2:-}"; shift 2 ;;
        --timeout-mins) TIMEOUT_MINS="${2:-3}"; shift 2 ;;
        --aspiration)   ASPIRATION="${2:-asp-115}"; shift 2 ;;
        --source)       SOURCE="${2:-world}"; shift 2 ;;
        *) shift ;;   # tolerate unknown args (fail-open posture)
    esac
done
[ -n "$AGENT" ] || { echo '{"checked":0,"error":"no-agent"}'; exit 0; }

PD="$SCRIPT_DIR/pending-deploys.py"
[ -f "$PD" ] || { echo '{"checked":0,"error":"no-capture-layer"}'; exit 0; }   # SG-a absent -> nothing to gate

# ── Fast exit: no pending entries (the common case) ─────────────────────────
hp=(has-pending)
[ -n "$GOAL" ] && hp+=(--goal-id "$GOAL")
if ! python3 "$PD" --agent "$AGENT" "${hp[@]}" >/dev/null 2>&1; then
    echo '{"checked":0,"cleared":0,"failed":0,"unverified":0,"unblocks_filed":0,"not_clean":false}'
    exit 0
fi

# ── Enumerate matching entries as repo<TAB>sha<TAB>dir<TAB>goal rows ─────────
ls=(list --json)
[ -n "$GOAL" ] && ls+=(--goal-id "$GOAL")
entries_json="$(python3 "$PD" --agent "$AGENT" "${ls[@]}" 2>/dev/null || echo '[]')"
rows="$(printf '%s' "$entries_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = []
for e in (data or []):
    if not isinstance(e, dict):
        continue
    print("\t".join([str(e.get("repo", "")), str(e.get("sha", "")),
                     str(e.get("dir", "")), str(e.get("goal_id", ""))]))
' 2>/dev/null || true)"

# ── File a HIGH Unblock for a FAILED deploy (dedup by repo@sha7) ─────────────
_file_deploy_unblock() {
    local repo="$1" sha="$2" gid="$3" verdict="$4"
    local sha7="${sha:0:7}"
    local osig="unblock:pending-deploy-${sha7}"
    # Dedup: if a live (pending|in-progress|blocked) Unblock already names this
    # repo@sha7, do not re-file. resolved/skipped ones do NOT block a re-file
    # (a re-failed re-push legitimately needs a fresh Unblock).
    local dup
    dup="$(MIND_AGENT="$AGENT" bash "$SCRIPT_DIR/aspirations-query.sh" --goal-field origin_signal "$osig" 2>/dev/null || echo '')"
    if printf '%s' "$dup" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    d = json.loads(raw) if raw else []
except Exception:
    sys.exit(1)   # unparseable -> treat as no-dup, allow filing
rows = d if isinstance(d, list) else (d.get("results") or d.get("goals") or [])
for g in (rows or []):
    if isinstance(g, dict) and g.get("status") in ("pending", "in-progress", "blocked"):
        sys.exit(0)   # live dup exists
sys.exit(1)
' 2>/dev/null; then
        echo "[pending-deploys-gate] Unblock for ${repo}@${sha7} already queued — not re-filing" >&2
        return 2   # deduped (distinct from 0=filed / 1=file-not-confirmed) so the caller does NOT count it
    fi
    local title desc
    title="Unblock: fix failed deploy ${repo}@${sha7} for ${gid:-unknown-goal}"
    desc="deploy-verify.sh reported a FAILED GitHub Actions run for ${repo} at ${sha} (pushed during ${gid:-a goal}). The pending-deploys ENFORCE gate (SG-b, g-115-2688-b) downgraded that goal's closure to not-clean. Investigate the failing run, fix it, re-push, and re-verify via deploy-verify.sh. Verdict JSON: ${verdict}"
    if TITLE="$title" DESC="$desc" OSIG="$osig" GID="${gid:-pending-deploys-gate}" python3 -c '
import json, os
print(json.dumps({
    "title":          os.environ["TITLE"],
    "description":    os.environ["DESC"],
    "status":         "pending",
    "priority":       "HIGH",
    "category":       "deployment",
    "participants":   ["agent"],
    "origin_signal":  os.environ["OSIG"],
    "discovered_by":  os.environ["GID"],
    "discovery_type": "deploy_failure",
}))
' 2>/dev/null | MIND_AGENT="$AGENT" bash "$SCRIPT_DIR/aspirations-add-goal.sh" "$ASPIRATION" --source "$SOURCE" >/dev/null 2>&1; then
        echo "[pending-deploys-gate] filed HIGH Unblock (${osig}) for ${repo}@${sha7}" >&2
        return 0
    fi
    echo "[pending-deploys-gate] WARN: Unblock filing for ${repo}@${sha7} did not confirm — pending entry kept as SG-c backstop" >&2
    return 1
}

checked=0 cleared=0 failed=0 unverified=0 unblocks=0 not_clean=0
sp_timeout=$(( TIMEOUT_MINS * 60 + 30 ))   # hard subprocess kill, past deploy-verify's own timeout
if [ -n "$rows" ]; then
    while IFS=$'\t' read -r _repo _sha _dir _gid; do
        [ -n "$_repo" ] && [ -n "$_sha" ] || continue
        checked=$(( checked + 1 ))
        rv=""
        # errexit is intentionally NOT enabled in this script (top is `set -uo
        # pipefail`, no -e): a resolve non-zero rc IS the signal, and the
        # arithmetic increments below yield exit 1 whenever a counter is still 0.
        if [ -n "$_dir" ]; then
            rv="$(python3 "$PD" --agent "$AGENT" resolve --repo "$_repo" --sha "$_sha" --dir "$_dir" --timeout-mins "$TIMEOUT_MINS" --subprocess-timeout "$sp_timeout" 2>/dev/null)"
        else
            rv="$(python3 "$PD" --agent "$AGENT" resolve --repo "$_repo" --sha "$_sha" --timeout-mins "$TIMEOUT_MINS" --subprocess-timeout "$sp_timeout" 2>/dev/null)"
        fi
        rc=$?
        if [ "$rc" -eq 0 ]; then
            cleared=$(( cleared + 1 ))
            echo "[pending-deploys-gate] verified ${_repo}@${_sha:0:7} (goal=${_gid:-?}) — obligation cleared" >&2
        elif [ "$rc" -eq 1 ]; then
            failed=$(( failed + 1 )); not_clean=1
            echo "[pending-deploys-gate] DEPLOY FAILED ${_repo}@${_sha:0:7} (goal=${_gid:-?}) — closure not-clean" >&2
            if _file_deploy_unblock "$_repo" "$_sha" "$_gid" "$rv"; then
                unblocks=$(( unblocks + 1 ))
            fi
        else
            unverified=$(( unverified + 1 )); not_clean=1
            echo "[pending-deploys-gate] UNVERIFIED ${_repo}@${_sha:0:7} (goal=${_gid:-?}) rc=$rc — entry kept for re-probe, closure not-clean" >&2
        fi
    done <<< "$rows"
fi

if [ "$not_clean" -eq 1 ]; then
    echo "[pending-deploys-gate] one or more deploys unresolved — clean-success closure refused; SG-c stop-hook will hold graceful stop until cleared" >&2
fi

nc="false"; [ "$not_clean" -eq 1 ] && nc="true"
printf '{"checked":%d,"cleared":%d,"failed":%d,"unverified":%d,"unblocks_filed":%d,"not_clean":%s}\n' \
    "$checked" "$cleared" "$failed" "$unverified" "$unblocks" "$nc"
exit 0
