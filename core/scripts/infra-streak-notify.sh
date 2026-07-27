#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# infra-streak-notify.sh — Run streak-alert + dedup + route new alerts to /notify-user.
#
#  wrapper. Sequence:
#   1. Invoke infra-health.py streak-alert → emits JSON of components past threshold
#      within recency window.
#   2. For each alerting component, build the DOWN-EPISODE dedup key
#      "{component}:episode:{streak_started_at}" (or "{component}:legacy-episode"
#      when no streak_started_at stamp exists). This REPLACED the original
#      per-probe "{component}:{last_failure}" key in  (extends 
#      to ALL components): the per-probe key advanced every sensor run, so a
#      persistent outage re-emailed every cycle. See the "Dedup semantics" block
#      below for the full contract.
#   3. Look up the episode key in <agent>/session/infra-streak-sent.jsonl.
#      - If present: dedup hit (same episode) — re-notify ONLY when
#        re_escalation_hours has elapsed; an advanced probe-time last_failure
#        within the same episode does NOT re-queue.
#      - If absent: fresh episode — queue for notification, append key to sent file.
#   4. If --notify flag is set, invoke /notify-user forged skill for each queued
#      entry. Default is dry-run (prints what would be sent, writes sent file).
#
# Why dry-run default: a new alert gate shouldn't send emails until explicitly
# enabled. The caller (a recurring goal, a forge-worker, or a human) flips
# --notify once confident the dedup is correct.
#
# Sent-file schema (one JSON per line):
#   {"key": "component:episode:<streak_started_at>", "component": "...",
#    "last_failure": "...", "first_notified_at": "2026-04-21T04:40:00",
#    "iteration_source": "", "dry_run": bool, "re_escalation": bool}
#
# Dedup semantics (, extends  to ALL components): the key is the
# DOWN-EPISODE (component + streak_started_at), not the per-probe last_failure —
# probe-time keys advanced every sensor run, re-queueing persistent failures on
# every run regardless of notification cadence. Within an episode, --notify
# re-queues a re_escalation entry only after proactive_escalation.
# re_escalation_hours (aspirations.yaml, default 24; env override
# INFRA_STREAK_RE_ESCALATION_HOURS) since the newest sent record. Dry-run never
# re-escalates (it must not restart the cadence clock). Symmetrically (),
# a dry-run record never SUPPRESSES the first real notification: when the
# episode's newest record is dry_run, --notify queues as FIRST CONTACT rather
# than gating on the rehearsal timestamp. Legacy episodes with no
# streak_started_at stamp use a stable "component:legacy-episode" key until the
# episode resets.
#
# Fleet-shared episode dedup (, zeta fresh-eyes F1): the sent-file is
# per-agent while the  notify duty rotates across world-queue claimants,
# so each NEW claimant re-emailed the same episode within the re_escalation
# window. In --notify mode a coordination-board breadcrumb (tags:
# infra-streak-sent,<episode-key>,<agent>) is the fleet-shared cooldown —
# mirrors inbox-alert-age-check (). Design decision (a) via the
# sanctioned shared store: the board is daemon-routed (locked, own-cloud-synced);
# a raw world-file append would bypass both. Option (b) — pinning --notify to a
# designated agent — rejected: it couples alert delivery to one agent's claim
# cadence. The breadcrumb posts at payload-emit time regardless of email
# outcome (prevents retry storms; same posture as ). Fail-open: a
# board read error yields an empty fleet view (local-only dedup, at most one
# extra email per claimant); a post error is stderr-only.
#
# Test seams: --alert-file=<path> injects the streak-alert JSON (skips live
# probes); INFRA_STREAK_SENT_FILE overrides the sent-file path;
# INFRA_STREAK_BOARD_STUB=<file> replaces board read/post with a local JSONL
# file of {"tags": [...]} records (fleet-dedup hermetic testing).
#
# Exit codes: 0 = success (including no-alerts and dry-run); 1 = streak-alert
# crashed ( — a dead monitor must not read as healthy) or sent-file
# write failed; 3 = alert_count=0 with a STALE probe store (unreliable, NOT
# confirmed healthy — rb-4013).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
source "$SCRIPT_DIR/_platform.sh"

NOTIFY=false
ALERT_FILE_OVERRIDE=""
for arg in "$@"; do
    case "$arg" in
        --notify) NOTIFY=true ;;
        --alert-file=*) ALERT_FILE_OVERRIDE="${arg#*=}" ;;
        *) ;;
    esac
done

AGENT="${MIND_AGENT:-}"
if [ -z "$AGENT" ]; then
    echo "infra-streak-notify: ERROR — MIND_AGENT not set" >&2
    exit 1
fi

SENT_FILE="${INFRA_STREAK_SENT_FILE:-$(agent_dir "$AGENT")/session/infra-streak-sent.jsonl}"
mkdir -p "$(dirname "$SENT_FILE")"
touch "$SENT_FILE"

# Windows/Git-Bash path translation (): on Windows, $SENT_FILE expands to
# an MSYS-style path like /c/<WORKSPACE>/... which Python on Windows cannot open
# (FileNotFoundError). cygpath -w converts to native C:\ form. On Linux/macOS or
# anywhere cygpath is unavailable, fall back to the original path.
SENT_FILE_NATIVE=$(cygpath -w "$SENT_FILE" 2>/dev/null || printf '%s' "$SENT_FILE")

if [ -n "$ALERT_FILE_OVERRIDE" ]; then
    #  test seam: injected alert JSON — skip live probes entirely so
    # dedup/re-escalation logic is hermetically testable. The override was
    # parsed since  but never consumed; this wires it.
    CURRENT_ALERTS=$(cat "$ALERT_FILE_OVERRIDE")
else
    # : disk-free floor check rides THIS recurring cadence ().
    # The probe (world/scripts/probe-disk-free.sh) is auto-discovered by
    # infra-health.py, but nothing else checks it periodically — without a
    # cadence-driven check the floor breach is never RECORDED, so streak-alert
    # below can never surface it (two boxes hit root-fs 100% on 2026-07-16 with
    # zero warning). Local df only, ~70ms — within this script's latency budget.
    # `|| true`: a probe error must never break the alert sweep itself.
    python3 "$CORE_ROOT/scripts/infra-health.py" check disk-free >/dev/null 2>&1 || true

    #  (guard-659): capture stdout ONLY -- no `2>&1`. The prior merge fed
    # infra-health.py's stderr into the json.loads below; any stderr line (e.g. a
    # WM-not-init WARN) failed the parse -> except -> ALERT_JSON='[]' -> ALERT_COUNT=0
    # -> false "no alerts" (a guard-465 silent-monitoring instance). stderr now flows
    # to this wrapper's stderr (visible to the caller), not into the JSON. streak-alert
    # emits its JSON on stdout; stderr is empty in the normal case (verified ).
    #
    #  (fresh-eyes F2): propagate a streak-alert CRASH as exit 1 instead of
    # swallowing it (`|| true` collapsed a crashed monitor into ALERT_JSON=[] ->
    # exit 0 "no alerts ... healthy" whenever the probe store was fresh -- the same
    # guard-465 class as , one layer up).
    # CRITICAL nuance (caught by the  live verify): rc=1 is the DESIGNED
    # alerts-pending signal (infra-health.py cmd_failing_streak: `if alerts:
    # sys.exit(1)`; parser help "nonzero exit = alerts pending") -- NOT a crash.
    # Discriminate by PARSE, not raw rc (rb-611 parse-then-gate): nonzero rc with
    # a parseable {components:[...]} envelope proceeds; nonzero rc with
    # unparseable/empty stdout is a real crash and exits 1.
    SA_RC=0
    CURRENT_ALERTS=$(python3 "$CORE_ROOT/scripts/infra-health.py" streak-alert) || SA_RC=$?
    if [ "$SA_RC" -ne 0 ]; then
        SA_PARSED=$(echo "$CURRENT_ALERTS" | python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read().strip())
    print("ok" if isinstance(d, dict) and "components" in d else "bad")
except Exception:
    print("bad")
')
        if [ "$SA_PARSED" != "ok" ]; then
            echo "infra-streak-notify: ERROR -- streak-alert exited rc=$SA_RC with unparseable output; refusing to report healthy on a crashed monitor (g-249-29/guard-465 class). Fix infra-health.py or re-run; alert state is UNKNOWN, not clean." >&2
            exit 1
        fi
    fi
fi
ALERT_JSON=$(echo "$CURRENT_ALERTS" | python3 -c "
import sys, json
raw = sys.stdin.read().strip()
try:
    d = json.loads(raw)
    print(json.dumps(d.get('components', [])))
except Exception:
    print('[]')
")

# : environment-reachability skip-list. A limited environment (a
# headless box, sandbox, or CI runner) structurally cannot reach some infra
# components -- their streaks are environment-reachability gates, not outages
# (rb-2908). Drop those components BEFORE counting/dedup/notify so the wrapper
# never tracks or escalates them. Domain-agnostic: the environment declares its
# own unreachable components via the INFRA_STREAK_SKIP_COMPONENTS env-var
# (comma-separated component names; per-machine scoping since env-vars are
# per-shell). Empty/unset -> skip nothing (backward-compatible). The list is
# passed via env (SKIP_LIST), never interpolated into the python source (guard-165).
if [ -n "${INFRA_STREAK_SKIP_COMPONENTS:-}" ]; then
    ALERT_JSON=$(echo "$ALERT_JSON" | SKIP_LIST="$INFRA_STREAK_SKIP_COMPONENTS" python3 -c "
import sys, json, os
alerts = json.loads(sys.stdin.read())
skip = {s.strip() for s in os.environ.get('SKIP_LIST', '').split(',') if s.strip()}
kept = [a for a in alerts if a.get('component') not in skip]
dropped = [a.get('component') for a in alerts if a.get('component') in skip]
if dropped:
    sys.stderr.write('infra-streak-notify: skipped ' + str(len(dropped)) + ' environment-unreachable component(s) per INFRA_STREAK_SKIP_COMPONENTS: ' + ', '.join(dropped) + '\n')
print(json.dumps(kept))
")
fi

ALERT_COUNT=$(echo "$ALERT_JSON" | python3 -c "import sys, json; print(len(json.loads(sys.stdin.read())))")

#  (rb-4013): probe-store freshness gate. streak-alert filters to failures
# within window_hours, so if the WHOLE store is stale (newest probe across all
# components older than window_hours) the recency filter has NO in-window data and
# ANY result -- especially alert_count=0 -- is a false reading of staleness, not the
# current state (the 2026-07-18  incident: the 12:08 run reported
# alert_count=0 while ci had been failing 10 days; the 15:00 run WITH a fresh
# infra-health check-all first surfaced 5 streaks). The caller (the 
# recurring goal) is EXPECTED to run `infra-health.py check-all` first; this gate
# catches when that was skipped or the fleet refresh cadence broke, so a stale run
# cannot masquerade as healthy. probe-freshness is a local YAML read only (~ms) --
# preserves the IRREDUCIBLY-LOCAL latency budget (line 2); this is option (b), NOT
# the minutes-long internal check-all of option (a). Fail-open: a probe-freshness
# error yields stale=false so a bug here never blocks the alert sweep.
if [ -n "$ALERT_FILE_OVERRIDE" ]; then
    FRESHNESS_JSON='{}'   # test seam: injected alerts — live-store freshness is irrelevant
else
    FRESHNESS_JSON=$(python3 "$CORE_ROOT/scripts/infra-health.py" probe-freshness 2>/dev/null || echo '{}')
fi
IS_STALE=$(echo "$FRESHNESS_JSON" | python3 -c "
import sys, json
try: print('true' if json.loads(sys.stdin.read()).get('stale') else 'false')
except Exception: print('false')
")
NEWEST_AGE=$(echo "$FRESHNESS_JSON" | python3 -c "
import sys, json
try: print(json.loads(sys.stdin.read()).get('newest_age_hours'))
except Exception: print('unknown')
")
WINDOW_H=$(echo "$FRESHNESS_JSON" | python3 -c "
import sys, json
try: print(json.loads(sys.stdin.read()).get('window_hours'))
except Exception: print('unknown')
")

if [ "$ALERT_COUNT" -eq 0 ]; then
    if [ "$IS_STALE" = "true" ]; then
        # 0 alerts from a STALE store is the false-healthy failure this gate exists
        # to prevent -- do NOT report healthy; WARN loudly and exit 3 (distinct from
        # 0=confirmed-healthy and 1=hard-error) so an automated caller structurally
        # cannot record a stale result as clean.
        echo "infra-streak-notify: WARNING PROBE DATA STALE -- newest infra-health probe is ${NEWEST_AGE}h old (> ${WINDOW_H}h recency window). streak-alert is BLIND to in-window failures, so alert_count=0 is UNRELIABLE and may be false-healthy (rb-4013 / g-249-06). Run 'infra-health.py check-all' to refresh the probe store, then re-run. NOT reporting healthy." >&2
        echo "infra-streak-notify: STALE -- alert_count=0 is UNRELIABLE (newest probe ${NEWEST_AGE}h > ${WINDOW_H}h window); run check-all first, NOT confirmed healthy (exit 3)"
        exit 3
    fi
    echo "infra-streak-notify: no alerts (streak-alert alert_count=0; probe store fresh: newest ${NEWEST_AGE}h <= ${WINDOW_H}h window)"
    exit 0
fi

# : alerts DID surface (they are in-window, hence fresh), but if the store
# is ALSO stale, MORE failing components may be hidden by staleness (the incident's
# 0-vs-5 gap). Notify the real alerts below, but WARN the picture is incomplete.
if [ "$IS_STALE" = "true" ]; then
    echo "infra-streak-notify: WARNING probe store stale (newest ${NEWEST_AGE}h > ${WINDOW_H}h window) -- the ${ALERT_COUNT} alert(s) below are real (in-window) but MORE failing components may be hidden; run 'infra-health.py check-all' for the complete picture (rb-4013 / g-249-06)." >&2
fi

# Dedup: build list of new (unsent) alerts + due re-escalations.
#  (extends  to ALL components): episode keying — the per-probe
# last_failure key advanced every sensor run, so persistent non-human-gated
# failures (ci=16/cloud-place=5/deploy-chain=6 on 2026-07-18) re-queued and
# re-emailed EVERY run regardless of re_escalation_hours. Within an episode the
# USER is re-notified only after proactive_escalation.re_escalation_hours
# (--notify mode only — dry-run must not restart the cadence clock).
# Inputs via env, python source single-quoted (guard-165).
NEW_ALERTS=$(echo "$ALERT_JSON" | \
    SENT_PATH="$SENT_FILE_NATIVE" NOTIFY_MODE="$NOTIFY" CONFIG_PATH="$CORE_ROOT/config/aspirations.yaml" \
    RE_ESC_OVERRIDE="${INFRA_STREAK_RE_ESCALATION_HOURS:-}" python3 -c '
import sys, json, os, datetime
alerts = json.loads(sys.stdin.read())
sent_path = os.environ["SENT_PATH"]
notify_mode = os.environ.get("NOTIFY_MODE") == "true"

re_esc_hours = 24.0
override = os.environ.get("RE_ESC_OVERRIDE", "").strip()
if override:
    re_esc_hours = float(override)
else:
    try:
        import yaml
        cfg = yaml.safe_load(open(os.environ["CONFIG_PATH"], encoding="utf-8")) or {}
        re_esc_hours = float((cfg.get("proactive_escalation") or {}).get("re_escalation_hours", 24))
    except Exception:
        pass  # fail-open to the 24h default

newest = {}  # key -> newest sent record (file order; last wins)
try:
    with open(sent_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                k = rec.get("key")
                if k:
                    newest[k] = rec
            except Exception:
                pass
except FileNotFoundError:
    pass

now = datetime.datetime.now()
new = []
for a in alerts:
    # Episode key for every component ( originally human-gated only).
    # Legacy mid-episode entries (no streak_started_at stamp) get a stable
    # per-component key — a NEW episode re-stamps at the 0->1 transition and
    # naturally rotates the key.
    if a.get("streak_started_at"):
        key = "%s:episode:%s" % (a.get("component"), a.get("streak_started_at"))
    else:
        key = "%s:legacy-episode" % a.get("component")
    rec = newest.get(key)
    if rec is None:
        a["_key"] = key
        new.append(a)
        continue
    # Dedup hit — same episode already recorded. Re-escalate the USER notify
    # only in --notify mode and only after re_esc_hours since the newest record.
    if notify_mode:
        #  (fresh-eyes F3): a dry-run record must not anchor the cadence
        # clock. Dry-run only ever appends as the episode-FIRST record (on a
        # dedup hit in dry mode nothing is appended), so newest.dry_run==true
        # means NO real notification was ever sent for this episode — queue as
        # FIRST CONTACT (not re-escalation) instead of gating on the rehearsal
        # timestamp. Enforces the header invariant "dry-run must not restart
        # the cadence clock" in BOTH directions.
        if rec.get("dry_run"):
            a["_key"] = key
            new.append(a)
            continue
        try:
            last = datetime.datetime.fromisoformat(str(rec.get("first_notified_at")))
            if (now - last).total_seconds() >= re_esc_hours * 3600:
                a["_key"] = key
                a["_re_escalation"] = True
                new.append(a)
        except (ValueError, TypeError):
            pass
print(json.dumps(new))
')

NEW_COUNT=$(echo "$NEW_ALERTS" | python3 -c "import sys, json; print(len(json.loads(sys.stdin.read())))")

if [ "$NEW_COUNT" -eq 0 ]; then
    echo "infra-streak-notify: $ALERT_COUNT alert(s) all previously sent (dedup hit)"
    exit 0
fi

NOW=$(date +%Y-%m-%dT%H:%M:%S)

#  (fresh-eyes F4): inputs via env, python source single-quoted
# (guard-165) — the prior double-quoted block interpolated $SENT_FILE_NATIVE/
# $NOW/$NOTIFY into the source text (a quote or trailing backslash in the
# path was a SyntaxError). Now matches the dedup block above.
echo "$NEW_ALERTS" | SENT_PATH="$SENT_FILE_NATIVE" NOW_TS="$NOW" NOTIFY_MODE="$NOTIFY" python3 -c '
import sys, json, os
new = json.loads(sys.stdin.read())
sent_path = os.environ["SENT_PATH"]
now = os.environ["NOW_TS"]
dry_run = os.environ.get("NOTIFY_MODE") != "true"
# : wrap open() in try/except so a bad path fails LOUDLY instead of
# silently losing alerts. Prior to this guard, a FileNotFoundError on Windows
# MSYS paths would print a traceback to stderr but the bash exit code stayed
# 0, masking the failure. Now we emit a clear error and exit 1 so the caller
# (recurring goal, forge worker, human) sees the breakage immediately.
try:
    f = open(sent_path, "a", encoding="utf-8")
except (FileNotFoundError, OSError, PermissionError) as e:
    sys.stderr.write(f"infra-streak-notify: ERROR opening sent_file {sent_path!r}: {e}\n")
    sys.exit(1)
with f:
    for a in new:
        record = {
            "key": a["_key"],
            "component": a["component"],
            "last_failure": a["last_failure"],
            "consecutive_failures": a.get("consecutive_failures"),
            "first_notified_at": now,
            "iteration_source": "g-249-05",
            "dry_run": dry_run,
            "re_escalation": bool(a.get("_re_escalation")),
        }
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        prefix = "[DRY-RUN]" if dry_run else "[NOTIFY]"
        if a.get("_re_escalation"):
            prefix += "[RE-ESCALATION]"
        comp, cf = a["component"], a.get("consecutive_failures")
        lf, reason = a["last_failure"], a.get("last_failure_reason", "(none)")
        print(f"{prefix} {comp}: {cf} consecutive failures, last at {lf}")
        print(f"  reason: {reason}")
'

if [ "$NOTIFY" = "true" ]; then
    # Iterate new alerts and call /notify-user forged skill for each.
    # /notify-user handles rate limiting + self-identity + fallback cascade.
    # : SUPPRESS the user email for components whose notify_suppressed_agents
    # list contains THIS box's agent -- box-locality false positives (a component this
    # box cannot host/reach reports \"down\", but that is NOT a product outage; rb-2908,
    # guard-1045). The streak is still tracked + logged above; only the user
    # notification is box-scoped. The hosting box (absent from the list) still
    # notifies. Agent passed via env (guard-165: never interpolate bash vars into
    # python source text).
    #  fleet-shared episode dedup (see header). Window = the same
    # re_escalation_hours the local dedup uses; the board read is --since-
    # windowed, so breadcrumb PRESENCE == claimed-within-window. Board hop is
    # --notify-only (the dry-run hot path stays local; header latency budget).
    RE_ESC_H=$(RE_ESC_OVERRIDE="${INFRA_STREAK_RE_ESCALATION_HOURS:-}" CONFIG_PATH="$CORE_ROOT/config/aspirations.yaml" python3 -c '
import os, math
# : ceil (not int-trunc) so the fleet board window is always >= the
# local float window. int(float(0.5))==0 gave a --since "0h" board query,
# shrinking the shared cooldown below the local dedup and re-permitting the
# cross-claimant double email. ceil of an integer config is unchanged.
v = os.environ.get("RE_ESC_OVERRIDE", "").strip()
if v:
    print(math.ceil(float(v)))
else:
    try:
        import yaml
        cfg = yaml.safe_load(open(os.environ["CONFIG_PATH"], encoding="utf-8")) or {}
        print(math.ceil(float((cfg.get("proactive_escalation") or {}).get("re_escalation_hours", 24))))
    except Exception:
        print(24)
')
    if [ -n "${INFRA_STREAK_BOARD_STUB:-}" ]; then
        FLEET_SENT_JSON=$(cat "$INFRA_STREAK_BOARD_STUB" 2>/dev/null || printf '')
    else
        FLEET_SENT_JSON=$(bash "$SCRIPT_DIR/board-read.sh" --channel coordination --since "${RE_ESC_H}h" --tag infra-streak-sent --json 2>/dev/null || printf '')
    fi
    echo "$NEW_ALERTS" | MIND_STREAK_AGENT="$AGENT" FLEET_SENT="$FLEET_SENT_JSON" \
        BOARD_STUB="${INFRA_STREAK_BOARD_STUB:-}" SCRIPT_DIR_ENV="$SCRIPT_DIR" python3 -c '
import sys, json, subprocess, os
new = json.loads(sys.stdin.read())
this_agent = os.environ.get("MIND_STREAK_AGENT", "")
board_stub = os.environ.get("BOARD_STUB", "")
script_dir = os.environ.get("SCRIPT_DIR_ENV", "")

# Fleet view: episode keys already claimed by ANY agent within the window.
# Parse leniently: whole-input JSON (dict-with-messages or list) OR JSONL lines.
fleet_keys = set()
raw = os.environ.get("FLEET_SENT", "").strip()
if raw:
    msgs = []
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            # dict-with-messages envelope OR a single bare message record
            msgs = d["messages"] if isinstance(d.get("messages"), list) else [d]
        else:
            msgs = d
    except Exception:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
                msgs.extend(m if isinstance(m, list) else [m])
            except Exception:
                pass
    for m in msgs:
        if not isinstance(m, dict):
            continue
        tags = m.get("tags") or []
        if "infra-streak-sent" in tags:
            for t in tags:
                if ":episode:" in t or t.endswith(":legacy-episode"):
                    fleet_keys.add(t)

def record_breadcrumb(key, component):
    msg = "infra-streak fleet-dedup breadcrumb: %s episode notified by %s (g-249-30)" % (component, this_agent)
    tags = "infra-streak-sent,%s,%s" % (key, this_agent)
    try:
        if board_stub:
            with open(board_stub, "a", encoding="utf-8") as f:
                f.write(json.dumps({"tags": tags.split(","), "body": msg}) + "\n")
        else:
            subprocess.run(["bash", os.path.join(script_dir, "board-post.sh"),
                            "--channel", "coordination", "--type", "status", "--tags", tags],
                           input=msg, text=True, capture_output=True, timeout=30)
    except Exception as e:
        sys.stderr.write("infra-streak-notify: WARN fleet breadcrumb post failed for %s: %s\n" % (key, e))

for a in new:
    suppressed = a.get("notify_suppressed_agents") or []
    if this_agent and this_agent in suppressed:
        print("[NOTIFY-SUPPRESSED] %s: box-locality gate for agent %r (not hosted/reachable from this box) -- streak tracked, user NOT notified (g-249-24/rb-2908)" % (a["component"], this_agent))
        continue
    key = a.get("_key", "")
    if key and key in fleet_keys:
        print("[NOTIFY-DEDUP-FLEET] %s: another agent already claimed this episode notification within the re_escalation window (board breadcrumb hit) -- streak tracked locally, user NOT re-notified (g-249-30)" % a["component"])
        continue
    subject = "Infra alert: %s %s consecutive failures" % (a["component"], a.get("consecutive_failures"))
    body = """Component: %s
Consecutive failures: %s
Last failure: %s
Reason: %s

Auto-fired by infra-streak-notify.sh (g-249-05). Check world/infra-health.yaml for full state.
""" % (a["component"], a.get("consecutive_failures"), a["last_failure"], a.get("last_failure_reason", "(none)"))
    # Placeholder: forged /notify-user invocation is Claude-side; the script
    # prints the payload so the caller (a forge skill step) can consume it.
    print("---NOTIFY-PAYLOAD---")
    print(json.dumps({"subject": subject, "body": body, "category": "blocker"}))
    print("---END-NOTIFY-PAYLOAD---")
    if key:
        record_breadcrumb(key, a["component"])
'
fi

echo "infra-streak-notify: queued $NEW_COUNT new alert(s) (sent-file: $SENT_FILE)"
