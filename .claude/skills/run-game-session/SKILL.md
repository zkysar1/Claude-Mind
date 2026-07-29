---
name: "run-game-session"
forged: true
forged_by: alpha
forged_date: "2026-03-29"
description: "Runs a live Roblox game session via AyoBridge, monitors NPC activity during the run, and verifies that cell data was generated and written to EFS afterwards. Use whenever the agent needs live NPC behavior data — testing a Processor run in the wild, validating a recent deploy, exercising a new NPC task, or producing fresh session data for /analyze-npc-behavior. Pulls outcomes from Studio Bridge (SYSTEM HALTED console scan — the authoritative client-integration error signal), Operator API, and the alert@ayoai.com S3 archive (infrastructure errors only; client-integration errors route out-of-band — see Step 5.5)."
user-invocable: false
minimum_mode: autonomous
tools_used: [Bash]
companion_scripts:
  - "world/scripts/roblox-studio.sh"
  - "world/scripts/operator-api.sh"
  - "world/scripts/email-read.sh"
  - "world/scripts/game-session-lifecycle.sh"
conventions:
  - infrastructure
revision_id: "skill-bootstrap-run-game-session-5f7a56"
previous_revision_id: null
---

# /run-game-session — Game Session Runner

Runs a complete game session lifecycle: pre-flight checks, session start,
NPC activity monitoring, data verification on EFS, and graceful shutdown.

## Parameters

```
/run-game-session                     # Default: 15 minutes
/run-game-session --duration <min>    # Custom duration in minutes
/run-game-session --verify-only       # Skip session, just verify latest EFS data
```

### Duration Discipline (recurring-goal fires)

When invoked as part of a recurring goal fire (e.g., `g-115-15`), DO NOT
override `--duration` below **10 minutes**. Cells land on completion
boundaries, so short sessions yield zero completed `CellExecutionLog`
entries — draining the data artifact the recurring goal exists to
produce. The recurring goal description mandates 15 minutes; honor it.
See `rb-434`, `rb-435`, `sig-006`, and `guard-369` for the incident,
the generalized principle, the structural detector, and the action
gate. Ad-hoc short sessions are fine for targeted debugging, not for
the recurring pipeline-health fire.

### Detached Lifecycle Runner (rb-3539 pattern — recurring fires)

For unattended full-lifecycle runs (recurring g-115-15 fires, long watches),
do NOT hand-write a temp script — use the promoted companion runner
`world/scripts/game-session-lifecycle.sh` (g-115-2266). It performs EFS
baseline → start-session → 25s init check → 150s-poll monitor (halt/end
detection, `len(lines)` from session-console JSON, never `wc -l`) → final
console dump → EFS post-collection (newest session dir by mtime, rb-1685),
and ends its log with `LIFECYCLE_COMPLETE status=...`. Collection only —
the LLM renders verdicts from the log after re-invocation.

Launch detached from a Bash tool call (`run_in_background: true`):

```
LOG="$(agent_dir "$AYOAI_AGENT")/temp/game-session-lifecycle-$(date +%Y%m%d-%H%M%S).log"
echo $$ > /tmp/gsl.pid; exec bash "$WORLD_PATH/scripts/game-session-lifecycle.sh" \
    --env dev --duration 15 --log "$LOG"
```

then register for the stop-hook cross-turn wait:
`background-jobs.sh register ... --completion-check "grep -q LIFECYCLE_COMPLETE $LOG"`.
Flags: `--env` (default dev), `--duration` (minutes, default 15 — keep >=10 on
recurring fires per the Duration Discipline above), `--log`, `--account`.

## Pre-Flight Checks

Before starting a session, verify all infrastructure is ready:

```
0. Ensure Bridge Server Running (AGENT RESPONSIBILITY)
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" start-bridge
   # This starts the local Python bridge server on port 28080.
   # The bridge is the AGENT'S side of the connection — always start it.
   # If already running, the script is idempotent (exits 0).
   # The Roblox Studio plugin connects TO this server automatically.

1. Bridge Status (verify plugin connected)
   Bash: sleep 5 && world/scripts/roblox-studio.sh status
   REQUIRE: plugin_connected == true
   IF false after 15s: WARN — "Bridge running but Studio plugin not connected. Studio may not be open."
   # Do NOT abort immediately — retry once after 10s. The plugin polls every few seconds.

2. Operator Health
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/operator-api.sh" GET /status
   REQUIRE: health_score > 0
   IF fail: WARN — "Operator unreachable. Game server may not have backend support."

3. Environment Server Deployed
   Bash: gh run list --repo zkysar1/Ayoai-Environment-Server --limit 1 --json conclusion
   REQUIRE: conclusion == "success"
   IF fail: WARN — "Latest server build failed. NPCs may run stale code."

4. (intentionally omitted — BitNet health is NOT pre-checkable)
   # BitNet runs on the EC2 game server at port 8081 and ONLY exists while
   # a game session is running. The agent has no direct network path to
   # game servers (per session-46 architecture review), so a pre-flight
   # probe is structurally impossible.
   #
   # The legacy `infra-health.sh check bitnet` path routes to
   # `world/scripts/probe-bitnet.sh` which hits localhost:8080 on the AGENT
   # machine. That is LOCAL DEV ONLY — in production it always fails and
   # produces misleading "BitNet down" signals. Do NOT call it here.
   #
   # Authoritative BitNet health signals are session-scoped and live
   # downstream in this skill:
   #   (a) SYSTEM HALTED in the session console (Step 3). Driver.java's
   #       runtime health timer (15s period, 4-failure threshold, g-115-116) triggers
   #       it within ~60s of BitNet death — the existing Step 3 scan
   #       already covers this.
   #   (b) `/reportapi/serverDetail` `bitnetHealthy` field on the running
   #       env-server (Step 5b below) — surfaces the live health and
   #       saturation rate without waiting for SYSTEM HALTED.
   #
   # There is NO cloud LLM fallback. `getFailoverProvider` returns null;
   # BitNet death = NPC intelligence death for the session. See
   # world/knowledge/tree/intelligence/ayoai-core-engine/bitnet-server/
   # bitnet-integration.md "If-Then" rules 35-36.
```

## Session Lifecycle

### Step 1: Start Session

```
# Pass the parsed --duration <min> through to the bridge as --duration.
# The script (roblox-studio.sh start-session) accepts --duration <min> and
# multiplies by 60 to produce timeoutSeconds; default 10min applies if
# omitted. Pre-fix (g-115-321, before 2026-04-30): the script silently
# dropped --duration and bridge always sent timeoutSeconds=600 — so all
# sessions hard-capped at 10min regardless of caller intent. SKILL.md
# documented --duration as a user-facing parameter; the wire path simply
# never honored it. Now wired.
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" start-session --duration <min>
→ Returns: {"session_id": "ses-XXXX", "queued": true}
Store session_id for monitoring.
```

### Step 2: Verify Initialization (30s timeout)

```
Wait 20 seconds for session to initialize.
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" session-console <session_id>
Parse console lines for:
  - "SYNC] Initial environment sync complete" → GOOD (server connected)
  - "SYSTEM HALTED" → ABORT — extract Error Type and Error message
  - "Rate limit" → ABORT — "Rate limit hit. Wait 60s and retry."

IF "SYNC complete" not found after 30s:
  WARN — "Session started but sync not confirmed. Proceeding with monitoring."
```

### Step 3: Monitor NPC Activity

```
EVERY 5 minutes until target duration:
  1. Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" status
     IF active_sessions == 0: session ended prematurely — go to Step 5

  2. Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" session-console <session_id>
     Count total console lines — if not growing, NPCs may be stalled.
     Check for "SYSTEM HALTED" → session crashed, go to Step 5.

  3. Log: "Session {session_id}: {elapsed}m / {target}m — {line_count} console lines"
```

### Step 3.5: Pre-Stop BitNet Health Snapshot (report API)

```
# While the env-server is still running, snapshot its self-reported health.
# This is the only agent-reachable production BitNet health signal — the
# pre-flight localhost:8080 probe has been removed (see Step 4 of Pre-Flight
# Checks above for rationale).
#
# Path: Operator /ec2-instances → filter type=game-server,state=running →
# HTTPS to ayoaiHostname:8686/reportapi/serverDetail (AYOAI-API-KEY header,
# self-signed cert accepted). See server-detail.py for URL derivation.

Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" server-detail
# Returns JSON:
#   servers[].bitnetHealthy        (bool — consecutiveBitNetFailures < 4)
#   servers[].llmSaturationRate    (float — circuitBreakerTrips / totalAttempts)
#   servers[].llmTotalAttempts, llmSuccesses, llmCircuitBreakerTrips
#   servers[].stepCount, usedMemoryPercent, totalMemoryGB
#
# IF servers is empty:
#   Log: "No running env-servers. Session may have already halted; rely on
#   Step 3 SYSTEM HALTED scan for the authoritative failure signal."
#
# FOR EACH server in servers:
#   IF bitnetHealthy == false:
#     Record in session report: "BitNet unhealthy on {instanceId} (serverKey={serverKey})"
#   IF llmSaturationRate > 0.50:
#     Record soft warning: "High LLM saturation {rate} — server may shut down
#     soon (>50% triggers shutdown per Driver.java saturation gate)."
#
# NOTE: A missing snapshot (server-detail fails, e.g., 401/network) is NOT
# an abort signal — it is observability gap. The authoritative live failure
# signal remains "SYSTEM HALTED" in the session console (Step 3 scan).
```

### Step 5: Verify Cell Data on EFS

Path source of truth: `world/conventions/efs-session-paths.md` (canonical
locator). Always probe via `world/scripts/efs-ssh.sh` per
`.claude/rules/probe-with-canonical-code-path.md` — never raw `ssh`.

```
ACCOUNT="b1fb6520-c051-70df-ca6e-6ce15efd8d47"
ENV_KEY="NPCDemoExperiment"
EFS_ROOT="/home/ec2-user/AyoAi-Efs/mnt/AyoAi"
SESSION_PATH="$EFS_ROOT/Accounts/$ACCOUNT/$ENV_KEY"

1. List SESSION DIRS ONLY (newest first):
   # Session dirs are named <unix-millis>_<3-digit-suffix> (all-numeric with one
   # underscore). A bare `ls -1t | head` grabs the newest ENTRY — often
   # ServerLifecycle.jsonl (a continuously-written file) or the shared memory/
   # dir (persistent cross-session, always freshest mtime) — NOT the just-ended
   # session dir, yielding an empty CellExecutionLog probe + wasted verification
   # cycles. rb-1685 (retrieval_count 23) encoded the target-the-session-dir
   # workaround; recurring g-115-15 hit this rake twice (2026-06-12, 2026-07-03)
   # because the pseudocode was never fixed. Filter to session-dir basenames
   # with `-type d -name '[0-9]*_[0-9]*'` (excludes memory/, testy/, and the
   # ServerLifecycle.jsonl file — plain `-type d` alone does NOT exclude the
   # fresh-mtime memory/ dir):
   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-ssh.sh" \
     "find $SESSION_PATH/ -maxdepth 1 -mindepth 1 -type d -name '[0-9]*_[0-9]*' \
      -printf '%T@ %f\n' 2>/dev/null | sort -rn | head -5"
   # <latest_session> = the basename (2nd field) of the first line.

2. Check memory directory exists:
   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-ssh.sh" "ls $SESSION_PATH/<latest_session>/memory/"
   REQUIRE: directories exist (BehaviorTrees, CellExecutionLog, ConflatedState)

3. Check for Intent data (post-fix verification):
   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-ssh.sh" "ls $SESSION_PATH/<latest_session>/memory/Intent/"
   IF Intent/ exists: "Intent data present — fix verified"
   IF Intent/ missing: "Intent data NOT present — fix may not be deployed"

4. Check cell count:
   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-ssh.sh" "wc -l $SESSION_PATH/<latest_session>/memory/CellExecutionLog/*.jsonl 2>/dev/null | tail -1"
   Report: "{N} cells generated"

5. Sample intent data for aspirationKey (canonical probe — never raw ssh,
   per the Step 5 header + probe-with-canonical-code-path.md):
   source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-ssh.sh" "head -3 $SESSION_PATH/<latest_session>/memory/Intent/*.jsonl"
   Check for "aspirationKey" field presence.
```

### Step 5.5: Post-Session Error-Signal Sweep

Two error classes can surface during a session and they travel on **different
channels**. A clean signal on one channel does NOT clear the other — the
authoritative discriminator is the SYSTEM HALTED console line, not the email
archive (rb-817: use the source-side signal, not a keyword-guessed proxy).

```
A. CLIENT-INTEGRATION errors (authoritative in-skill signal — already scanned
   in Step 2 and Step 3, surfaced here):
   - errorSource ∈ {USER_ENVIRONMENT_ERROR, INVALID_USER_INPUT,
     AYOAI_INTERNAL_ERROR, NO_AYOAI_API_RESPONSE}
   - Origin: GameScripts .../GlobalErrorCatch.server.lua executeSystemHalt()
     → fireCriticalError (red screen to players) + sendErrorNotification()
       POST https://api.ayoai.com/restV1/EnvironmentUsersNotifications
   - Delivery: the ENVIRONMENT ACCOUNT-OWNER's inbox. These are NOT delivered
     to alert@ayoai.com and are NOT in the SES→S3 archive. email-read.sh /
     alert-sweep.sh structurally cannot see them (guard-465: a monitoring
     wrapper only sees what its data source contains).
   - Detect: the "SYSTEM HALTED" + "Error Type:" + "Error:" lines from the
     Step 2/3 session-console scan ARE the signal. Extract:
       halt_error_type   = the "Error Type:" value (e.g. USER_ENVIRONMENT_ERROR)
       halt_error_detail = the "Error:" description (e.g. "CRITICAL ERROR:
                           BTEvents target unit not found in tree")
     Carry both into the Step 6 report. Absence of a SYSTEM HALTED line in a
     console that otherwise grew normally = no client-integration halt.

B. INFRASTRUCTURE errors (BitNet death, deploy/build failure, Lambda error):
   - Origin: GH Actions repos / env-server → SendErrorAlert Lambda → SES email
     to alert@ayoai.com → S3 archive (AYO_SES_EMAIL_S3_BUCKET).
   - Detect (THIS is the email-read.sh use the front-matter promises —
     rb-189: a declared capability is only real when a step invokes it):
       Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/email-read.sh" check-alerts --json --max 15
     Parse the JSON array. For each entry where is_failure == true AND
     is_analytics_test == false AND is_success == false:
       record "Infra alert during session window: {subject} [{date}]"
     is_analytics_test==true entries are the /api/analytics/test diagnostic
     self-test (alert-system-sns.md:51) — log-only, never escalate.
   - Any non-zero check-alerts exit (2 = S3/creds failure; other = probe
     crash) is NOT "no alerts". Record "infra alert sweep UNAVAILABLE
     (rc=N)" — do not assert all-clear (guard-465 / verify-before-assuming.md
     rule 4: a silent/failed probe is zero signals, not one). Test rc != 0,
     not rc == 2 specifically.
```

This step does not abort or block — the session is already over. It only
enriches the Step 6 report so neither error channel is silently dropped.

### Step 6: Report

```
Bash: echo -e "Session: {session_id}\nDuration: {actual_minutes}m (target: {target}m)\nConsole lines: {count}\nCells generated: {count}\nIntent data: {present/missing}\naspirationKey: {found/not_found}\nClient-integration halt: {halt_error_type + halt_error_detail / none}\nInfra alert sweep: {N matched / none / UNAVAILABLE rc=N}\nStatus: {SUCCESS / PARTIAL (crashed early) / FAILED}"
```

## Error Recovery

| Error | Recovery |
|-------|----------|
| Bridge not connected | Bridge server is agent-side — run `roblox-studio.sh start-bridge`. If bridge running but plugin not connected after 30s: Studio may be closed. Create pending-question for user AND continue with data-side work (EFS verification, processor, analysis). Do NOT create `participants:[user]` blocker. |
| Rate limit (HTTP 429) | Wait 60s, retry once. If still failing, abort and create Investigate goal. |
| SYSTEM HALTED (timeout) | Log crash time. Check if enough data was generated (>100 cells = usable). |
| SSH connection refused | Run `ssh-keygen -R <host>` then retry with `-o StrictHostKeyChecking=accept-new` |
| No session data on EFS | Check session went to correct account. Verify Environment Server is running. |

## Chaining

- **Called by**: `/aspirations-execute` (when goal has `skill: run-game-session`)
- **Calls**: `roblox-studio.sh`, `operator-api.sh`, `efs-ssh.sh` (EFS), `email-read.sh` (Step 5.5 infra-alert sweep)
- **Does NOT modify**: any state files, knowledge tree, or aspirations

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The Output block in Step 6 is a markdown summary — the skill MUST follow it with a
Bash call. Minimum terminal call: `Bash: echo "run-game-session complete"`.
