#!/usr/bin/env bash
# Composite goal-queue store: scheduled orphan-collection tick ( U15a).
#
# composite_gc_runner.py is the caller U14 built for the delete pass, and nothing
# scheduled it. This wrapper is the schedule: iteration-close.sh productivity-check
# calls it beside aspirations-evict-tick.sh, and it starts ONE runner process in
# the background at most once per `interval_minutes` on this box.
#
# THE TICK OBSERVES, ALWAYS. It never passes --apply and has no config key that
# could. The runner deletes only when it is given --apply AND the backend's own
# flag names the environment, and this file supplies neither. Arming deletion is a
# reviewed edit to the `run_pass` function below plus that environment flag, in
# that order (flip checklist: core/config/rationale/aspirations-store-segmentation.md).
# While both composite flags are unset the runner makes no store call and prints
# verdict "inactive", so the log line this tick writes each interval is the proof
# the wiring fires BEFORE anything is switched on.
#
# One caller is enough, by design. A pass is fleet-single-flight (a lease and a
# cadence stamp in the object store), so N callers cost N small reads and one pass.
# That is the opposite of history-vacuum-tick.sh, whose store is machine-local and
# so needed a call site a worker box reaches (). A call site in the
# reducer-only productivity-check is sufficient here and is not that defect.
#
# Same LOCAL-tick contract as the siblings (history-vacuum-tick.sh,
# aspirations-evict-tick.sh): a machine-local stamp gates the cadence, a per-box
# lock dir (a stale one is reclaimed after 2h) stops overlap, the run is
# BACKGROUNDED so iteration close never waits on it, and everything is fail-open
# (a missed tick is recovered by the next iteration).
#
# The stamp is a SPAWN throttle for this box and nothing more. It records that a
# pass was started and returned, never that anything was collected (rb-9456): the
# cadence authority is the runner's own fleet-wide stamp in the object store, and
# the evidence of effect is the verdict line this tick logs.
#
# ROUTING (U16). The runner never posts: it prints one result line whose `post` field holds the
# human line for a delete or an anomaly. This wrapper is the caller that owns the channel and the
# de-duplication. After a pass it reads the last line of the runner's output that opens a JSON object
# (stderr is merged into the output, so a warning printed after the summary is skipped, U19) and
#   - posts the runner's `post` to the coordination board (an anomaly as an `escalation`, a delete
#     as a `finding`), then records the condition's key in a per-box state file;
#   - posts nothing more for the same condition for 24 h. The key is the verdict plus the stable
#     lead phrase of each anomaly (digits blanked, cut at the first colon or parenthesis), never a
#     timestamp, a count or an exception text: a key that changes on every pass re-posts a
#     persistent condition on every pass (rb-2954);
#   - ends the episode only on a completed clean pass (verdict observed or applied, no anomaly,
#     rc 0), so a recurrence posts again. A not-due, lease-held, inactive or busy pass says nothing
#     about whether the condition went away, so it leaves the state alone;
#   - treats a runner that left no parseable result line, or exited non-zero without a `post`, as
#     an anomaly too: a runner that dies before it can describe itself is the failure the post
#     exists for, and it would otherwise reach only this log;
#   - reads exit 124, or 137 once the bound's own time has passed, as the runner having been stopped by the
#     wall-clock bound below (THE BOUND): an anomaly of its own class, so a stop keeps its own key and is not
#     read as a crash. A bare 137 sooner than that is something else (the OOM killer) and is not blamed on it;
#   - records the key only after the poster returned rc 0 AND a message id (guard-5382, guard-5403),
#     so a failed post is retried at the next pass and never remembered as sent.
# The post announces; it does not own the work, and nothing is filed. The state file is per box, so
# a reducer that moves boxes repeats a post at most once per episode per move. The routing runs
# after the stamp and the lock have been settled, so a slow or failing poster cannot hold either.
#
# THE BOUND (U17). The runner never renews its lease (composite_gc_runner.LEASE_TTL_S, 1800 s): after that
# long a second caller may take the lock from a pass that is still running. So the runner runs under
# `timeout -k`, with a bound BELOW the lease: RUNNER_MAX_S (1500 s, a choice) leaves 300 s before the lease
# can be taken, 270 s after the KILL that follows the TERM by KILL_GRACE_S. That is the room for the
# start-up that precedes the lease's own clock (0.24 to 0.27 s over three runs) and for clock skew between
# boxes, since the lease's expiry is written with the holder's clock and compared with the contender's.
# One S3 operation's own worst case is 3 x (10 + 30) = 120 s (OwnCloudBackend), so a stalled call ends the
# pass with a failed verdict of the runner's own (error, state-unreadable, ledger-unreadable) long before
# this fires; what the bound stops is a pass made of many slow calls, or one that hangs. A stopped runner
# cannot print its summary (guard-3378), so what it finished is unknown and the post says so. A test pins
# RUNNER_MAX_S plus the kill grace below the lease. The two seams are read as given: a 0 disables the bound
# (GNU timeout reads a zero duration as no limit) and a value past the lease defeats it, so they are for tests.
#
# Config: core/config/aspirations.yaml § composite_gc_tick
#   enabled: true|false   interval_minutes: 30
# Fail-SAFE: only a literal boolean true enables. Any other value, a missing
# block or an unreadable file resolves to disabled, and a probe that FAILED says
# so on stderr (the call site's sink) instead of reading as a quiet "off".
#
# Log: core/logs/composite-gc-tick.log. Per pass: one header (time, the runner's
# exit code, the bound agent, the seconds the pass took) and the last 5 lines of the runner's output, whose
# last line that opens a JSON object is its one-line JSON summary.
#
# Test seams (tests only): COMPOSITE_GC_TICK_RUNNER, COMPOSITE_GC_TICK_CONFIG,
# COMPOSITE_GC_TICK_STATE_DIR (holds the log, the stamp, the lock dir and the post state),
# COMPOSITE_GC_TICK_POSTER (stands in for board-post.sh), COMPOSITE_GC_TICK_POST_TIMEOUT_S (a hung
# poster is killed after this many seconds, default 120), COMPOSITE_GC_TICK_RUNNER_MAX_S (the runner's wall-clock bound, default 1500),
# COMPOSITE_GC_TICK_KILL_GRACE_S (the KILL that follows the bound's TERM, default 30) and
# COMPOSITE_GC_TICK_SYNC (run inline instead of backgrounded).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

RUNNER="${COMPOSITE_GC_TICK_RUNNER:-$SCRIPT_DIR/composite_gc_runner.py}"
CONFIG="${COMPOSITE_GC_TICK_CONFIG:-$PROJECT_ROOT/core/config/aspirations.yaml}"
STATE_DIR="${COMPOSITE_GC_TICK_STATE_DIR:-${CORE_ROOT:-$SCRIPT_DIR/..}/logs}"
LOG="$STATE_DIR/composite-gc-tick.log"
STAMP="$STATE_DIR/.composite-gc-tick-last-run"
LOCKDIR="$STATE_DIR/.composite-gc-tick-lock"
POSTER="${COMPOSITE_GC_TICK_POSTER:-$SCRIPT_DIR/board-post.sh}"
POST_STATE="$STATE_DIR/.composite-gc-tick-post"
ROUTE_CHANNEL="coordination"   # channel names are not validated and a typo is permanent (board.md): read this one back before changing it
REPOST_S=86400
POST_TIMEOUT_S="${COMPOSITE_GC_TICK_POST_TIMEOUT_S:-120}"
RUNNER_MAX_S="${COMPOSITE_GC_TICK_RUNNER_MAX_S:-1500}"   # below composite_gc_runner.LEASE_TTL_S (1800): see THE BOUND
KILL_GRACE_S="${COMPOSITE_GC_TICK_KILL_GRACE_S:-30}"     # a runner that ignores the TERM is KILLed this much later
ROUTE_FOOTER='Posted by the composite-GC tick (g-358-202 U16), not by an agent. The same condition is not posted again from this box for 24 h, and a clean pass ends it. Where to look: core/logs/composite-gc-tick.log on the posting box, and the stored state documents, read back with the two read-backs in "The tick and the flip checklist (U15)" in core/config/rationale/aspirations-store-segmentation.md. Nothing is filed for this post: whoever acts on it files or claims an Investigate.'
mkdir -p "$STATE_DIR" 2>/dev/null || true

# --- Config probe (single python read; fail-SAFE to disabled) -----------------
# Prints "<enabled> <interval_minutes>". The interval is validated below, once.
probe="$(TICK_CFG="$CONFIG" python3 - <<'PY' || echo "false 30"
import os, sys, yaml
try:
    cfg = yaml.safe_load(open(os.environ["TICK_CFG"], encoding="utf-8")) or {}
    blk = cfg.get("composite_gc_tick") or {}
    print("true" if blk.get("enabled") is True else "false", blk.get("interval_minutes", 30))
except Exception as exc:
    print("composite-gc-tick: config probe failed, tick disabled: %s" % exc, file=sys.stderr)
    print("false 30")
PY
)"
read -r ENABLED INTERVAL_MIN <<EOF
$probe
EOF
ENABLED="${ENABLED:-false}"
case "${INTERVAL_MIN:-}" in ''|*[!0-9]*) INTERVAL_MIN=30 ;; esac
INTERVAL_MIN=$((10#$INTERVAL_MIN))   # base 10: a leading zero must not read as octal
[ "$INTERVAL_MIN" -ge 1 ] || INTERVAL_MIN=30

[ "$ENABLED" = "true" ] || exit 0

# --- Cadence gate (machine-local stamp). A stamp dated in the future is due,
# like the runner's own: honouring it would stop the tick until the clock caught up.
if [ -f "$STAMP" ]; then
    now=$(date +%s)
    last=$(stat -c %Y "$STAMP" 2>/dev/null || stat -f %m "$STAMP" 2>/dev/null || echo 0)
    elapsed=$(( now - last ))
    [ "$elapsed" -lt 0 ] || [ "$elapsed" -ge $(( INTERVAL_MIN * 60 )) ] || exit 0
fi

# --- Per-box lock (a stale lock is reclaimed after 2h, mirroring the siblings) --
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCKDIR" 2>/dev/null || stat -f %m "$LOCKDIR" 2>/dev/null || date +%s) ))
    [ "$lock_age" -ge 7200 ] || exit 0
    rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
    mkdir "$LOCKDIR" 2>/dev/null || exit 0
fi

# --- Routing (see the header). Best effort: it prints one `composite-gc route:` line per
# decision that matters, and its caller discards what it returns.
route_result() {
    local rc="${1:-0}" out="${2:-}" elapsed="${3:-0}" decision first rest action sig kind keytext body ptype msgid prc id
    decision="$(ROUTE_RC="$rc" ROUTE_OUT="$(printf '%s\n' "$out" | tail -n 20)" ROUTE_STATE="$POST_STATE" \
        ROUTE_REPOST_S="$REPOST_S" ROUTE_BOUND_S="$RUNNER_MAX_S" ROUTE_ELAPSED_S="$elapsed" python3 - <<'PY'
import hashlib, json, os, re, sys, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
rc = int(os.environ.get("ROUTE_RC") or 0)
bound, elapsed = int(os.environ["ROUTE_BOUND_S"]), int(os.environ["ROUTE_ELAPSED_S"])
# timeout answers 124 when it stopped the runner; 137 is the KILL that follows a TERM the runner ignored, but any
# other SIGKILL (the OOM killer) reads 137 too, so it is the bound's only once the bound's own time has passed
stopped = rc == 124 or (rc == 137 and elapsed >= bound)
lines = [ln for ln in os.environ.get("ROUTE_OUT", "").splitlines() if ln.strip()]
state, repost_s = os.environ["ROUTE_STATE"], int(os.environ["ROUTE_REPOST_S"])
# stderr is merged into the output (2>&1: a crash's traceback is what an anomaly post carries), so a warning printed
# after the summary is the last line of it. The result is the last line that opens a JSON object (guard-659)
objs = [line for line in lines if line.startswith("{")]
res = None
if objs:
    try:
        res = json.loads(objs[-1])
    except ValueError:
        pass


def lead(text):
    # the stable lead phrase of one line: digits blanked, cut at the first ':' or '(' (what follows
    # is a measurement, an id or an exception text, and a key built from it never matches twice)
    t = re.sub(r"\d+", "#", str(text).lower())
    return re.split(r"[:(]", t, maxsplit=1)[0].strip()


verdict = lead((res or {}).get("verdict") or "none")
post = (res or {}).get("post")
kind = body = None
classes = []
if isinstance(post, dict) and post.get("body"):
    kind = "anomaly" if post.get("severity") == "anomaly" else "deleted"
    body = str(post["body"])
    if kind == "anomaly":
        classes = sorted({lead(a) for a in (res.get("anomalies") or [])})
elif res is None or rc != 0:
    kind = "anomaly"
    tail = "; ".join(ln.strip()[:300] for ln in lines[-3:]) or "no output"
    if stopped:
        classes, why = ["wall-clock-bound"], (
            "the tick's wall-clock bound (%d s, below the runner's lease) stopped the runner (rc=%d). A stopped runner "
            "cannot print its summary, so what it finished is unknown: re-read the stored state and the newest archive "
            "run before acting, and do not re-run a delete pass on the strength of the missing line. A repeated stop "
            "at this bound says the bound or the store's size is wrong, not that the runner hung" % (bound, rc))
    elif res is None:
        classes, why = ["no-result-line"], "the runner printed no result line (rc=%d)" % rc
    else:
        classes, why = ["rc-without-post"], "the runner exited rc=%d with verdict %s and no post" % (rc, res.get("verdict"))
    body = "composite GC anomaly: %s\n- %s\n- last output: %s" % (verdict, why, tail)

if kind is None:
    print("clear - -" if verdict in ("observed", "applied") and os.path.exists(state) else "none - -")
    print("-")
    sys.exit(0)

key = "%s:%s:%s" % (kind, verdict, "|".join(classes))
sig = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
action = "post"
try:
    age = time.time() - os.stat(state).st_mtime
    if open(state, encoding="utf-8").read().strip() == sig and 0 <= age < repost_s:
        action = "dedup"
except OSError:
    pass
print("%s %s %s" % (action, sig, kind))
print(key)
if action == "post":
    print(body)
PY
)" || { echo "composite-gc route: the decision step failed; nothing posted"; return 1; }
    first="${decision%%$'\n'*}"
    rest="${decision#*$'\n'}"
    IFS=' ' read -r action sig kind <<<"$first"
    keytext="${rest%%$'\n'*}"
    body="${rest#*$'\n'}"
    case "${action:-none}" in
        post)
            if [ "$kind" = "anomaly" ]; then ptype=escalation; else ptype=finding; fi
            msgid="$(printf '%s\n\n%s\n' "$body" "$ROUTE_FOOTER" | timeout "$POST_TIMEOUT_S" bash "$POSTER" --channel "$ROUTE_CHANNEL" --type "$ptype" --tags "composite-gc,$kind")"; prc=$?
            id="$(printf '%s\n' "$msgid" | grep -Eo 'msg-[0-9]{8}-[0-9]{6}-[A-Za-z0-9_-]+' | head -n 1)"
            if [ "$prc" -eq 0 ] && [ -n "$id" ]; then
                printf '%s\n' "$sig" >"$POST_STATE.tmp" 2>/dev/null && mv -f "$POST_STATE.tmp" "$POST_STATE" 2>/dev/null \
                    || echo "composite-gc route: posted as $id but the de-duplication state could not be written; the next pass repeats the post"
                echo "composite-gc route: posted $kind to $ROUTE_CHANNEL as $id key=$sig ($keytext)"
            else
                echo "composite-gc route: post FAILED (rc=$prc, message id '${id:-none}'); nothing recorded, the next pass retries key=$sig ($keytext)"
            fi ;;
        dedup) echo "composite-gc route: deduplicated $kind key=$sig ($keytext); the next post for it is due $REPOST_S s after the last one" ;;
        clear) rm -f "$POST_STATE"; echo "composite-gc route: clean pass, the episode is over" ;;
        *) ;;
    esac
    return 0
}

# --- The pass. Stamp and lock advance whatever the runner returned, so a runner
# that fails fast is retried once per interval, not once per iteration.
run_pass() {
    local out rc t0 elapsed
    t0=$(date +%s)
    out="$(timeout -k "$KILL_GRACE_S" "$RUNNER_MAX_S" python3 "$RUNNER" 2>&1)"; rc=$?
    elapsed=$(( $(date +%s) - t0 ))
    echo "--- $(date +%Y-%m-%dT%H:%M:%S) composite-gc tick rc=$rc agent=${MIND_AGENT:-unset} elapsed=${elapsed}s ---"
    printf '%s\n' "$out" | tail -n 5
    touch "$STAMP" 2>/dev/null || true
    rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
    route_result "$rc" "$out" "$elapsed" || echo "composite-gc route: the router failed (rc=$?)"
}

if [ -n "${COMPOSITE_GC_TICK_SYNC:-}" ]; then
    run_pass >>"$LOG" 2>&1 </dev/null
else
    ( run_pass ) >>"$LOG" 2>&1 </dev/null &
fi

exit 0
