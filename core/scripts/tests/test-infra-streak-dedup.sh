#!/usr/bin/env bash
# test-infra-streak-dedup.sh — hermetic tests for infra-streak-notify.sh
# episode-keyed dedup + re_escalation_hours cadence (, extends ).
#
# Uses the --alert-file= injection seam + INFRA_STREAK_SENT_FILE override, so no
# live probes run and no production sent-file is touched. Scenarios:
#   1. First run on a fresh episode → queued once, episode-form key.
#   2. Same episode, ADVANCED probe-time last_failure → dedup hit, 0 queued
#      (the  over-fire regression: probe-time keys re-queued every run).
#   3. --notify with the sent record backdated > re_escalation_hours →
#      re-escalation queued, re_escalation:true recorded.
#   4. --notify with a fresh sent record (< re_escalation_hours) → 0 queued.
#   5. Dry-run with an aged record → 0 queued (dry-run never re-escalates).
#   6. Legacy alert (no streak_started_at) → legacy-episode key, dedups on rerun.
#   7. Dry-run first contact then --notify within window → queued as FIRST
#      CONTACT, not suppressed and not re-escalation ( fresh-eyes F3:
#      a rehearsal record must not anchor the cadence clock).
#   8. SA_RC crash discrimination (): live path (no --alert-file) with a
#      python3 shim making streak-alert CRASH (rc=2, unparseable stdout) →
#      wrapper exits 1 with the refusing-to-report-healthy ERROR ( /
#      guard-465 class — a dead monitor must not read as healthy).
#  8b. Same shim emitting a valid {"components": []} envelope with rc=1 (the
#      DESIGNED alerts-pending signal shape) → wrapper proceeds normally and
#      exits 0 (parse-then-gate, rb-611: discriminate by parse, not raw rc).
#
# Pass: prints "TEST PASS: 7 dedup cases + 2 SA_RC crash-discrimination cases
# verified", exit 0. Fail: exit 1.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../_paths.sh"

WRAPPER="$CORE_ROOT/scripts/infra-streak-notify.sh"
TMPDIR_T=$(mktemp -d)
trap 'rm -rf "$TMPDIR_T"' EXIT

ALERTS="$TMPDIR_T/alerts.json"
SENT="$TMPDIR_T/sent.jsonl"
export INFRA_STREAK_SENT_FILE="$SENT"
export INFRA_STREAK_RE_ESCALATION_HOURS=24
export MIND_AGENT="${MIND_AGENT:-testagent}"
#  hermeticity: EVERY --notify case must run against a board STUB, or
# the fleet-dedup live branch posts REAL coordination-board breadcrumbs carrying
# real episode keys (observed 2026-07-18: two live foxtrot breadcrumbs from
# cases 3/7 during the first post-fleet-dedup run — a live breadcrumb falsely
# suppresses a genuine notification for re_escalation_hours). Cases 9/9b
# override with their own stub file.
# _py_path: MSYS -> forward-slash Windows form, for paths handed to WINDOWS-NATIVE
# python (). mktemp -d yields /tmp/tmp.XXXX; windows python resolves that
# against the drive root as C:	mp	mp.XXXX, so CASE 9's breadcrumb write failed with
# "No such file or directory: '/tmp/tmp.XXXX/board-stub.jsonl'".
#
# APPLY PER-PATH, NEVER TO $TMPDIR_T ITSELF. Converting TMPDIR_T wholesale REGRESSED
# CASE 8 (measured): SHIM_DIR became C:/Users/.../shim, and MSYS splits PATH on ':',
# so that entry broke into "C" + "/Users/.../shim" and the python3 stub was never
# found. One variable, two incompatible consumers -- bash PATH needs POSIX form,
# windows python needs drive form. Convert at the point of use.
#
# -m not -w: forward slashes survive bash unescaped; -w backslashes would be eaten
# (guard-581). No cygpath on Linux/macOS -> passthrough.
_py_path() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}
export INFRA_STREAK_BOARD_STUB="$(_py_path "$TMPDIR_T/default-board-stub.jsonl")"
: > "$INFRA_STREAK_BOARD_STUB"

fail() { echo "CASE $1 FAIL: $2"; exit 1; }

# ── Case 1: fresh episode, first run → queued once with episode key ──────────
cat > "$ALERTS" <<'EOF'
{"components": [{"component": "ci", "consecutive_failures": 16, "last_failure": "2026-07-18T18:34:04",
  "last_failure_reason": "workflow failing", "streak_started_at": "2026-07-08T07:35:56",
  "human_gated": false, "notify_suppressed_agents": []}]}
EOF
: > "$SENT"
out=$(bash "$WRAPPER" --alert-file="$ALERTS")
echo "$out" | grep -q "queued 1 new alert" || fail 1 "expected 1 queued: $out"
grep -q '"key": "ci:episode:2026-07-08T07:35:56"' "$SENT" || fail 1 "episode key missing in sent-file: $(cat "$SENT")"
echo "CASE 1 PASS: fresh episode queued once (episode key)"

# ── Case 2: same episode, advanced last_failure → dedup hit (the regression) ─
cat > "$ALERTS" <<'EOF'
{"components": [{"component": "ci", "consecutive_failures": 17, "last_failure": "2026-07-18T22:00:00",
  "last_failure_reason": "workflow failing", "streak_started_at": "2026-07-08T07:35:56",
  "human_gated": false, "notify_suppressed_agents": []}]}
EOF
out=$(bash "$WRAPPER" --alert-file="$ALERTS")
echo "$out" | grep -q "all previously sent (dedup hit)" || fail 2 "expected dedup hit: $out"
[ "$(wc -l < "$SENT")" -eq 1 ] || fail 2 "sent-file grew on dedup hit: $(cat "$SENT")"
echo "CASE 2 PASS: advanced probe-time last_failure dedups within episode"

# ── Case 3: --notify with record aged > 24h → re-escalation ──────────────────
cat > "$SENT" <<'EOF'
{"key": "ci:episode:2026-07-08T07:35:56", "component": "ci", "last_failure": "2026-07-17T10:00:00", "consecutive_failures": 12, "first_notified_at": "2026-07-16T10:00:00", "iteration_source": "g-249-05", "dry_run": false}
EOF
out=$(bash "$WRAPPER" --alert-file="$ALERTS" --notify)
echo "$out" | grep -q "\[NOTIFY\]\[RE-ESCALATION\] ci" || fail 3 "expected re-escalation: $out"
grep -q '"re_escalation": true' "$SENT" || fail 3 "re_escalation record missing: $(cat "$SENT")"
[ "$(wc -l < "$SENT")" -eq 2 ] || fail 3 "expected 2 sent records: $(cat "$SENT")"
echo "CASE 3 PASS: --notify re-escalates after re_escalation_hours"

# ── Case 4: --notify with fresh record (just written by case 3) → suppressed ─
out=$(bash "$WRAPPER" --alert-file="$ALERTS" --notify)
echo "$out" | grep -q "all previously sent (dedup hit)" || fail 4 "expected suppression: $out"
[ "$(wc -l < "$SENT")" -eq 2 ] || fail 4 "sent-file grew while suppressed: $(cat "$SENT")"
echo "CASE 4 PASS: --notify suppressed within re_escalation_hours"

# ── Case 5: dry-run with aged record → no re-escalation ──────────────────────
cat > "$SENT" <<'EOF'
{"key": "ci:episode:2026-07-08T07:35:56", "component": "ci", "last_failure": "2026-07-17T10:00:00", "consecutive_failures": 12, "first_notified_at": "2026-07-16T10:00:00", "iteration_source": "g-249-05", "dry_run": false}
EOF
out=$(bash "$WRAPPER" --alert-file="$ALERTS")
echo "$out" | grep -q "all previously sent (dedup hit)" || fail 5 "dry-run must not re-escalate: $out"
[ "$(wc -l < "$SENT")" -eq 1 ] || fail 5 "dry-run restarted the cadence clock: $(cat "$SENT")"
echo "CASE 5 PASS: dry-run never re-escalates (clock preserved)"

# ── Case 6: legacy alert (no streak_started_at) → stable legacy key ──────────
cat > "$ALERTS" <<'EOF'
{"components": [{"component": "env-server", "consecutive_failures": 4, "last_failure": "2026-07-18T18:00:00",
  "last_failure_reason": "probe failed", "streak_started_at": null,
  "human_gated": false, "notify_suppressed_agents": []}]}
EOF
: > "$SENT"
out=$(bash "$WRAPPER" --alert-file="$ALERTS")
echo "$out" | grep -q "queued 1 new alert" || fail 6 "expected 1 queued: $out"
grep -q '"key": "env-server:legacy-episode"' "$SENT" || fail 6 "legacy key missing: $(cat "$SENT")"
cat > "$ALERTS" <<'EOF'
{"components": [{"component": "env-server", "consecutive_failures": 5, "last_failure": "2026-07-18T20:00:00",
  "last_failure_reason": "probe failed", "streak_started_at": null,
  "human_gated": false, "notify_suppressed_agents": []}]}
EOF
out=$(bash "$WRAPPER" --alert-file="$ALERTS")
echo "$out" | grep -q "all previously sent (dedup hit)" || fail 6 "legacy rerun must dedup: $out"
echo "CASE 6 PASS: legacy no-stamp alert uses stable per-component key"

# ── Case 7: dry-run first contact, then --notify within window → queued (F3) ─
cat > "$ALERTS" <<'EOF'
{"components": [{"component": "ci", "consecutive_failures": 16, "last_failure": "2026-07-18T18:34:04",
  "last_failure_reason": "workflow failing", "streak_started_at": "2026-07-08T07:35:56",
  "human_gated": false, "notify_suppressed_agents": []}]}
EOF
: > "$SENT"
out=$(bash "$WRAPPER" --alert-file="$ALERTS")
echo "$out" | grep -q "queued 1 new alert" || fail 7 "dry-run first contact should queue: $out"
grep -q '"dry_run": true' "$SENT" || fail 7 "dry-run record missing: $(cat "$SENT")"
out=$(bash "$WRAPPER" --alert-file="$ALERTS" --notify)
echo "$out" | grep -q "\[NOTIFY\] ci" || fail 7 "notify after dry-run must queue first contact: $out"
if echo "$out" | grep -q "RE-ESCALATION"; then fail 7 "first contact must not be re-escalation: $out"; fi
[ "$(wc -l < "$SENT")" -eq 2 ] || fail 7 "expected 2 records: $(cat "$SENT")"
tail -1 "$SENT" | grep -q '"dry_run": false' || fail 7 "newest record should be the real notify: $(cat "$SENT")"
out=$(bash "$WRAPPER" --alert-file="$ALERTS")
echo "$out" | grep -q "all previously sent (dedup hit)" || fail 7 "dry-run after real notify must dedup: $out"
echo "CASE 7 PASS: dry-run does not anchor first contact; --notify sends immediately"

# ── Cases 8/8b: SA_RC crash discrimination on the LIVE path () ────────
# The --alert-file seam bypasses the SA_RC branch entirely, so these cases run
# the wrapper WITHOUT it, behind a python3 shim that intercepts ONLY the
# infra-health.py invocations (keeps the run hermetic — no live probes, no
# probe-store writes) and execs the real interpreter for the parse helpers:
#   streak-alert    → per-SHIM_MODE behavior (crash | designed)
#   probe-freshness → '{}' (freshness unknown → stale=false, fail-open)
#   check disk-free → silent no-op (output is discarded and ||true'd anyway)
# MIND_SKIP_PY_SHIM=1 is REQUIRED for the shim to be reachable ().
# _paths.sh prepends core/scripts/.python-shim to the FRONT of PATH, so on
# Windows it shadows this stub the instant the wrapper sources it — the real
# monitor then ran, reported "no alerts", and CASE 8 failed as "expected exit
# 1, got rc=0". Note REAL_PY is captured BEFORE the seam is applied, so the
# shim still execs the genuine interpreter for non-intercepted calls.
REAL_PY=$(command -v python3)
SHIM_DIR="$TMPDIR_T/shim"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/python3" <<SHIM
#!/usr/bin/env bash
for a in "\$@"; do
    case "\$a" in
        streak-alert)
            if [ "\${SHIM_MODE:-}" = "crash" ]; then
                echo "Traceback (most recent call last): simulated monitor crash" >&2
                exit 2
            else
                printf '{"components": []}'
                exit 1
            fi
            ;;
        probe-freshness) printf '{}'; exit 0 ;;
    esac
done
case " \$* " in
    *"infra-health.py check "*) exit 0 ;;
esac
exec "$REAL_PY" "\$@"
SHIM
chmod +x "$SHIM_DIR/python3"

# Case 8: streak-alert crashes (rc=2, unparseable stdout) → wrapper must exit 1
# with the  refusing-to-report-healthy ERROR, never a clean "no alerts".
set +e
out=$(SHIM_MODE=crash MIND_SKIP_PY_SHIM=1 PATH="$SHIM_DIR:$PATH" bash "$WRAPPER" 2>&1)
rc=$?
set -e
[ "$rc" -eq 1 ] || fail 8 "expected exit 1 on crashed monitor, got rc=$rc: $out"
echo "$out" | grep -q "refusing to report healthy on a crashed monitor" || fail 8 "expected crash-discrimination ERROR: $out"
echo "$out" | grep -q "rc=2" || fail 8 "ERROR should carry the crash rc: $out"
echo "CASE 8 PASS: crashed streak-alert (unparseable) exits 1, not false-healthy"

# Case 8b: valid {"components": []} envelope with rc=1 (the designed
# alerts-pending exit shape) → parse succeeds, wrapper proceeds and exits 0.
set +e
out=$(SHIM_MODE=designed MIND_SKIP_PY_SHIM=1 PATH="$SHIM_DIR:$PATH" bash "$WRAPPER" 2>&1)
rc=$?
set -e
[ "$rc" -eq 0 ] || fail 8b "expected exit 0 on designed-signal rc=1, got rc=$rc: $out"
echo "$out" | grep -q "no alerts" || fail 8b "expected normal no-alerts path: $out"
echo "CASE 8b PASS: parseable envelope with nonzero rc proceeds (designed signal)"

# ── Case 9: fleet-shared dedup — two-agent simulation, single email per
# episode per window (). Agent A (--notify, empty board stub) emits
# the payload AND posts a breadcrumb; agent B (fresh sent-file, same stub)
# gets a fleet dedup hit: NO payload, [NOTIFY-DEDUP-FLEET] line instead.
cat > "$ALERTS" <<'EOF'
{"components": [{"component": "ci", "consecutive_failures": 16, "last_failure": "2026-07-18T18:34:04",
  "last_failure_reason": "workflow failing", "streak_started_at": "2026-07-08T07:35:56",
  "human_gated": false, "notify_suppressed_agents": []}]}
EOF
BOARD_STUB="$(_py_path "$TMPDIR_T/board-stub.jsonl")"
: > "$BOARD_STUB"
SENT_A="$TMPDIR_T/sent-agent-a.jsonl"; : > "$SENT_A"
out=$(INFRA_STREAK_SENT_FILE="$SENT_A" INFRA_STREAK_BOARD_STUB="$BOARD_STUB" MIND_AGENT=agent-a \
      bash "$WRAPPER" --notify --alert-file="$ALERTS")
echo "$out" | grep -q -- "---NOTIFY-PAYLOAD---" || fail 9 "agent A expected payload: $out"
grep -q "infra-streak-sent" "$BOARD_STUB" || fail 9 "agent A expected breadcrumb in stub: $(cat "$BOARD_STUB")"
grep -q "ci:episode:2026-07-08T07:35:56" "$BOARD_STUB" || fail 9 "breadcrumb missing episode key: $(cat "$BOARD_STUB")"
echo "CASE 9 PASS: agent A notified + posted fleet breadcrumb"

# Case 9b: agent B — no local sent record (fresh file) but the stub carries
# agent A's breadcrumb → local dedup queues it, fleet gate suppresses the email.
SENT_B="$TMPDIR_T/sent-agent-b.jsonl"; : > "$SENT_B"
out=$(INFRA_STREAK_SENT_FILE="$SENT_B" INFRA_STREAK_BOARD_STUB="$BOARD_STUB" MIND_AGENT=agent-b \
      bash "$WRAPPER" --notify --alert-file="$ALERTS")
echo "$out" | grep -q -- "---NOTIFY-PAYLOAD---" && fail 9b "agent B must NOT emit payload (fleet dedup): $out"
echo "$out" | grep -q "NOTIFY-DEDUP-FLEET" || fail 9b "agent B expected fleet dedup hit line: $out"
breadcrumbs=$(grep -c "infra-streak-sent" "$BOARD_STUB")
[ "$breadcrumbs" -eq 1 ] || fail 9b "suppressed path must not re-post breadcrumb (got $breadcrumbs): $(cat "$BOARD_STUB")"
echo "CASE 9b PASS: agent B fleet-dedup suppressed — single email per episode per window across agents"

echo "TEST PASS: 7 dedup cases + 2 SA_RC crash-discrimination cases + 2 fleet-dedup cases verified"
