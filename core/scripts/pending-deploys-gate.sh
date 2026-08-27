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
# THIS CONSTANT DRIVES TWO LIMITS, AND THEIR ORDERING IS LOAD-BEARING (guard-1737).
#   (1) --timeout-mins -> deploy-verify.sh's own polling deadline  = TIMEOUT_MINS*60
#   (2) sp_timeout     -> the hard subprocess kill below           = TIMEOUT_MINS*60+30
# The +30 gap exists so deploy-verify ALWAYS reaches its own timeout first and
# emits its considered {"status":"unverified"} verdict; the kill is only a
# backstop. Invert that ordering and every slow-CI probe reports "invocation
# error" instead, changing the reported CAUSE while nothing goes red. Any change
# here must preserve (2) > (1). Fractional minutes are NOT available: deploy-verify
# computes `deadline=$(( now + TIMEOUT_MINS * 60 ))` in bash integer arithmetic.
#
# WAS 3 (sp_timeout 210s) until g-115-5877. The gate runs INSIDE iteration-close,
# which runs inside a foreground Bash tool call bounded at 120s, so 210 > 120 made
# every close with a pending deploy DETERMINISTICALLY killed at the bound (exit
# 143) — and killed HALF-APPLIED: lastAchievedAt advanced, claim released,
# loop_state counters bumped and the commit created, while outcome_note still held
# the PREVIOUS run's text and no health row was written. Every cheap signal reads
# "closed", so a retry double-counts. Measured twice in one iteration (echo, cc-03,
# 2026-08-11). 1 => 60s poll + 90s kill, which fits with headroom.
#
# Shortening the per-close CI wait does not weaken the obligation: an unresolved
# entry is KEPT and re-probed at the next close (rc=2 path below). Verification is
# spread across closes rather than blocking one close for three minutes — which is
# what a pending-deploys TRACKER is for.
TIMEOUT_MINS=1
# Empty = resolve per deployment at FILING time (g-115-4166). A literal asp-115
# is the UPSTREAM deployment's queue and exists in no other deployment, so
# downstream every Unblock filed here failed aspiration_not_found — silently,
# because the filing call below routes its errors to /dev/null and degrades to
# the SG-c backstop. An explicit --aspiration/--source still wins.
ASPIRATION=""
SOURCE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --agent)        AGENT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --goal)         GOAL="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --timeout-mins) TIMEOUT_MINS="${2:-3}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --aspiration)   ASPIRATION="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --source)       SOURCE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
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
    # budget_skipped is carried HERE TOO (g-115-5877), not only on the main path.
    # A key present on one exit and absent on another is shape drift: a consumer
    # that reads it unconditionally KeyErrors on whichever path it forgot about,
    # and this is the path taken in the overwhelmingly common case.
    echo '{"checked":0,"cleared":0,"failed":0,"unverified":0,"unblocks_filed":0,"budget_skipped":0,"not_clean":false}'
    exit 0
fi

# ── Enumerate matching entries as repo<US>sha<US>dir<US>goal rows ───────────
# US (0x1f), NOT tab. `dir` is OPTIONAL (empty for a repo-root deploy) and sits
# in the MIDDLE, and tab is an IFS *whitespace* character — so `IFS=$'\t' read`
# collapses the two adjacent tabs and shifts goal_id left into _dir. Measured on
# cc-02 2026-08-14 against the live store, which held two such entries:
#   printf 'REPO\tSHA\t\tGOALID\n' | IFS=$'\t' read -r a b c d  -> c=GOALID d=
#   printf 'REPO\x1fSHA\x1f\x1fGOALID\n' | IFS=$'\x1f' read ... -> c=      d=GOALID
# Consequence chain of the tab form: _dir receives the goal-id, so the `-n "$_dir"`
# branch below calls `resolve --dir g-335-328` against a nonexistent directory,
# which returns rc=2 UNVERIFIED, so the entry is KEPT and re-probed at EVERY close,
# forever, while flagging not_clean=1 (SG-c then holds graceful stop). An entry with
# an empty dir could therefore NEVER be verified. The `(goal=${_gid:-?})` rendering
# in every message below was the visible symptom, and it reads as an ORPHANED entry
# — which is how it was first diagnosed, wrongly. Same defect and same US remedy as
# core/scripts/mutation-partition-proof.sh:79 and the two expansion-parsed sites in
# iteration-close.sh (394, 1705); this was the site that missed that sweep.
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
    print("\x1f".join(str(v).replace("\x1f", " ").replace("\n", " ") for v in
                      [e.get("repo", ""), e.get("sha", ""),
                       e.get("dir", ""), e.get("goal_id", "")]))
' 2>/dev/null || true)"

# ── File a HIGH Unblock for a FAILED deploy (dedup by repo@sha7) ─────────────
_file_deploy_unblock() {
    local repo="$1" sha="$2" gid="$3" verdict="$4"
    local sha7="${sha:0:7}"
    local osig="unblock:pending-deploy-${sha7}"
    # Dedup: if a live (pending|in-progress|blocked) OR completed Unblock already
    # names this repo@sha7, do not re-file. A COMPLETED Unblock for this EXACT
    # origin_signal means the (immutable) sha's failure was already captured and
    # addressed -- re-filing is pure noise (the g-115-2897 storm: 8 duplicate
    # bddb90c Unblocks). Suppressing the duplicate FILING does not drop the
    # failure: the ledger entry persists and keeps the closure not-clean (SG-b) +
    # holds graceful stop (SG-c), so an unresolved deploy stays surfaced through
    # those orthogonal mechanisms rather than through spammy re-files. Only
    # skipped/expired do NOT block a re-file (a re-failed re-push legitimately
    # needs a fresh Unblock).
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
    if isinstance(g, dict) and g.get("status") in ("pending", "in-progress", "blocked", "completed"):
        sys.exit(0)   # live-or-completed dup exists -> suppress re-file
sys.exit(1)
' 2>/dev/null; then
        echo "[pending-deploys-gate] Unblock for ${repo}@${sha7} already queued — not re-filing" >&2
        return 2   # deduped (distinct from 0=filed / 1=file-not-confirmed) so the caller does NOT count it
    fi
    local title desc _et
    # g-115-4166: resolve here rather than at script top — this function runs
    # ONLY on a failed deploy, so the common clean-gate path (which returns at
    # the no-pending-entries fast exit above) never pays for the subprocess.
    if [ -z "$ASPIRATION" ] || [ -z "$SOURCE" ]; then
        _et="$(bash "$SCRIPT_DIR/escalation-target.sh")" || _et="asp-115 world"
        [ -n "$ASPIRATION" ] || ASPIRATION="${_et%% *}"
        [ -n "$SOURCE" ] || SOURCE="${_et##* }"
    fi
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

checked=0 cleared=0 failed=0 unverified=0 unblocks=0 not_clean=0 budget_skipped=0
sp_timeout=$(( TIMEOUT_MINS * 60 + 30 ))   # hard subprocess kill, past deploy-verify's own timeout

# ── TOTAL-LOOP BUDGET (g-115-5877) ──────────────────────────────────────────
# Per-entry bounding is NOT sufficient and that was the subtler half of the bug.
# This loop runs `resolve` ONCE PER ENTRY, each with its own sp_timeout, so the
# worst case is N * sp_timeout — a quantity with no upper bound at all, since N is
# whatever the tracker has accumulated. Fixing only the per-entry constant leaves
# two pending deploys blowing the same 120s foreground bound.
#
# The budget is spent on WHOLE entries, never by clamping sp_timeout down to the
# remaining time: a clamped kill would fire BEFORE deploy-verify's own deadline and
# invert the ordering the constant block above exists to protect (guard-1737).
# deploy-verify takes integer minutes only, so the two limits cannot be scaled
# together anyway. An entry therefore either runs with its ordering-correct pair or
# is deferred untouched.
#
# A DEFERRED ENTRY IS NOT A LOST ONE. It takes the SAME path an in-progress CI run
# takes (unverified): the entry is KEPT, the closure is flagged not-clean, and the
# next close re-probes it. No new state, no new failure mode — deferral reuses the
# existing fail-safe rather than inventing a second one.
#
# The FIRST entry always runs regardless of budget. Otherwise an explicit
# --timeout-mins larger than the budget would silently verify NOTHING, ever, which
# is a worse failure than overrunning once: it looks exactly like a clean gate.
: "${MIND_PD_GATE_BUDGET_SECS:=100}"
budget_secs="$MIND_PD_GATE_BUDGET_SECS"
loop_start=$(date +%s 2>/dev/null || echo 0)
if [ -n "$rows" ]; then
    # IFS=$'\x1f' (US), NOT $'\t' — see the emitter comment above line 114. Tab is
    # IFS whitespace, so an empty middle `dir` collapses and shifts goal_id into
    # _dir, permanently un-verifying the entry. US is not whitespace; empty fields
    # survive. If you change this delimiter, change the emitter in the SAME edit.
    while IFS=$'\x1f' read -r _repo _sha _dir _gid; do
        [ -n "$_repo" ] && [ -n "$_sha" ] || continue
        # Budget check BEFORE the probe, and never for the first entry.
        if [ "$checked" -gt 0 ] && [ "$loop_start" != 0 ]; then
            _now=$(date +%s 2>/dev/null || echo 0)
            _elapsed=$(( _now - loop_start ))
            if [ $(( _elapsed + sp_timeout )) -gt "$budget_secs" ]; then
                budget_skipped=$(( budget_skipped + 1 )); not_clean=1
                echo "[pending-deploys-gate] BUDGET: deferring ${_repo}@${_sha:0:7} (goal=${_gid:-?}) — ${_elapsed}s spent, next probe needs ${sp_timeout}s, budget ${budget_secs}s. Entry KEPT and re-probed at the next close; closure not-clean." >&2
                continue
            fi
        fi
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
    # SG-c does NOT hold the stop, and this line used to say it did (g-335-1313).
    # stop-hook.sh:487-504 is a roll-then-ALLOW backstop in the not-RUNNING gate:
    # it calls `pending-deploys.py roll-handoff` for VISIBILITY and then exits 0.
    # Its own comment is explicit — "NEVER blocks the stop (an un-clearable
    # framework-CI obligation must not wedge a session)" — and boot/SKILL.md 4d
    # calls the carry-over an "awareness surface only". No gate anywhere blocks
    # on pending deploys (Gate 2.5 is pending-agents, Gate 2.6 background-jobs).
    # The false wording cost a real goal: a reader saw it in the stderr log,
    # believed a /stop was wedged, and filed MEDIUM work whose stated reason to
    # act did not exist. A message asserting a consequence in ANOTHER component
    # is a claim about that component's code, and nothing keeps the two in sync
    # (guard-4282, same class inverted — here the prose went stale, not the value).
    # Describe only what THIS gate did; name the visibility path, not a hold.
    echo "[pending-deploys-gate] one or more deploys unresolved — clean-success closure refused; entries kept and re-probed at the next close, and rolled into handoff.yaml at session end for the next boot to surface (SG-c is visibility-only and never blocks a stop)" >&2
fi

if [ "$budget_skipped" -gt 0 ]; then
    echo "[pending-deploys-gate] BUDGET: ${budget_skipped} entr(ies) deferred to the next close to stay inside the ${budget_secs}s loop budget — none were dropped" >&2
fi

nc="false"; [ "$not_clean" -eq 1 ] && nc="true"
# budget_skipped is a NEW key, deliberately additive: existing consumers read the
# six original keys positionally-by-name and are unaffected. It is reported on
# STDOUT rather than stderr alone because this gate's stderr is redirected into a
# log by both iteration-close seams, and stderr from a nested process inside a
# backgrounded Bash call is not captured at all (guard-772) — so a deferral that
# existed only as a warning would be invisible exactly when it matters.
# NOTE: a deferred entry is NOT counted in `checked`. It was not checked.
printf '{"checked":%d,"cleared":%d,"failed":%d,"unverified":%d,"unblocks_filed":%d,"budget_skipped":%d,"not_clean":%s}\n' \
    "$checked" "$cleared" "$failed" "$unverified" "$unblocks" "$budget_skipped" "$nc"
exit 0
