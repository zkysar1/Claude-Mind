---
name: start
description: "Creates or resumes an agent in reader (read-only), assistant (user-directed), or autonomous mode (perpetual loop), handling full initialization for new agents (Self, program, paths, aspirations, curriculum) and state transitions for existing ones. USER-ONLY — Claude must NEVER invoke /start. Fires only when the user types /start {agent-name} [--mode {mode}]. Enforces the one-autonomous-session-per-agent invariant and supports observer sessions alongside running loops. Auto-recovers zombie sessions (state=RUNNING + stale heartbeat + no pending obligations) inline so /start <name> just works after a crash; --recover is reserved for the --force override path."
triggers:
  - "/start"
minimum_mode: any
conventions: [session-state, curriculum]
revision_id: "skill-bootstrap-start-3fc46d"
previous_revision_id: null
---

# /start — Start or Resume Agent

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

## Syntax

```
/start <agent-name>                    # Default: autonomous (backward compat)
/start <agent-name> --mode reader      # Read-only knowledge access
/start <agent-name> --mode assistant   # User-directed learning
/start <agent-name> --mode autonomous  # Full perpetual loop (same as no flag)
```

On resume (agent already exists):
```
/start                                 # Resume in current mode
/start --mode <mode>                   # Switch mode and resume
```

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

**Step 0.5: Parse Mode + Recovery Flags** — Extract the following from positional arguments:

- `--mode <value>`: mode flag. Valid values: `reader`, `assistant`, `autonomous`. If omitted, default to `autonomous`. This default applies uniformly — including the Phase A-0 transplant-resume path, where a bare `/start <agent>` on a freshly-cloned agent resumes it autonomously, exactly like a bare `/start` on any IDLE agent. Pass `--mode reader` (or `assistant`) explicitly for the cautious first-boot-on-a-new-machine case.
- `--recover`: recovery flag. Set `recover = true` if any argument is the literal string `--recover`. This flag triggers the crashed-runner cleanup in Step 0.7 below. Only meaningful when agent state is RUNNING; fails loud otherwise.
- `--force`: force flag. Set `force = true` if any argument is the literal string `--force`. Bypasses the heartbeat-staleness precondition on `--recover` (emergency override for the "heartbeat fresh but runner is stuck" case). No effect outside recovery.
- `--override-output-style <justification>`: override flag for the Step 0.6 + C7.7 autonomous+Explanatory gate. When present with a non-empty justification string, Step 0.6 lets the autonomous mode proceed, and C7.7 passes the same value to `output-style-gate.sh --override` for audit logging. The justification is echoed to `world/output-style-overrides.jsonl`.

The flag parser must run flag extraction BEFORE positional extraction so `/start --recover` (no agent name) binds to the current session's agent rather than being misinterpreted as `/start <agent-name=--recover>`.

**Step 0.6: Output-style preflight (autonomous mode only)** — When the parsed mode is `autonomous` (the default), read `.claude/settings.local.json` for the active `outputStyle` and warn the user IMMEDIATELY if it's set to `Explanatory`. The C7.7 Layer-B gate fires the same check later, but only AFTER all the long Phase A/B/C work (path prompts, permissions, init-mind, conventions, The Program, identity). Failing at C7.7 wastes ~30+ min of user investment. This preflight surfaces the collision before any state mutation.

Bash: `py -3 -c "import json,pathlib;p=pathlib.Path('.claude/settings.local.json');s=(json.loads(p.read_text(encoding='utf-8')).get('outputStyle') or '').strip().lower() if p.exists() else '';print(s)" 2>/dev/null`

IF the output is `explanatory`: print the following warning and STOP (do not proceed to Step 0.7 or Step 1):

```
⚠ OUTPUT-STYLE PREFLIGHT WARNING

You requested autonomous mode but the active output style is Explanatory.
This combination is a documented loop killer (rb-629, guard-454,
.claude/rules/return-protocol.md): Explanatory mandates trailing
"✶ Insight" blocks that land AFTER the terminal Skill(aspirations)
call as text, ending the turn and killing the loop.

The Layer-B gate at C7.7 would refuse this combination anyway, but
that's after ~30 min of path/identity/Program setup. Bailing early.

Switch with /output-style default (or any non-Explanatory style),
then re-issue /start <agent>. Or re-issue with
--override-output-style "<justification>" to audit-log and proceed.
```

IF the user re-issues with `--override-output-style "<justification>"`, accept and proceed to Step 0.7 (the override is re-validated and audit-logged by the C7.7 gate). Other output styles (default, concise, etc.) pass this preflight.

Fail-open: if the file or `outputStyle` key is absent, or `py -3` is unavailable, proceed silently — the C7.7 gate is the safety net.

**Step 0.7: Recovery Branch (only if `recover = true`)** — Runs BEFORE Step 1's state check so recovery can rewrite the state before Step 1 reads it.

Preconditions (all must hold, else fail loud — do NOT change any state):

1. Current agent-state is RUNNING (`MIND_AGENT=<agent-name> bash core/scripts/session-state-get.sh` returns `RUNNING`). If IDLE or UNINITIALIZED, there is nothing to recover — print "Nothing to recover: agent-state is <STATE>" and exit.

2. **Runner is structurally dead per the 6-condition liveness gate** OR `force = true`.

   Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-dead-check.sh`

   The helper checks 6 signals (state == RUNNING, heartbeat stale, no recent
   stop-hook BLOCK, execution-diary stale, stop-requested NOT set, no
   background-jobs pending) — the SAME gate that `recovery-gate.sh`
   (SessionStart hook auto-recovery) uses, and that the IDLE-branch
   auto-recovery section below ("RUNNING + requested mode is autonomous")
   mirrors in LLM-orchestrated form. SINGLE SOURCE OF TRUTH at
   `core/scripts/runner-dead-check.sh`. Stderr emits a per-condition summary;
   stdout emits structured JSON for `--force` audit logging.

   Exit codes:
   - `0` = runner is DEAD (all 6 conditions met — safe to recover)
   - `1` = runner is ALIVE (at least one liveness signal positive)
   - `2` = script error (fail-open conservative — refuse recovery)

   **IF exit code 0**: proceed to cleanup below.

   **IF exit code 2**: print "Refusing to recover: `runner-dead-check.sh`
   returned script error (rc=2). Investigate the helper and its sub-probes
   (`heartbeat-stale.sh`, `runner-recent-block.sh`, `session-signal-exists.sh`,
   `background-jobs.sh`) before retrying." and exit without state changes.

   **IF exit code 1 AND `force = false`**: print the stderr text from the
   helper (it lists which liveness signals are still positive), then:

   ```
   Refusing to recover: <agent-name> appears alive (one or more liveness signals positive).

   If another window is running the autonomous loop, /stop <agent-name> from that
   terminal first, then /start <agent-name> --recover here.

   If you just want to observe or assist without taking over the runner role:
     /start <agent-name> --mode reader
     /start <agent-name> --mode assistant

   To force recovery anyway (CLOBBERS in-flight work — only when you know the
   live signal is itself stale): /start <agent-name> --recover --force
   ```

   and exit without state changes.

   **IF exit code 1 AND `force = true`**: print "FORCING recovery despite live
   signals (--force):" followed by the helper's stderr per-condition list.
   Append a JSON audit record to `agents/<agent-name>/session/recovery-force-audit.jsonl`
   using the explicit locked-append helper so the write is race-safe even when
   two terminals attempt `--recover --force` concurrently:

   ```bash
   AGENT_NAME="<agent-name>" \
   HELPER_JSON='<full stdout JSON from runner-dead-check.sh>' \
   py -3 -c "
   import json, os, sys, datetime
   sys.path.insert(0, 'core/scripts')
   from _fileops import locked_append_jsonl
   record = json.loads(os.environ['HELPER_JSON'])
   record.update({
       'timestamp': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
       'agent':     os.environ['AGENT_NAME'],
       'trigger':   '/start --recover --force',
   })
   locked_append_jsonl(
       f'agents/{os.environ[\"AGENT_NAME\"]}/session/recovery-force-audit.jsonl',
       record,
   )
   "
   ```

   # Rationale (WHY locked_append + WHY 6-condition gate): core/config/rationale/start-recovery-ceremony.md
   Then proceed to cleanup.

**Both Step 1's `session-state-get.sh` call and the `runner-dead-check.sh` helper MUST use the explicit `MIND_AGENT=<agent-name>` prefix.** Step 0.7 runs BEFORE the session-binding rewrite in the IDLE branch Step 0, so `.active-agent-<SID>` may not exist or may point at a different agent. Without the prefix the scripts fall back to `_paths.sh`'s first-available-conf loop and would probe the wrong agent.

If preconditions pass, run cleanup in order. Recovery is **manifest-driven** —
the authoritative list of files to clear lives in `core/config/session-manifest.yaml`
(see `core/config/conventions/session-state.md` → "Session File Manifest").
Adding a new transient session file ONLY requires an entry in that YAML; this
recovery block picks it up automatically.

# Rationale (WHY state-set-first ordering): core/config/rationale/start-recovery-ceremony.md
- Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`

  If `session-state-set.sh` exits non-zero, fail loud — do NOT fall through.
  A failed state-set is recoverable on next SessionStart because manifest-
  clear has not yet fired (observer signals + SID still present, state still
  RUNNING — a normal RUNNING state).
  Output: "ERROR: Failed to set agent-state to IDLE. Manifest-clear was
   SKIPPED to avoid half-recovered zombie. Investigate agents/<agent-name>/session/agent-state
   directly before retrying." DONE.

- Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-manifest-clear.sh`
  Manifest-driven clear of every session file with `recovery_action: clear`.
  Runs AFTER state-set IDLE succeeded (g-115-683 reorder); the cleanup
  window now shows state=IDLE + sid present instead of state=RUNNING +
  sid=missing. SINGLE SOURCE OF TRUTH for the clear operation —
  `recovery-gate.sh` (SessionStart hook auto-recovery) calls the same
  script, and both consume `session-snapshot.sh --output json` (the
  canonical manifest parser, also used by `session-desync-check.sh`). The
  three signal files (`stop-requested`, `stop-loop`, `loop-active`) are all
  `recovery_action: clear` in the manifest, so this one call handles them
  too — no separate `session-signal-clear.sh` calls are required.

- Bash: `rm -f "agents/<agent-name>/session/recovery-failure-count" "agents/<agent-name>/session/recovery-failed-permanent" 2>/dev/null || true`
  Manual override: clear the recovery-circuit-breaker counter (2026-05-12
  hardening, Tier 2c). When recovery-gate has refused further automatic
  retries after 3 consecutive `_perform_recovery` failures, `/start --recover
  --force` is the documented escape hatch — it forces a fresh recovery
  attempt by deleting both the counter and the permanent-signal file.
  These two files are `recovery_action: preserve` in the manifest (so they
  survive normal manifest-clear runs to preserve cross-session memory of
  the failure state) — clearing them is a deliberate user-acknowledged
  override, hence the manual rm here outside the manifest pipeline.

- On success: Output: "Recovered crashed runner session. Cleared stale signals and session files (manifest-driven). Proceeding with normal start."

Fall through to Step 1 — state is now IDLE, which routes to the IDLE branch below (which respects `--mode`, default autonomous). This reuses existing cleanup semantics instead of duplicating them.

**Authorization note**: `/start --recover` is the third authorized caller of `session-state-set.sh` alongside the existing `/start` (IDLE → RUNNING) and `/stop` (RUNNING → IDLE) paths. The cleanup above is a targeted /start sub-path; see `.claude/rules/user-interaction.md` Script-Level Restrictions.

**Step 1: Check Requested Agent's State** — The agent name comes from the `/start <name>` argument.
Check THIS agent's state specifically:

Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-get.sh`

The `MIND_AGENT=<agent-name>` env prefix ensures we read `agents/<agent-name>/session/agent-state`,
not another agent's state. If no `<agent-name>` was provided (bare `/start` or `/start --mode`),
omit the prefix — use the current session binding.

**Step 1.5: UNINITIALIZED Drift-Warning Probe** — Defensive check for the
inlined-helper drift class. When Step 1 returns `UNINITIALIZED`, the agent
dir might genuinely not exist OR `session-state-get.sh` might have a stale
inlined `_APD` (AGENTS_PARENT_DIR) constant relative to `core/scripts/_paths.sh`
(rb-1092 — five sites inline that constant for latency, see CLAUDE.md
"Agent-dir Resolution"). The latter case would re-initialize a fully working
agent, clobbering aspirations, journal, handoff, and session history. This
probe distinguishes the two before the Phase A re-init begins.

```bash
IF state == "UNINITIALIZED":
    Bash: test -f "agents/<agent-name>/session/agent-state" && cat "agents/<agent-name>/session/agent-state"
    IF the file exists AND contents trim to "IDLE" or "RUNNING":
        Print the diagnostic below and STOP. Do NOT proceed to the
        UNINITIALIZED branch — that would re-initialize a fully working
        agent and lose all per-agent state.

        ⚠ /start drift-warning: state-get returned UNINITIALIZED but
        agents/<agent-name>/session/agent-state on disk is <X>.

        Suspected inlined-helper drift on AGENTS_PARENT_DIR (_APD).
        Five files inline that constant for latency (see CLAUDE.md
        "Agent-dir Resolution"):
          core/scripts/session-state-get.sh
          core/scripts/session-mode-get.sh
          core/scripts/session-signal-exists.sh
          core/scripts/cleanup-stale-bindings.sh   (also inlines _SDN)
          core/scripts/_wake_signals.py            (uses _AGENTS_PARENT_DIR)

        Investigate:
          git diff --stat -- core/scripts/session-state-get.sh core/scripts/_paths.sh
          git show HEAD:core/scripts/session-state-get.sh | grep _APD

        Reconcile working tree to HEAD before retrying /start.

        DONE.
    ELSE (file missing, empty, or other content):
        Fall through to the UNINITIALIZED branch — genuine first-run or
        wiped state.
```

This is the Layer-A tactical defense (loud diagnostic at /start entry).
The companion Layer-B is `/verify-learning`'s inlined-_APD audit, which
grep-checks the 5 sites against `_paths.sh` on a routine cadence so drift
is caught even when no /start re-entry surfaces it.

## Behavior by Current State

### RUNNING (agent-state contains "RUNNING")

The agent is in autonomous mode. This could mean another Claude Code window is
actively running the loop, OR a previous session crashed/closed without `/stop`.

Branch on the **requested mode** (parsed in Step 0.5):

#### RUNNING + requested mode is `autonomous` (or no `--mode` flag)

Two scenarios produce RUNNING-on-disk:
  1. **Live runner** — another Claude Code window is actively running the loop.
  2. **Zombie** — the previous session crashed without `/stop` and the state
     file is stale; no live runner exists.

Distinguish them via the 6-condition zombie gate.
# Rationale (WHY inline instead of calling helper + WHY each condition): core/config/rationale/start-recovery-ceremony.md
**SSOT: `core/scripts/runner-dead-check.sh` — conditions MUST stay in sync.**
Gate fires when ALL SIX hold:
  (1) `agent-state == RUNNING`            (already established by Step 1)
  (2) `heartbeat-stale.sh` returns `stale`
  (2.5) `runner-recent-block.sh` returns 1 (no BLOCK in last 5 min)
  (2.7) `execution-diary.jsonl` mtime older than 15 min (DIARY_STALE_MINUTES)
  (3) `stop-requested` is NOT set
  (4) `background-jobs.sh has-pending` exits 1 (no Tier-A registered job)

**Every probe call MUST use the explicit `MIND_AGENT=<agent-name>` prefix.**
At this point in `/start`, `.active-agent-<SID>` has not been written yet
(that happens in the IDLE branch's Step 0). Without the prefix, the
PreToolUse hook cannot auto-inject MIND_AGENT, and `_paths.sh` falls
through to its no-agent path: `AGENT_DIR` is empty, all probes read from
bogus paths, and EVERY probe returns the auto-recovery-passing value
regardless of actual agent health. That would auto-recover live runners.
Same warning as Step 0.7 (lines 44-45).

Probe condition 2 (heartbeat):

Bash: `MIND_AGENT=<agent-name> bash core/scripts/heartbeat-stale.sh`

**IF output is `fresh`** → live reducer runner detected (scenario 1). This second
terminal activates as a **Worker Body** (asp-306 / g-306-73 one-mind-two-bodies).
DO NOT auto-recover the reducer — that would clobber the active loop.

**To take over the reducer role instead** (reducer genuinely wedged despite fresh
heartbeat): `/start <agent-name> --recover --force`

**To open a read-only/assistant observer window**: `/start <agent-name> --mode reader`
or `/start <agent-name> --mode assistant`

**Worker Body Activation Sequence:**

W0. **Bind this session** (worker variant — MUST NOT touch `running-session-id` or
    `latest-session-id`; those are reducer-owned):

    Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; bash core/scripts/sid-collision-check.sh "<agent-name>" "$MIND_SID" || { echo "ERROR:SID_COLLISION"; exit 2; }; bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent "<agent-name>" --mode autonomous --retire-legacy >/dev/null && echo "BOUND_WORKER:$MIND_SID"`

    **HALT ON EMPTY MIND_SID** — display the same error as IDLE Step 0 below.
    **HALT ON SID_COLLISION** — display the same error as IDLE Step 0 below.

W0.4. **Write Body manifest** `--role worker` (FORK-BODY Phase 1B — g-306-62):

    Bash: `bash core/scripts/body-manifest.sh write --sid "$MIND_SID" --agent "<agent-name>" --role worker >/dev/null || echo "[start-worker] body-manifest write failed (non-fatal)" >&2`

    (Records this terminal as a Worker Body. The Reducer's `running-session-id` is
    already on disk — body-manifest.sh reads it to verify this session is NOT the
    reducer. Fail-open — must never block the bind.)

W1. **Fork canonical WM** (FORK-BODY Phase 1B — private WM snapshot for the worker):

    Bash: `mkdir -p "agents/<agent-name>/sessions/$MIND_SID" && cp "agents/<agent-name>/session/working-memory.yaml" "agents/<agent-name>/sessions/$MIND_SID/working-memory.yaml" 2>/dev/null && echo "WM_FORKED" || echo "[start-worker] WM fork failed — worker starts with empty WM (degraded but operational)" >&2`

    (The `bash-agent-inject.py` PreToolUse[Bash] hook (Phase 1A, g-306-68) detects
    this forked file and exports `BODY_WM_PATH` pointing to it on every Bash call,
    so all `wm-*.sh` writes land in the forked copy, never the canonical agent-wide
    WM. Reducers never have a per-SID WM file, so `BODY_WM_PATH` is never injected
    for them — backward-compat preserved. Merged back into the Reducer's WM at
    `aspirations-consolidate` Step -1 via `body-merge.py`. If the fork fails, the
    worker proceeds with an empty WM — degraded but operational; body-merge Step -1
    is a no-op for an unforked worker.)

W0.5. **Open session telemetry** (session-telemetry WP1 — fire-and-forget):

    Bash: `TSID="$MIND_SID" TAGENT="<agent-name>" TMODE="worker" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_open; write_open(sid=os.environ["TSID"], agent=os.environ["TAGENT"], mode=os.environ["TMODE"], started_by="claude-code")' >/dev/null 2>&1 || true`

W2. **Ensure daemon** (fail-open):

    Bash: `bash core/scripts/mind-api-start.sh || echo "[start-worker] daemon-start failed (non-fatal)" >&2`

W3. **Invoke worker loop**:

    Skill(worker-loop) with args: `<agent-name>`

    (Worker runs select→claim→execute only. Encode/reflect/state-update are the
    Reducer's sole responsibility at aspirations-consolidate generalize-down.
    State stays RUNNING under the Reducer's SID — this worker does NOT set
    agent-state, agent-mode, or persona-active.)

**IF output is `stale`** → probe condition 2.5:

Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-recent-block.sh <agent-name>; echo "recent_block_rc=$?"`

Exit-code semantics (matches recovery-gate.sh Cond 2.5):
- `runner-recent-block.sh`: 0=recent BLOCK present (alive), 1=none in last 5 min, 2+=script error.
  Treat `recent_block_rc != 1` as "hold back" (conservative — same pattern as
  Cond 4 `bg_jobs_rc != 1`; script errors must NOT stomp a possibly-live runner).

**IF recent_block_rc != 1** → runner appears alive (rc=0) OR probe errored
(rc=2+). Either way DO NOT auto-recover. Output the same "live runner detected"
message as the heartbeat=fresh path. DONE. No state changes.

**IF recent_block_rc == 1** → no recent BLOCK; continue to conditions 3 and 4:

Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-signal-exists.sh stop-requested; echo "stop_req_rc=$?"`
Bash: `MIND_AGENT=<agent-name> bash core/scripts/background-jobs.sh has-pending; echo "bg_jobs_rc=$?"`

Exit-code semantics (matches recovery-gate.sh):
- `session-signal-exists.sh stop-requested`: 0=signal SET, 1=signal absent.
- `background-jobs.sh has-pending`: 0=jobs PRESENT, 1=none, 2+=script error.
  Treat `bg_jobs_rc != 1` as "hold back" (conservative — script errors
  must not trigger recovery).

**IF stop_req_rc == 1 AND bg_jobs_rc == 1** (no stop-requested + no registered
background jobs; recent_block_rc == 1 already verified above) → **AUTO-RECOVER**
(zombie confirmed, scenario 2). Same cleanup as Step 0.7's explicit `--recover`
branch and as recovery-gate.sh's `_perform_recovery`.
# Rationale (WHY state-set-first ordering): core/config/rationale/start-recovery-ceremony.md

  Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`

  If `session-state-set.sh` exits non-zero, fail loud — do NOT fall through
  to manifest-clear. A failed state-set leaves state=RUNNING + sid present
  (normal RUNNING, recoverable on next SessionStart). Output:
  "ERROR: Failed to set agent-state to IDLE (session-state-set.sh non-zero exit).
   Manifest-clear was SKIPPED to avoid half-recovered zombie. Investigate
   agents/<agent-name>/session/agent-state directly before retrying." DONE.

  **Session-telemetry crash close (WP5, 2026-06-03).** This branch auto-recovers
  a crashed prior runner — the SAME event recovery-gate.sh handles via its
  SessionStart hook (WP4), but here it is LLM-orchestrated at /start time. The
  crashed runner's SID is still in `running-session-id` (manifest-clear below
  has not run yet), so finalize its durable telemetry record now with
  status=crashed, ended_reason=recovery-gate. MUST run BEFORE manifest-clear
  (which deletes running-session-id). write_crash forces goals_completed=-1
  (the crashed runner's outcome is unknown). Fire-and-forget (|| true) — a
  telemetry failure must NEVER abort recovery. guard-165: SID/agent via ENV,
  python source single-quoted. `py -3` (Bash-tool context — NOT a sourced .sh,
  so the Microsoft-Store-stub rule applies). Only when a crashed SID is present.
  Bash: `RECSID=$(cat "agents/<agent-name>/session/running-session-id" 2>/dev/null | tr -d '\r\n'); [ -n "$RECSID" ] && TSID="$RECSID" TAGENT="<agent-name>" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_crash; write_crash(sid=os.environ["TSID"], agent=os.environ["TAGENT"])' >/dev/null 2>&1 || true`

  Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-manifest-clear.sh`
  Runs AFTER state-set IDLE succeeded; cleanup window now shows
  state=IDLE + sid present (mirrors aspirations-graceful-stop D1→D6).

  On success, output:
  "Auto-recovered crashed runner session for <agent-name> (heartbeat stale, no
   pending obligations, no graceful stop in flight). Cleared stale signals and
   session files (manifest-driven). Proceeding with normal start."

  **Fall through to Step 1** — state is now IDLE, which routes to the IDLE
  branch below.

**ELSE** (recent BLOCK in last 5 min OR stop-requested set OR background-jobs
pending OR script error) → safety hold-back. Auto-recovery would clobber a
live runner or in-flight work; route the user to the right resolution:

```
⚠ Agent '<agent-name>' is in autonomous mode (RUNNING state), heartbeat stale,
  but auto-recovery was held back to preserve in-flight work:
    - <include if recent_block_rc == 0: "a stop-hook BLOCK was logged in the last 5 minutes — runner appears alive despite stale heartbeat (transient platform-hook timeout suspected). If you believe the runner is genuinely wedged, force recovery with --recover --force">
    - <include if stop_req_rc == 0: "a graceful stop is in flight (stop-requested signal present) — re-run /stop <agent-name> to finalize the stop">
    - <include if bg_jobs_rc == 0: "registered Tier-A background-jobs are still tracked (see agents/<agent-name>/session/background-jobs.yaml) — collect or kill them before recovering">
    - <include if bg_jobs_rc not in (0,1): "background-jobs.sh has-pending probe returned an unexpected exit (rc=<bg_jobs_rc>) — investigate the script before forcing recovery">

To force recovery anyway (CLOBBERS in-flight work — only when you know
the in-flight signal is itself stale):
  /start <agent-name> --recover --force

Or open a read-only/assistant window alongside the (maybe-) running loop:
  /start <agent-name> --mode reader
  /start <agent-name> --mode assistant
```

DONE.

#### RUNNING + requested mode is `reader` or `assistant` (Observer Session)

The observer session coexists with the autonomous loop. It does NOT write to
agent-state, agent-mode, persona-active, or running-session-id.

0. **Bind session** (observer variant — MUST NOT touch `agents/<agent-name>/session/latest-session-id`):

   Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; bash core/scripts/sid-collision-check.sh "<agent-name>" "$MIND_SID" || { echo "ERROR:SID_COLLISION"; exit 2; }; bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent "<agent-name>" --mode "<target-mode>" --retire-legacy >/dev/null && echo "BOUND:$MIND_SID" && printf '\n╔════════════════════════════════════════════════════════════╗\n║                                                            ║\n║    ✓  RACE_WINDOW_CLOSED                                   ║\n║       Safe to /start another agent in another terminal     ║\n║                                                            ║\n╚════════════════════════════════════════════════════════════╝\n\n'`

   `$MIND_SID` is injected by the PreToolUse hook (`core/scripts/bash-agent-inject.py`) — the single source of truth for this session's SID.

   `<target-mode>` here is the observer mode from the `--mode` flag on `/start` (`reader` or `assistant`). Substituted by the LLM at invocation time.

   Phase 2.6: the binding lives at `agents/<agent-name>/sessions/$MIND_SID/binding.yaml` — full intel (agent, mode, started_at, started_by), no root-cruft `.active-agent-<SID>` file. The `--retire-legacy` flag deletes any pre-Phase-2.6 `.active-agent-<SID>` file at PROJECT_ROOT to harden against the migration window.

   The collision check is identical to IDLE Step 0 — observer sessions can ALSO stomp a live binding when Claude Code reuses a SID (e.g., a stray `claude --continue` in a new terminal followed by `/start <other> --mode reader`). Refusing the bind at this boundary protects the runner whose binding would otherwise be flipped.

   **DO NOT ADD** a write to `agents/<agent-name>/session/latest-session-id` here. That file is runner-owned (written atomically with `running-session-id` by autonomous Step 3 / Phase C9.9 runner claim and by `session-save-id.sh` four-witness compact match). An observer writing its own SID there desyncs the pair and breaks `/stop`'s runner/observer detection. Observer writes the per-session binding only.

   **HALT ON EMPTY MIND_SID** — if output contains `ERROR:EMPTY_MIND_SID`, STOP and display the same error message shown in IDLE Step 0 below.

   **HALT ON SID_COLLISION** — if output contains `ERROR:SID_COLLISION`, STOP and display the same SID_COLLISION message shown in IDLE Step 0 below.

   The PreToolUse[Bash] hook will auto-inject `MIND_AGENT=<agent-name>` on subsequent Bash calls. Write `MIND_AGENT=<other> <cmd>` explicitly if you need a cross-agent probe.

0.4. **Write Body manifest** (FORK-BODY, Phase 1B — g-306-62): record this
   observer session as a Body of the Mind. Observers are read-only and NEVER
   fork the WM (`--role observer` → `forked_wm_hash: null`, no body-WM-file),
   so Phase 1A routing stays agent-wide — this is purely a manifest record that
   a Body exists. On close it will be marked `closed-pending-merge` by stop-hook
   (wired in Phase 1C — g-306-63, where the body-merge consumer lands); in Phase
   1B nothing consumes `body_state`, so a lingering `active` observer manifest is
   fully INERT for routing. Fail-open — a manifest write must never block the bind.
   Bash: `bash core/scripts/body-manifest.sh write --sid "$MIND_SID" --agent "<agent-name>" --role observer >/dev/null || echo "[start] body-manifest write failed (non-fatal, Phase 1B inert)" >&2`

0.5. **Open session telemetry** (session-telemetry WP1, 2026-06-03): write the
   initial `status=active` record so this observer session is visible in the
   live-sessions view BEFORE it closes (the close is WP2 in /stop's IDLE branch).
   World is already configured here (the agent is RUNNING), so WORLD_DIR resolves
   and the record lands at world/telemetry/session-records/<agent-name>/$MIND_SID.json.
   write_open is idempotent (returns without clobbering if the record exists) and
   never raises. `<target-mode>` is the observer mode (reader/assistant). guard-165:
   SID/agent/mode via ENV, python source single-quoted. `py -3` (Bash-tool context).
   Fire-and-forget (|| true) — telemetry must never block the bind.
   Bash: `TSID="$MIND_SID" TAGENT="<agent-name>" TMODE="<target-mode>" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_open; write_open(sid=os.environ["TSID"], agent=os.environ["TAGENT"], mode=os.environ["TMODE"], started_by="claude-code")' >/dev/null 2>&1 || true`

1. **Ensure daemon is running** (fail-open):
   - Bash: `bash core/scripts/mind-api-start.sh || echo "[start] daemon-start failed (non-fatal)" >&2`

2. **Skip all state-writing scripts** — do NOT call:
   - `session-mode-set.sh` (would overwrite autonomous mode)
   - `session-state-set.sh` (state stays RUNNING for the runner)
   - `session-persona-set.sh` (would interfere with runner)
   - `owncloud-pull.sh` (the IDLE branch's Step 2.6 continuity pull is
     INTENTIONALLY omitted here): an observer coexists with a live autonomous
     runner that is actively writing continuity files (handoff.yaml,
     working-memory.yaml, ...). A pull would download S3 over those files and
     race the runner's in-flight writes — the same reason observers skip every
     other state write. The observer reads whatever local state exists. If an
     observer on a freshly-moved machine sees stale data, the correct path is
     `/stop` (which routes through IDLE and runs Step 2.6) — or, for a crashed
     runner, the zombie auto-recovery flips to IDLE and Step 2.6 fires there.

3. **Mode-specific setup:**

   **Reader observer:**
   - Invoke `/prime` (with `--read-only` context — reader mode)
   - Load mode instructions: Read `core/config/modes/reader.md`
   - Output: "Reader mode active (observer). The autonomous loop continues in its session. I have read-only access to all accumulated knowledge. Ask me anything."

   **Assistant observer:**
   - Invoke `/prime`
   - Load mode instructions: Read `core/config/modes/assistant.md`
   - Output: "Assistant mode active (observer). The autonomous loop continues in its session. I can learn when you teach me — give me directives like 'learn about X' or 'remember that Y'.\n\n⚠ Note: Concurrent writes to working memory or the knowledge tree may conflict with the running loop. Reader mode is safer for observation only."

### IDLE (agent-state contains "IDLE")

0-pre. **Interrupted-stop check (FW-11, g-317-09 / g-317-14)**

   Before any binding or resume work, detect an autocompact-interrupted graceful
   stop so its consolidation/handoff is not lost when the user re-engages via
   `/start` instead of a chat message. This is the explicit-resume twin of the
   Session Start Protocol IDLE-branch check (CLAUDE.md, g-317-09): the passive
   session-start path probes the same sentinel, but a user who runs `/start`
   would otherwise skip straight to the IDLE→RUNNING flip below and strand the
   half-finished stop's learning.

   Bash: `MIND_AGENT=<agent-name> bash core/scripts/stop-checkpoint.sh resume-needed`

   The explicit `MIND_AGENT=` prefix is REQUIRED — the Phase 2.6 binding is not
   written until Step 0 below, so the PreToolUse[Bash] auto-inject hook cannot
   resolve the agent yet; `stop_checkpoint.py` reads the agent from `MIND_AGENT`.

   - **Exit 0** (a `stop-checkpoint.json` is present — a prior `/stop` was
     interrupted mid-sequence, most often during the long D4 consolidate):
     - Read the current on-disk mode: Bash:
       `MIND_AGENT=<agent-name> bash core/scripts/session-mode-get.sh`
     - **If current mode == `autonomous`** (the interrupted stop never reached
       D7 — D7 is what sets the post-stop mode, and D4 consolidate runs before
       D7, so a still-`autonomous` mode means the consolidation/handoff may be
       incomplete): invoke `/aspirations-graceful-stop --resume`. That handler
       idempotently completes the remaining stop obligations (consolidate,
       handoff, set target mode, clear the checkpoint), emits its own
       stop-complete message, and ends the turn. DONE — do NOT proceed to Step 0.
       After it completes, display:
       > Detected an autocompact-interrupted graceful stop and completed its
       > consolidation/handoff first so no learning is lost. Re-run
       > `/start <agent-name> [--mode <mode>]` to resume.
     - **Else** (current mode is already `assistant`/`reader` — the interrupted
       stop substantially completed; D7 ran, so D4 consolidate already landed,
       and only the D7.1 checkpoint-clear was missed): Bash:
       `MIND_AGENT=<agent-name> bash core/scripts/stop-checkpoint.sh clear`
       to retire the stale sentinel, then continue to Step 0 normally.
   - **Exit 1** (the common case — no interrupted stop, or the resume-count
     breaker has tripped per `stop_checkpoint.py` MAX_RESUME_ATTEMPTS): continue
     to Step 0 normally.

0. **Rebind Agent to Session**

   Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; bash core/scripts/sid-collision-check.sh "<agent-name>" "$MIND_SID" || { echo "ERROR:SID_COLLISION"; exit 2; }; bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent "<agent-name>" --mode "<target-mode>" --retire-legacy >/dev/null && echo "BOUND:$MIND_SID" && printf '\n╔════════════════════════════════════════════════════════════╗\n║                                                            ║\n║    ✓  RACE_WINDOW_CLOSED                                   ║\n║       Safe to /start another agent in another terminal     ║\n║                                                            ║\n╚════════════════════════════════════════════════════════════╝\n\n'`

   `$MIND_SID` is the authoritative this-session SID, injected by the PreToolUse[Bash] hook (`core/scripts/bash-agent-inject.py`). See `guard-341`.

   `<target-mode>` substitution: the LLM substitutes the determined target mode (from the `--mode` flag, or `autonomous` by default) when emitting this Bash call. Mode determination happens implicitly at /start parse time — by the time the LLM reads this line, the target mode is known.

   Phase 2.6: the binding lives at `agents/<agent-name>/sessions/$MIND_SID/binding.yaml` — full intel (agent, mode, started_at, started_by). The dir name IS the SID. Multiple sessions for one agent appear as multiple sibling dirs under `agents/<agent-name>/sessions/`. The `--retire-legacy` flag deletes any pre-Phase-2.6 `.active-agent-<SID>` file at PROJECT_ROOT.

   The trailing `RACE_WINDOW_CLOSED` line is the explicit "safe to /start the next agent" signal — once the binding is committed, no concurrent `/start` in another terminal can interfere with THIS agent's identity. The remaining work (`/prime`, mode setup, `/boot`, aspirations loop) only touches per-agent files (different agent dir) and properly-locked shared files (`world/team-state.yaml` via `locked_modify_yaml`, `world/board/*.jsonl` via `locked_append_jsonl`, `world/aspirations.jsonl` via `locked_modify_jsonl`). Users who want to start multiple agents in parallel: when you see `RACE_WINDOW_CLOSED`, open the next terminal and `/start <other>` immediately — no wait needed.

   `sid-collision-check.sh` refuses the bind when the binding for `$MIND_SID` already binds to a DIFFERENT agent whose runner is alive (`agent-state=RUNNING` AND heartbeat fresh). It is aware of both Phase 2.6 `agents/*/sessions/<SID>/binding.yaml` and legacy `.active-agent-<SID>` layouts. Closes the `claude --continue`/`--resume` SID-reuse path that silently overwrites a live binding (2026-05-12 zeta-bravo cross-binding incident). DO NOT add a `--force` override — the platform offers `claude --fork-session` for the user's explicit "I want a separate session" intent.

   **DO NOT ADD** a write to `agents/<agent-name>/session/latest-session-id` here. That file is runner-owned: for reader/assistant modes it must not be written by non-runner sessions (same rule as observer Step 0 above); for autonomous mode, Step 3 below performs the atomic pair-write of `latest-session-id` + `running-session-id` together — that is the single canonical runner-claim site.

   **HALT ON EMPTY SID** — if output contains `ERROR:EMPTY_MIND_SID`, STOP. Do NOT proceed to Step 1. Display to the user:
   > Cannot bind this terminal to agent `<agent-name>`: the PreToolUse[Bash] hook did not inject `MIND_SID`. This usually means the hook timed out or failed silently. Check `.claude/settings.json` for the hook registration, then close this terminal, open a new one, and retry `/start <agent-name>`.

   **HALT ON SID_COLLISION** — if output contains `ERROR:SID_COLLISION`, STOP. Do NOT proceed to Step 1. Display to the user (substituting the bound agent name from the stderr `bound_to=` field):
   > Cannot bind this terminal to agent `<agent-name>`: session_id `$MIND_SID` is already bound to a DIFFERENT agent (whose runner is currently RUNNING with a fresh heartbeat).
   >
   > This terminal likely ran `claude --continue` or `claude --resume` and inherited a live session's id. Two windows sharing one session_id will corrupt both agents' bindings and silently kill the autonomous loop.
   >
   > **Fix**: close this terminal AND relaunch Claude Code with `claude --fork-session` (which forces a new session_id). Or `/stop` the other agent first.

   The PreToolUse[Bash] hook will auto-inject `MIND_AGENT=<agent-name>` on subsequent Bash calls from the binding file. Write `MIND_AGENT=<other> <cmd>` explicitly if you need a deliberate cross-agent probe (the hook detects and preserves explicit overrides).

0.4. **Write Body manifest** (FORK-BODY, Phase 1B — g-306-62) — reader/assistant
   modes ONLY here. These observer-class sessions are recorded now with
   `--role observer` (never forks; INERT for routing). For **autonomous** mode
   the Body manifest is written LATER with `--role worker`, AFTER the
   running-session-id claim (Step 3 below) — so the reducer-aware fork decision
   reads the freshly-claimed running-session-id. Writing it here for autonomous
   would read a stale/empty running-session-id and could wrongly fork the
   reducer, so autonomous is intentionally skipped at this site. Fail-open.
   - IF target-mode is `reader` or `assistant`:
     Bash: `bash core/scripts/body-manifest.sh write --sid "$MIND_SID" --agent "<agent-name>" --role observer >/dev/null || echo "[start] body-manifest write failed (non-fatal, Phase 1B inert)" >&2`
   - IF target-mode is `autonomous`: skip — the worker FORK-BODY step after the
     runner claim (Step 3) writes it.

0.5. **Open session telemetry** (session-telemetry WP1, 2026-06-03): write the
   initial `status=active` record for THIS session so it is visible in the
   live-sessions view before it closes. The matching close is WP3 (graceful-stop
   D6.6) for the autonomous runner path, or WP2 (/stop IDLE branch) for a
   reader/assistant start — both finalize the same world/telemetry/session-records/<agent-name>/$MIND_SID.json
   record. World is already configured here (resumed/existing agent), so WORLD_DIR
   resolves. `<target-mode>` is the determined target mode (the same value the
   binding-write above used — `reader`, `assistant`, or `autonomous`). For the
   autonomous path the runner claim (Step 3) writes latest-session-id == $MIND_SID,
   so the close targets this same record. write_open is idempotent and never
   raises. guard-165: SID/agent/mode via ENV, python source single-quoted.
   `py -3` (Bash-tool context). Fire-and-forget (|| true).
   Bash: `TSID="$MIND_SID" TAGENT="<agent-name>" TMODE="<target-mode>" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_open; write_open(sid=os.environ["TSID"], agent=os.environ["TAGENT"], mode=os.environ["TMODE"], started_by="claude-code")' >/dev/null 2>&1 || true`

1. Determine target mode:
   - If `--mode` flag provided: use that mode
   - Else: `autonomous` (always — regardless of previous mode)

2. Set mode: Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-mode-set.sh <target-mode>`

   **HALT ON NON-ZERO EXIT (g-115-1032, 2026-05-21)** — if `session-mode-set.sh`
   exits non-zero, STOP. Do NOT proceed to step 2.5 / mode-specific branches.
   Mode signal write failed; disk default (absence of `agent-mode`) is reader
   per CLAUDE.md "Mode System". Without this halt, an assistant-mode `/start`
   that silently failed the mode-set would land the agent in reader mode (the
   disk default), and the mode-specific branch below would emit a misleading
   "Assistant mode active" output while the agent's persona/loop/write
   capabilities silently mismatch the user's stated intent. Display:
   > Cannot transition agent `<agent-name>` to mode `<target-mode>`
   > (session-mode-set.sh exited non-zero). Investigate stderr above and
   > retry `/start <agent-name> --mode <target-mode>`.

   The explicit `MIND_AGENT=` prefix is belt-and-suspenders: it bypasses the
   PreToolUse[Bash] hook's auto-inject path, so even if the hook is still
   cold-starting Python and times out, the script still receives the env var.
   Without this, the very first Bash call after writing `.active-agent-<SID>`
   can race the hook and fail with "no agent active (MIND_AGENT not set)".

2.5. Clear stale stop signals (runs for ALL modes — this is the single cleanup
   site for stop-requested / stop-loop on IDLE entry):
   Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-signal-clear.sh stop-requested`
   Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-signal-clear.sh stop-loop`

   Hoisted here so a partial /stop that left `stop-requested` on disk (e.g., the
   session was closed after graceful-stop D1 ran but before D3 cleared it)
   doesn't survive into reader or assistant mode. State is already IDLE, so no
   loop polling could be interrupted; clearing is purely hygienic. Do NOT also
   do this clear in the autonomous sub-path below — it's already done HERE.
   Do NOT add this clear to the RUNNING observer branch above — observers MUST
   NOT touch signal files (that's the observer contract).

2.6. Pull continuity files from S3 (own-cloud machine-move resume — runs for ALL modes):
   Bash: `MIND_AGENT=<agent-name> bash core/scripts/owncloud-pull.sh --agent "<agent-name>" || echo "[owncloud-pull] WARN: continuity pull returned non-zero — resuming from local state (may be stale on a just-moved machine; the periodic sweep reconciles next tick)"`

   The read-side complement of /stop's D6.7 flush (session-continuity redesign,
   2026-06-02). Under the own-cloud backend this materializes the agent's
   continuity-tier session files (handoff.yaml, working-memory.yaml,
   execution-diary.jsonl, reasoning-snapshot.yaml, pending-questions.yaml, ...)
   from S3 to local NOW — BEFORE Step 3's `/prime` (reader/assistant) or `/boot`
   (autonomous) does its raw Read of those files. Without it, an agent moved to a
   new machine would resume from a STALE or absent local copy and lose everything
   the previous machine learned. The endpoint (owncloud_sync.pull_continuity) is
   freshness-aware: it NEVER clobbers a local file carrying unpushed local writes
   (the same-machine crash-restart case) — the manifest baseline gates every
   overwrite. Under the local backend it is a clean no-op.

   Placement rationale: runs for ALL modes after the binding (Step 0) and
   mode-set (Step 2) but while state is still IDLE — so it is OUTSIDE the
   autonomous "nothing stoppable between RUNNING and /boot" critical section
   (it precedes the RUNNING flip at the autonomous sub-path below), and it runs
   BEFORE the autonomous `wm-set session_start` so this session's stamp lands on
   the freshly-pulled working-memory rather than being clobbered by the pull.
   Non-blocking (`|| echo WARN`): a pull error must never block /start — local
   state is the fallback and the daemon's periodic sweep reconciles. The daemon
   need not be up yet (the wrapper auto-spawns it); Step 3's `mind-api-start.sh`
   is then a no-op.

3. Based on target mode:

   **Reader mode:**
   - Bash: `bash core/scripts/mind-api-start.sh || echo "[start] daemon-start failed (non-fatal)" >&2`
     (Ensure runtime daemon is up. Fail-open — same rationale as autonomous mode above.)
   - Set persona: Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-persona-set.sh true`
   - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
     (Populates the WM top-level `session_start` key declared in wm.py:41.
     Consumed by `session-artifacts-count.sh` to bound "created-since-session-start"
     counts for the productivity-stop-gate encoding_ratio; the helper exits 2
     on unset, which makes the gate treat total artifacts as 0. Seeded here on
     every /start entry; preserved across autocompact via
     `wm.py::SESSION_IDENTITY_FIELDS`; cleared by /stop's
     `wm-clear-identity.sh` (graceful-stop D4.5).)
   - Invoke `/prime` (with `--read-only` context — reader mode)
   - Load mode instructions: Read `core/config/modes/reader.md`
   - Output: "Reader mode active. I have access to all accumulated knowledge. Ask me anything."

   **Assistant mode:**
   - Bash: `bash core/scripts/mind-api-start.sh || echo "[start] daemon-start failed (non-fatal)" >&2`
     (Ensure runtime daemon is up. Fail-open — same rationale as autonomous mode above.)
   - Set persona: Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-persona-set.sh true`
   - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
     (Same rationale as reader mode above — populates the WM top-level
     `session_start` key consumed by `session-artifacts-count.sh`.)
   - Invoke `/prime`
   - Load mode instructions: Read `core/config/modes/assistant.md`
   - Output: "Assistant mode active. I can learn when you teach me — give me directives like 'learn about X' or 'remember that Y'."

   **Autonomous mode:**
   (stop-requested / stop-loop already cleared in Step 2.5 above — don't repeat.)
   - Bash: `bash core/scripts/mind-api-start.sh || echo "[start] daemon-start failed (non-fatal — wrapper auto-spawn is fallback)" >&2`
     (Ensure runtime daemon is up before any wrapper call. Idempotent — no-op
     if already running. Fail-open: if spawn fails, the wrapper layer's
     rt_ensure_running handles it on first call. Do NOT make /start fail on
     daemon spawn failure.)
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/output-style-gate.sh --mode autonomous`
     (Layer-B gate for guard-454 / rb-629 — refuses autonomous mode + Explanatory
     output style, the documented loop-killer combo. Wrapper detects `outputStyle`
     from `.claude/settings.local.json` and forwards to
     `world/scripts/output-style-mode-guard.sh`. Fail-open if any precondition
     is missing. Runs BEFORE any state mutation so a refusal leaves agent-state
     untouched. Exit codes: 0=proceed, 2=REFUSE, 3=override accepted.)
     - On exit 2 (REFUSE): STOP /start. Display:
       > Layer-B gate refused autonomous mode + Explanatory output style
       > (guard-454, rb-629 — known silent-loop killer). Either run
       > `/output-style default` and re-issue `/start <agent-name>`, or call
       > the gate directly with `--override "<justification>"` to audit-log
       > and proceed.
     - On exit 3 (override accepted): proceed; the gate logged an audit entry
       to `world/output-style-overrides.jsonl`.
     - On exit 0: proceed normally.
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/heartbeat-tick.sh --bypass-state`
     (Seeds `runner-heartbeat` mtime AND stamps team-state `last_active` NOW
     in one call — heartbeat-tick.sh writes both. MUST precede the RUNNING
     transition to close the observer-probe race (state=RUNNING with a stale
     heartbeat/last_active from the previous session). Liveness is pure mtime
     — see `core/config/conventions/compact-recovery.md`. DO NOT add a separate
     `team-state-update.sh ... last_active ...` line here; it duplicates the
     write heartbeat-tick just performed. `--bypass-state` is REQUIRED because
     state is still IDLE at this point — the heartbeat MUST seed before the
     RUNNING flip per rb-323; the gate in `heartbeat-tick.sh` refuses bare
     ticks against IDLE state to prevent the `heartbeat_without_running`
     desync class (alpha incident 2026-05-13 cbb27ab3).)
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/team-state-update.sh --field "agent_status.<agent-name>.current_focus" --value "\"\"" || true`
     (Clear stale current_focus from the previous session's shutdown — without this,
     a partner reading team-state.yaml sees the prior session's "session ended" or
     stale focus value indefinitely. Convention coordination.md:275 is retrospective
     ("set on completion"), so we can't write a prospective "starting" — clearing to
     "" is the convention-aligned signal of "no completion yet this session". The
     first aspirations-state-update or aspirations-consolidate write populates
     current_focus with the first real completion. Fail-open with `|| true` so a
     team-state write failure never blocks the RUNNING transition — stderr is NOT
     suppressed, so write errors surface.)
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/team-state-update.sh --field "agent_status.<agent-name>.session_ended" --value "false" || true`
     (Clear stale session_ended from the previous /stop — g-240-72. Without this,
     a partner reading team-state.yaml sees session_ended=true for a live agent
     and may make wrong concurrency assumptions. /stop sets session_ended=true on
     graceful exit; the field then persists until the next /start. We clear here
     instead of removing the field so the absence-vs-false distinction stays
     unambiguous to partner readers. Same fail-open semantics as current_focus
     above — write failure must not block the RUNNING transition.)
   - Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; RUNNER_TOKEN=$(py -3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null || python3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null); [ -n "$RUNNER_TOKEN" ] || { echo "ERROR:RUNNER_TOKEN_GEN_FAILED"; exit 3; }; AGENT_STATE_DIR="agents/<agent-name>/session"; mkdir -p "$AGENT_STATE_DIR" && echo "$MIND_SID" > "$AGENT_STATE_DIR/running-session-id.tmp" && mv "$AGENT_STATE_DIR/running-session-id.tmp" "$AGENT_STATE_DIR/running-session-id" && echo "$MIND_SID" > "$AGENT_STATE_DIR/latest-session-id.tmp" && mv "$AGENT_STATE_DIR/latest-session-id.tmp" "$AGENT_STATE_DIR/latest-session-id" && echo "$RUNNER_TOKEN" > "$AGENT_STATE_DIR/runner-token.tmp" && mv "$AGENT_STATE_DIR/runner-token.tmp" "$AGENT_STATE_DIR/runner-token" && echo "RUNNER_TOKEN=$RUNNER_TOKEN"`
     (Canonical runner-claim: writes THREE files atomically — `running-session-id`, `latest-session-id`, and `runner-token` — into `agents/<agent-name>/session/`. The Phase 2.5.D `agents/` parent prefix MUST be in the heredoc path; without it, the writes land at `agents/<agent-name>/session/` at PROJECT_ROOT (the 2026-05-19 bravo/ cruft incident — the L1 hook only gates Write/Edit, not Bash heredoc writes, so a missing `agents/` prefix silently creates a directory at the wrong root). The first two files hold the Claude Code SID (routing identity used by stop-hook). The third is a FRAMEWORK-OWNED UUID4 (uniqueness identity) — protects against Claude Code reusing a session_id across windows via `claude --continue` / `--resume`. With the token, every BLOCK and watchdog event records the runner-instance identity, so a SID-collision shows up as "same SID, different runner-token" in `core/logs/stop-hook.log` and watchdog events instead of silent corruption. The 2026-05-12 cross-binding incident was invisible to forensics without this signal. DO NOT split these writes into separate Bash commands — the triple-write is the atomic unit. DO NOT remove the `RUNNER_TOKEN_GEN_FAILED` halt; without a token, the loop runs with no uniqueness anchor. Per rb-323/guard-403, observer-paired signals MUST be seeded BEFORE the state-set RUNNING below — same race rb-323 identified for heartbeat-tick. If RUNNER_TOKEN_GEN_FAILED here, state stays IDLE (clean retry); if state-set ran first, state would be RUNNING with no SID files and Path B would have to recover.)

     **HALT ON RUNNER_TOKEN_GEN_FAILED** — if output contains `ERROR:RUNNER_TOKEN_GEN_FAILED`, STOP. Both `py -3` and `python3` failed to generate a UUID. Display to the user:
     > Cannot start agent `<agent-name>`: the framework-owned runner-token could not be generated (Python unavailable). Check that `py -3` or `python3` works; the runner-token is required for SID-collision detection.

   - **Write Body manifest** (FORK-BODY reducer, Phase 1B — g-306-62 / g-330-03): record this
     autonomous session as the Reducer Body, AFTER the running-session-id claim above
     so the reducer-aware fork decision reads the just-claimed running-session-id.
     This is the CLAIM-REDUCER / FORK-BODY split: the triple-write above is
     CLAIM-REDUCER (the existing single-runner claim — running-session-id ==
     $MIND_SID now), and this is FORK-BODY (always writes the manifest). Because
     this session IS the reducer (`role: reducer`), FORK-BODY does NOT fork the
     WM (`forked_wm_hash: null`, no body-WM-file) — Phase 1A routing stays
     agent-wide, byte-identical to pre-Phase-1B behavior. Only a future 2nd+
     worker (a Body started while a DIFFERENT reducer holds running-session-id)
     forks. `reducer_sid` is null in the manifest (this IS the reducer; workers
     and observers populate it from running-session-id). The manifest `role` is
     consumed Mind-side ONLY — it coordinates which terminal owns encode/reflect
     on the shared tree; no external service reads it. Safe before the state flip
     (per-session file write, touches no
     observer-paired signal — keeps the RUNNING→/boot critical section empty per
     the F4 rationale below). Fail-open — a manifest write must never block the
     RUNNING transition.
     Bash: `bash core/scripts/body-manifest.sh write --sid "$MIND_SID" --agent "<agent-name>" --role reducer >/dev/null || echo "[start] body-manifest write failed (non-fatal, Phase 1B inert)" >&2`

   - Bash: `rm -f agents/<agent-name>/session/iteration-checkpoint.json agents/<agent-name>/session/compact-pending agents/<agent-name>/session/compact-checkpoint.yaml`
     (F4 reorder, 2026-05-20: moved BEFORE the state-set RUNNING below so the
     critical section between RUNNING and /boot is truly empty. These are
     pure-Bash cleanups of stale per-session files; safe to run at IDLE.)
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-signal-clear.sh loop-active`
     (F4 reorder: same rationale — moved before the state flip.)
   - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
     (F4 reorder: moved before the state flip — wm-set is a pure-Bash write
     of a top-level WM key, safe to run at IDLE. Populates the WM top-level
     `session_start` key declared in wm.py:41. Consumed by
     `session-artifacts-count.sh` to bound artifact counts for the
     productivity-stop-gate encoding_ratio. Seeded HERE on /start IDLE→autonomous;
     /boot does NOT re-seed on autocompact restart because
     `wm.py::SESSION_IDENTITY_FIELDS` makes cmd_reset preserve session_start
     across the consolidate→wm-reset→/boot chain — the value survives the
     autocompact boundary without a re-seed. /stop clears it explicitly
     via `wm-clear-identity.sh` in graceful-stop D4.5 — the ONE authorized
     clear site.)
   - **DDB runner-claim acquire (single-runner lifecycle, design §4).**
     Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-claim.sh acquire --agent <agent-name>; echo "ACQUIRE_RC=$?"`
     (Cross-machine half of single-runner enforcement: a conditional DDB IDLE->RUNNING
     claim taken just BEFORE the local state-set below, using the runner-token from the
     triple-write above. Under STORAGE_BACKEND=own-cloud the daemon does the real DDB
     CAS; on any other backend runner-claim.sh no-ops (exit 0). **HALT ON ACQUIRE_RC=4**:
     a peer machine holds a live DDB claim for
     `<agent-name>` — do NOT proceed to session-state-set RUNNING; state stays IDLE
     (the observer-paired signals seeded above are harmless at IDLE). Display: "Cannot
     start `<agent-name>` in autonomous mode — another machine holds a live runner
     claim (DDB session-lock); stop the other runner or wait ~OWNERSHIP_STALE_SECONDS."
     Any OTHER non-zero rc (1 = daemon/DDB error) is FAIL-OPEN: log and PROCEED — a
     transient DDB hiccup must not block a legitimate start.)
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh RUNNING`
     (State flip — observable to /stop, recovery-gate, partner agents. Per rb-323/guard-403, this MUST be the last write in the RUNNING-claim sequence: every observer-paired signal — heartbeat above, triple-write directly above — is seeded first, so the invariant "state=RUNNING implies fresh heartbeat AND non-empty SID files" holds from the transition moment.)

     **HALT ON NON-ZERO EXIT (F1, 2026-05-20)** — if the script exits non-zero
     (write permission error, _paths.sh resolution failure, daemon endpoint
     error, etc.), STOP. Do NOT proceed to `/boot`. State remains IDLE; the
     observer-paired signals seeded above are harmless (fresh heartbeat at
     IDLE means nothing to observers). Display:
     > Cannot transition agent `<agent-name>` to RUNNING (session-state-set.sh
     > exited non-zero). The agent stays IDLE; investigate stderr above and
     > retry `/start <agent-name>`. Without this halt, `/boot` would read
     > state=IDLE and abort with "Agent is stopped" — confusing failure mode.
   - (Watchdog setup is no longer needed at /start. The agent-watchdog runs
     as a periodic probe from `iteration-close.sh` productivity-check phase —
     `agent-watchdog.py --tick`. State persists across iterations via
     `agents/<agent>/session/watchdog-prev-state.json`. Cross-platform; no daemon,
     no PID file, no /start spawn. Add new probes to `build_probes()` in
     `core/scripts/agent-watchdog.py`.)
   - Output: "Agent resumed. Learning loop starting."
   - Invoke `/boot`

### UNINITIALIZED (agent-state doesn't exist or <agent>/ doesn't exist)

The UNINITIALIZED first-boot ceremony (Phase A-0 transplant detection, Phase A
agent-name binding, Phase B path configuration, the B0.5/C0.5 bootstrap gates,
and program.md setup) is extracted to an on-demand digest so this hot-path
SKILL.md stays lean: it runs ONLY on brand-new agent creation, never on the
common IDLE->RUNNING resume path, yet its ~627 lines were loaded into context on
EVERY /start invocation (the boot-footprint that g-115-1723 targets).

**Read `core/config/start-uninitialized-ceremony.md` and follow it in order**
(Phase A-0 -> A -> B -> B0.5 -> C0 -> C0.5 -> program.md), THEN continue to the
Phase C dispatch immediately below for the mode-specific first-boot flow.



Phase C then adapts based on mode. The three mode-specific first-boot flows
(Reader / Assistant / Autonomous, steps C1.9-C11) are extracted to an on-demand
digest so this hot-path SKILL.md stays lean: they run ONLY during UNINITIALIZED
first-boot, never on the common IDLE->RUNNING resume path, yet their ~530 lines
were loaded into context on EVERY /start invocation (the boot-footprint that
g-115-1723 targets).

**Read `core/config/start-phase-c.md` and follow the section matching the mode
confirmed above** (Reader / Assistant / Autonomous). That digest holds the full
C1.9-C11 first-boot sequence for all three modes.

## Chaining
- Calls: /boot (autonomous mode), /prime (all modes during init; reader/assistant resume)
- Called by: User only. NEVER by Claude.
