#!/usr/bin/env bash
# reducer-self-fence.sh — the AUTHORIZED stop-signal writer for a superseded reducer.
#
# The DDB runner claim is a LEASE. A lease needs T_stepdown < T_takeover: the
# holder must stop acting as leader BEFORE a peer may legally seize the claim, or
# both act as reducer at once. Until , T_stepdown was effectively
# INFINITY — the holder never checked whether it still held. Measured 2026-08-05:
# cc-04 lost its claim at 14:38 and kept executing goals as reducer for 2.5+ hours
# while two other bodies acquired it. Split-brain was avoided by luck.
#
# AUTHORIZATION: `.claude/rules/stop-hook-compliance.md` rule 2 names this script
# as the second authorized caller of `session-signal-set.sh stop-requested`
# outside /stop (productivity-stop-gate.sh is the first). INVOKED ONLY by
# heartbeat-tick.sh, inside its existing STORAGE_BACKEND=own-cloud branch. The
# LLM MUST NOT invoke this directly.
#
# The DECISION is script-gated in reducer_self_fence.py::decide (pure, fully
# branch-tested) — not LLM-discretionary — so the threshold math cannot be
# bypassed. This wrapper owns only the WRITE.
#
# FAIL-OPEN EVERYWHERE. A fence that cannot decide must never stop a healthy
# loop, and must never block an iteration: every failure path here exits 0
# without writing. That direction is the opposite of the sibling worker poll and
# is deliberate (see the module docstring).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT="${MIND_AGENT:-}"
[ -z "$AGENT" ] && exit 0   # not a bound runner context

# Idempotent: a stop is already in progress, so there is nothing to add and
# re-writing stop-target-mode could race /stop's own write.
if bash "$SCRIPT_DIR/session-signal-exists.sh" stop-requested 2>/dev/null; then
    exit 0
fi

# Resolve the session dir FIRST so every bail-out below can leave evidence.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

# F-001 () — the `|| true` above is load-bearing for fail-open and also
# permits _paths.sh to fail SILENTLY, leaving agent_dir UNDEFINED. `$(agent_dir
# ...)` then expands to EMPTY and SESSION_DIR becomes the filesystem-root path
# "/session". Measured on cc-02 and independently on cc-08 (both Linux): under
# root with a writable /, the `mkdir -p "$SESSION_DIR"` further down SUCCEEDS.
# The stand-down would then write stop-target-mode into /session while
# session-signal-set.sh correctly sets stop-requested in the REAL session dir —
# leaving the signal SET with no target mode where /stop Phase -1.4 reads it
# (no fallback, by design). That is exactly the split the ORDER-CRITICAL block
# below exists to prevent, defeated two lines above it.
#
# These two bail-outs deliberately do NOT call _undecided: it is not defined yet,
# and more importantly it writes its marker INTO SESSION_DIR, which is precisely
# the value that cannot be trusted here. So this branch is stderr-only and writes
# NOTHING. That knowingly forfeits guard-772's durable marker in this one branch —
# writing evidence to an unvalidated filesystem path is the worse trade.
_PR="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd)"
if [ "$(type -t agent_dir 2>/dev/null)" != "function" ]; then
    echo "[reducer-self-fence] UNDECIDABLE: _paths.sh did not define agent_dir — refusing to resolve a session dir. This box is NOT self-fenced; a lost claim would go undetected here." >&2
    exit 0
fi
SESSION_DIR="$(agent_dir "$AGENT" 2>/dev/null)/session"
if [ -z "$_PR" ] || { [ "$SESSION_DIR" != "$_PR" ] && [ "${SESSION_DIR#"$_PR"/}" = "$SESSION_DIR" ]; }; then
    echo "[reducer-self-fence] UNDECIDABLE: resolved session dir '$SESSION_DIR' is outside PROJECT_ROOT ('$_PR') — refusing to write. This box is NOT self-fenced; a lost claim would go undetected here." >&2
    exit 0
fi
UNDECIDED_MARK="$SESSION_DIR/reducer-fence-undecided"

# guard-2753, measured 2026-08-05 on THIS subsystem: a fail-open that swallows a
# lease-renewal error cannot tell a transient hiccup from a permanent ownership
# refusal, and the wedge INVERTS the lock's protection — it reports protection it
# is not providing. This fence's OWN inability to decide is the same failure one
# level up: silently exiting 0 forever would leave a box unfenced with nothing
# saying so. So every cannot-decide path leaves a durable marker (guard-772:
# stderr alone is invisible inside a backgrounded tick), and a successful
# decision clears it — so the marker always means "CURRENTLY undecidable", never
# "was once".
_undecided() {
    [ -d "$SESSION_DIR" ] && printf 'at=%s\nreason=%s\n' \
        "$(date +%Y-%m-%dT%H:%M:%S)" "$1" > "$UNDECIDED_MARK" 2>/dev/null || true
    echo "[reducer-self-fence] UNDECIDABLE: $1 — this box is NOT self-fenced; a lost claim would go undetected here." >&2
    exit 0
}
_decided() { rm -f "$UNDECIDED_MARK" 2>/dev/null || true; }

# rt_python_launcher is the SSOT launcher ("py -3" on Windows). Resolve it the
# same way runner-claim.sh does rather than hardcoding an interpreter.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_runtime.sh" 2>/dev/null || _undecided "_runtime.sh unsourceable"
PYLAUNCH="$(rt_python_launcher 2>/dev/null || true)"
[ -z "$PYLAUNCH" ] && _undecided "no python launcher"

# UNQUOTED so it word-splits ("py -3" is two tokens).
VERDICT_JSON="$($PYLAUNCH "$SCRIPT_DIR/reducer_self_fence.py" 2>/dev/null)" || true
[ -z "$VERDICT_JSON" ] && _undecided "decision module produced no output"

# Parse with the launcher too — no jq dependency, and the payload comes from a
# script we just ran, so it is trusted JSON. Passed via env, never interpolated
# into the python source (guard-165).
FENCE_FIELDS="$(VERDICT_JSON="$VERDICT_JSON" $PYLAUNCH - <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    d = json.loads(os.environ["VERDICT_JSON"])
except Exception:
    sys.exit(1)
print(d.get("verdict", ""))
print(d.get("trigger", ""))
print((d.get("reason", "") or "").replace("\n", " ")[:300])
PYEOF
)" || _undecided "verdict JSON unparseable"

VERDICT="$(printf '%s\n' "$FENCE_FIELDS" | sed -n '1p')"
TRIGGER="$(printf '%s\n' "$FENCE_FIELDS" | sed -n '2p')"
REASON="$(printf '%s\n' "$FENCE_FIELDS" | sed -n '3p')"

# A verdict was reached — including every HOLD trigger, which is a real decision
# and not an inability to make one. The two exceptions are the module's OWN
# undecidable states, which must keep the marker set rather than clear it.
case "$TRIGGER" in
    holder-unreadable|body-role-unreadable|config-unreadable)
        _undecided "decision module could not discriminate: $TRIGGER" ;;
    *) _decided ;;
esac

[ "$VERDICT" != "stand-down" ] && exit 0

mkdir -p "$SESSION_DIR" 2>/dev/null || _undecided "session dir not creatable"

# guard-772: a stderr-only warning is INVISIBLE when the tick runs inside a
# backgrounded Bash call, which is the normal case. The durable marker is the
# primary signal; stderr is the convenience copy. Written BEFORE the stop so the
# evidence survives even if the signal write fails.
printf 'fenced_at=%s\ntrigger=%s\nreason=%s\n' \
    "$(date +%Y-%m-%dT%H:%M:%S)" "$TRIGGER" "$REASON" \
    > "$SESSION_DIR/reducer-self-fenced" 2>/dev/null || true

# ORDER CRITICAL — /stop Phase -1.4 reads stop-target-mode with no fallback, so
# the file MUST exist before stop-requested is set. Do NOT reorder these two
# writes. Same invariant as /stop step 1 and productivity-stop-gate.sh L512-518.
printf 'assistant' > "$SESSION_DIR/stop-target-mode" 2>/dev/null || exit 0

if ! bash "$SCRIPT_DIR/session-signal-set.sh" stop-requested 2>/dev/null; then
    # Leave no dangling stop-target-mode: the file must exist ONLY while a stop
    # is actually in progress, or the next reader sees a stop that is not
    # happening. Same revert-on-failure as productivity-stop-gate.sh L537-548.
    rm -f "$SESSION_DIR/stop-target-mode" 2>/dev/null || true
    echo "[reducer-self-fence] WARN: stand-down decided ($TRIGGER) but session-signal-set failed; reverted stop-target-mode. Loop continues as reducer — SPLIT-BRAIN RISK." >&2
    exit 0
fi

# : the stand-down is now committed — this box stops being the reducer
# and only the user-only /start can rejoin it. guard-772 (cited above for the
# durable marker) applies with full force to the USER-facing half too: this tick
# normally runs inside a backgrounded Bash call, so every echo below is invisible
# to a human. The durable marker tells a later reader; this tells the user NOW.
# stderr intentionally un-redirected (guard-3737); -f not -x (guard-1124).
if [ -f "$SCRIPT_DIR/stop-reason-record.py" ]; then
    python3 "$SCRIPT_DIR/stop-reason-record.py" \
        --path reducer-self-fence --agent "$AGENT" \
        --reason "reducer lease stand-down ($TRIGGER): $REASON" \
        || echo "[reducer-self-fence] WARN: stop-reason recorder exited non-zero; stand-down may be unannounced." >&2
else
    echo "[reducer-self-fence] WARN: stop-reason-record.py missing — standing down with nobody told." >&2
fi

echo "[reducer-self-fence] ═══ STANDING DOWN AS REDUCER ═══" >&2
echo "[reducer-self-fence] trigger=$TRIGGER" >&2
echo "[reducer-self-fence] $REASON" >&2
echo "[reducer-self-fence] Graceful stop requested (target mode: assistant). The loop completes" >&2
echo "[reducer-self-fence] its in-flight obligations, then stops. Re-issue /start here to rejoin:" >&2
echo "[reducer-self-fence] a bare /start auto-derives the WORKER role while a peer holds the claim." >&2
echo "[reducer-self-fence] marker: $SESSION_DIR/reducer-self-fenced" >&2
exit 0
