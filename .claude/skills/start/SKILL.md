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

   `_fileops.locked_append_jsonl` creates the file if missing and uses the
   same lockfile protocol as every other JSONL writer in the framework —
   single source of truth for race-safe appends. Then proceed to cleanup.

   **Origin (g-115-947, 2026-05-19)**: replaced the single-signal heartbeat-stale
   precondition with the 6-condition helper. Prior to this, `/start --recover`
   only checked `heartbeat-stale.sh`, while `recovery-gate.sh` and the
   auto-recovery branch used 6 conditions (g-115-492 multi-signal + g-115-494
   diary freshness). A non-runner terminal running `/start --recover` during
   a heartbeat-stale window (common between iterations) could stomp the live
   runner's `running-session-id` — canonical incident 2026-05-18 (alpha
   session 4334f019 vs d964c395). The 6-condition gate closes that gap.

**Both Step 1's `session-state-get.sh` call and the `runner-dead-check.sh` helper MUST use the explicit `MIND_AGENT=<agent-name>` prefix.** Step 0.7 runs BEFORE the session-binding rewrite in the IDLE branch Step 0, so `.active-agent-<SID>` may not exist or may point at a different agent. Without the prefix the scripts fall back to `_paths.sh`'s first-available-conf loop and would probe the wrong agent.

If preconditions pass, run cleanup in order. Recovery is **manifest-driven** —
the authoritative list of files to clear lives in `core/config/session-manifest.yaml`
(see `core/config/conventions/session-state.md` → "Session File Manifest").
Adding a new transient session file ONLY requires an entry in that YAML; this
recovery block picks it up automatically.

Ordering (g-115-683, 2026-05-13, inverse of rb-323/guard-403): state-set
IDLE fires BEFORE manifest-clear so observers never see state=RUNNING +
sid=missing during the cleanup window. If state-set fails, manifest-clear is
SKIPPED — leaves state=RUNNING + sid present (normal RUNNING, recoverable
on next SessionStart) rather than the half-recovered zombie state (sid gone
+ state=RUNNING) that the previous ordering produced. Mirrors
aspirations-graceful-stop D1→D6 pattern (state flip first, observer cleanup
after).

- Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`

  If `session-state-set.sh` exits non-zero, fail loud — do NOT fall through.
  A failed state-set is recoverable on next SessionStart because manifest-
  clear has not yet fired (observer signals + SID still present, state still
  RUNNING — a normal RUNNING state). This matches the loud-fail discipline
  in recovery-gate.sh's `_perform_recovery` and the auto-recovery branch
  under "RUNNING + requested mode is autonomous" — three parallel cleanup
  sites, all loud, all state-set-first.
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

Distinguish them via the 6-condition zombie gate. **The six conditions and
the exact probe scripts MUST match `core/scripts/runner-dead-check.sh`**
(g-115-947 canonical helper, 2026-05-19) — that helper is the single source
of truth, and both `core/scripts/recovery-gate.sh` (`run_gate_for_agent`)
and `/start --recover` Step 0.7 defer to it. This LLM-orchestrated section
is the THIRD parallel implementation: it stays inline rather than calling
the helper because each branch below produces a different user-facing
message (live runner detected / safety hold-back with per-condition cause /
auto-recover success), and a bash helper that emits JSON cannot drive the
LLM's per-branch user prose. Any change to the 6 conditions MUST update
BOTH `runner-dead-check.sh` AND the inline checks below — they cannot
diverge without producing inconsistent recovery behavior between the
silent-script and LLM paths. The
gate fires when ALL SIX hold:
  (1) `agent-state == RUNNING`            (already established by Step 1)
  (2) `heartbeat-stale.sh` returns `stale`
  (2.5) `runner-recent-block.sh` returns 1 (no BLOCK in last 5 min)
  (2.7) `execution-diary.jsonl` mtime older than 15 min (DIARY_STALE_MINUTES)
  (3) `stop-requested` is NOT set
  (4) `background-jobs.sh has-pending` exits 1 (no Tier-A registered job)

Condition 2.5 is the multi-signal liveness fix (g-115-492). Heartbeat
staleness alone is too weak when transient platform issues (e.g., Claude
Code 2.1.133 stop-hook timeouts) cause heartbeat to stale even though the
runner is alive. A recent BLOCK in `core/logs/stop-hook.log` proves stop-hook
fired AND the loop re-entered — three events that don't happen on a
dead runner. Without 2.5, the 2026-05-09 cross-binding stomp recurs.

Condition 2.7 is the second multi-signal liveness fix (g-115-494). When
stop-hooks are intermittently timing out, the runner can stay alive
(processing user messages, background-task notifications) but the
heartbeat-tick path is not reliably executed. `execution-diary.jsonl`
is appended at every Phase start/end (sub-minute granularity) AND survives
stop-hook interruptions because phase-end writes BEFORE the LLM yields the
turn — making diary mtime a more reliable liveness signal than heartbeat
when stop-hooks are flaky.

When all four hold, `/start` auto-recovers inline so the user can simply
re-run `/start <agent-name>` without a manual `--recover` ceremony. The
explicit `--recover` / `--recover --force` paths still exist for cases where
the gate holds back (heartbeat fresh but runner is wedged, or stop-requested
set, or background-job is registered but actually orphaned).

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

**IF output is `fresh`** → live runner detected (scenario 1). DO NOT auto-recover —
that would clobber an active loop. Output:

```
⚠ Agent '<agent-name>' is in autonomous mode (RUNNING state), heartbeat fresh.

Another window is running the loop. Only one autonomous session per agent is
allowed.

To recover:
  1. If another window is running the loop — run /stop <agent-name> there
  2. If you believe the runner is wedged despite a fresh heartbeat — force
     recovery with: /start <agent-name> --recover --force

Or open a read-only/assistant window alongside the running loop:
  /start <agent-name> --mode reader
  /start <agent-name> --mode assistant
```

DONE. No state changes. No-op.

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
branch and as recovery-gate.sh's `_perform_recovery`. Ordering (g-115-683,
2026-05-13, inverse of rb-323/guard-403): state-set IDLE fires BEFORE
manifest-clear so observers never see state=RUNNING + sid=missing during
cleanup. If state-set fails, manifest-clear is SKIPPED to keep the agent in
a recoverable normal-RUNNING state instead of a half-recovered zombie.

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

**Phase A-0: Transplant-Resume Detection (cloned agent landing on a new machine)**

UNINITIALIZED has TWO causes that need OPPOSITE handling:
- **Genuine first run** — brand-new agent, nothing on disk → full init (Phase A/B/C).
- **Transplant/clone** — the agent dir arrived via `git clone` with its tracked
  content intact (`.initialized`, `self.md`, `aspirations.jsonl`,
  `curriculum.yaml`, journal/, experience/), but `session/agent-state` is absent
  because `session/` and `local-paths.conf` are gitignored (machine-local, never
  travel). Running full init here would re-elicit identity and **overwrite the
  cloned `self.md`/`curriculum.yaml`** → resume as an EXISTING agent instead.

`session-state-get.sh` only inspects `agent-state`, so it can't tell these apart.
The tracked `.initialized` marker can: it clones with the agent, so its presence
on an otherwise-UNINITIALIZED agent means "already initialized, just not started
on THIS machine."

Bash: `bash core/scripts/agent-resume-scaffold.sh "<agent-name>"; echo "rc=$?"`

The scaffold (idempotent, verified) writes a default `local-paths.conf` (local
own-cloud cache under `<cache-root>/<env-id>/`, override via `RUNTIME_CACHE_ROOT`)
and creates `session/`. It NEVER writes `agent-state`/`agent-mode` (those stay
/start's job — guard-340) and NEVER touches tracked content. Branch on rc:

- **rc=2** (no agent dir OR no `.initialized` — genuine first run): proceed to
  **Phase A** below. The rest of this UNINITIALIZED branch is unchanged.

- **rc=1** (scaffold error): STOP, display stderr, do not proceed (guard-372 — fail loud).

- **rc=0** (transplanted agent — scaffolded): do NOT run Phase A/B/C init.
  Resume it as an EXISTING agent. **Resume mode = the parsed `--mode` value
  (default `autonomous`)** — a transplant-resume is treated exactly like a bare
  `/start` on any IDLE agent: bare `/start <agent>` runs the loop; `--mode
  reader`/`assistant` is honored for the cautious first-boot-on-a-new-machine
  case. (The earlier reader-first default for bare transplant-resume was removed
  2026-06-04: it forced a two-step `/start` dance — first call landed reader,
  a second was needed to actually run — which the user found annoying. The
  dual-runner risk it guarded against is covered by the ownership warning below
  plus the own-cloud write lock.)

  IF the resume mode is `autonomous`, FIRST print this ownership warning, then
  proceed:
  "⚠ One machine per agent: starting `<agent-name>`'s autonomous loop here. If
  `<agent-name>` is still RUNNING on its origin machine, `/stop` it there NOW —
  two runners of the SAME agent on one own-cloud world claim and release each
  other's goals (the DDB lock prevents file corruption, not this semantic
  collision). Also confirm `.env.local` is configured for own-cloud (the one
  manual step — secrets never travel in git)."

  Steps:
  1. Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`
     (UNINITIALIZED→IDLE — the same init endpoint reader/assistant first-boot
     already uses; authorized /start path, see `.claude/rules/user-interaction.md`.
     **HALT ON NON-ZERO EXIT**: STOP, display stderr.)
  2. Execute the **IDLE branch** (Step 0 onward, above) with `<target-mode>` set
     to the resume mode. It binds the session, pulls world/meta from S3
     (own-cloud), primes, and — for `autonomous` — claims the runner and hands
     off to `/boot`. NO identity prompts, NO clobber of tracked content, any mode.
  3. After the IDLE branch's mode output, append this notice (substitute
     `<world_path>` from the rc=0 JSON and `<chosen-mode>`):
     "✓ Resumed transplanted agent `<agent-name>` in `<chosen-mode>` mode — cloned
     identity + memory intact; scaffolded only the machine-local session + paths
     (world cache: `<world_path>`). world/meta rehydrate from S3 on first daemon
     read.{IF chosen-mode is reader or assistant, append: " (Once the pull looks
     right and `<agent-name>` is `/stop`-ped on its origin machine, `/start
     <agent-name> --mode autonomous` runs the loop here.)"} If `.env.local`
     isn't set up for own-cloud yet — the one manual step, secrets never travel
     in git — do that first."

**Phase A: Agent Name and Session Binding**

The agent name from the `/start <name>` command becomes the directory name.
The agent directory must exist before path configuration (since `local-paths.conf` lives inside it).

A0. **Prerequisites Check** — verify the runtime environment BEFORE writing any state:

   Bash: `bash core/scripts/check-prerequisites.sh`

   The script verifies Python 3.8+, PyYAML, bash 4+ (required) and warns
   on missing git or psutil (optional — framework still works). On failure
   it prints one consolidated friendly error block with copy-pasteable fix
   commands and exits 2.

   **HALT ON FAILURE** — if exit code ≠ 0, STOP. Display the script's stderr
   to the user verbatim and do NOT proceed to A1. The user must install the
   missing prerequisites and re-run `/start <agent-name>`.

   Rationale: pre-2026-05-17 a fresh install would crash 4 scripts deep
   with a cryptic `ModuleNotFoundError: No module named 'yaml'` from
   init-meta.sh → meta-init.py. The wife (a non-technical user) had no
   path to diagnose this. The prerequisites check surfaces all missing
   pieces in one pass with one error message.

A1. Validate the agent name (from the `/start <name>` argument):

   Bash: `bash core/scripts/validate-agent-name.sh "<agent-name>"`

   The script enforces:
   - Lowercase kebab-case: `^[a-z][a-z0-9-]*$` (must start with a letter,
     then letters / digits / hyphens only)
   - Not in the reserved-name list (`core`, `meta`, `world`, `node_modules`,
     `.git`, `.claude`, `.github`)

   Exit codes:
   - `0` — valid, proceed to A2
   - `2` — invalid format
   - `3` — reserved name

   **HALT ON FAILURE** — if exit code != 0, STOP. Display the script's stderr
   verbatim to the user. Do NOT proceed to A2. The user must re-run
   `/start <valid-name>` with a corrected name.

   Rationale: failing fast here prevents the user from spending minutes on
   path elicitation (Phase B) and identity capture (Phase C2-C5) only to
   have init-agent.sh:33-46 reject the name at C0. Single source of truth
   caveat: `validate-agent-name.sh` and `init-agent.sh:33-46` MUST stay in
   sync — they implement the same regex and reserved-name list. Defense in
   depth: A1 catches typos early; init-agent re-validates in case A1 was
   bypassed.

A2. **Bind Agent to Session**

   First, ensure the agent dir + local-paths.conf placeholder exist so the binding writer can validate. The full conf is configured in Phase B; here we just create the directory shape so `session-binding-write.sh` doesn't fail its agent-dir-exists check.

   Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; mkdir -p "agents/<agent-name>" && touch "agents/<agent-name>/local-paths.conf" && bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent "<agent-name>" --mode "<target-mode>" --retire-legacy >/dev/null && echo "BOUND:$MIND_SID" && printf '\n╔════════════════════════════════════════════════════════════╗\n║                                                            ║\n║    ✓  RACE_WINDOW_CLOSED                                   ║\n║       Safe to /start another agent in another terminal     ║\n║                                                            ║\n╚════════════════════════════════════════════════════════════╝\n\n'`

   `<target-mode>` substitution: the LLM substitutes the target mode (`reader`, `assistant`, or `autonomous` — default for UNINITIALIZED entry without `--mode` is `autonomous`).

   (No write to `agents/<agent-name>/session/latest-session-id` here. For autonomous mode, the Phase C9.9 runner claim atomically writes both `latest-session-id` and `running-session-id` together. Same runner-only-writes-both rule as IDLE Step 0.)

   **HALT ON EMPTY SID** — if output contains `ERROR:EMPTY_MIND_SID`, STOP. Do NOT proceed to Phase A3. Display to the user:
   > Cannot initialize agent `<agent-name>`: the PreToolUse[Bash] hook did not inject `MIND_SID`. Close this terminal, open a new one, and retry `/start <agent-name>`. If the issue persists, check `.claude/settings.json` for the PreToolUse hook registration and `core/scripts/bash-agent-inject.sh` / `.py`.

   Phase 2.6: the binding lives at `agents/<agent-name>/sessions/$MIND_SID/binding.yaml` — the per-session dir name IS the SID. One binding per session, no shared root file. The directory shape is also the queryable record of "how many sessions has this agent had" (count subdirs under `agents/<agent-name>/sessions/`).

A3. Create the agent directory (if it doesn't exist):

   Bash: `mkdir -p agents/<agent-name>`

**Phase B: Configure Paths** (only if `agents/<agent>/local-paths.conf` does not yet contain `WORLD_PATH=`).

Each agent stores its own path configuration. `world/` and `meta/` can live
inside the project root (simplest, single-machine) OR at external user-supplied
paths (shared drive / NAS / cloud-sync folder for multi-machine sharing).

**Why a content check, not an existence check (F3, 2026-05-20)**: A2 above
calls `touch local-paths.conf` to satisfy `session-binding-write.py:77`'s
"conf-file-must-exist" gate (the binding writer refuses to write the binding
without a conf file present, even if empty). That leaves an EMPTY conf on
disk after A2. A literal-minded reading of an existence check would conclude
"conf exists, skip Phase B" — bypassing path elicitation entirely and leaving
`_paths.sh:164` resolving `WORLD_DIR` to empty string. Phase B's check (here
and at "skip Phase B entirely" below) is content-aware: empty conf from A2
fails the WORLD_PATH= grep, so Phase B runs and B7 populates the conf.
Populated conf passes the grep, so Phase B skips on legitimate resume.

**B0.5. BOOTSTRAP PATHS GATE — NON-SKIPPABLE (mirrors C1.9 semantics).**

This gate exists because of the 2026-05-20 testy incident: the agent invented
`<project_root>-world` and `-meta` sibling paths as "reasonable defaults"
between A6 and B7, without prompting the user — because Auto Mode triggered
"make the reasonable call" reasoning. The invented paths then propagated
through B7 (conf write), B10 (permission grants), C0 (init), and the user
had to manually clean up the test repo to retry. Path choices are
unrecoverable once written: directories get created, settings.local.json
gets seeded with those paths, and reverting requires a manual delete.

Rules — ALL mandatory, in order, BEFORE B7 (the conf write):

1. **NEVER invent paths.** Even in auto mode, even with the suggested default
   visible in the prompt — the agent MUST NOT write `local-paths.conf`
   without explicit user authorization. "Reasonable default" reasoning is
   the documented testy-incident failure mode. The bar is identical to
   C1.9: "fabricating identity by inference" became "fabricating paths by
   inference" — same shape, same gate.

2. **Suggest, then ask.** Show the suggested default (`./world` and `./meta`
   inside the project root, alongside `core/`, `mind_api/`, `agents/`) AND
   the alternative shapes (shared remote / cloud-sync for multi-machine).
   Always pose the question — even if the user is expected to accept the
   default.

3. **What counts as user authorization** (any one is sufficient):
   - **Explicit path**: "use /Users/me/foo" — proceed with that path
   - **Confirm suggestion**: "yes, use the default", "go with `./world`",
     "the suggestion is fine" — proceed with default
   - **Explicit delegation**: "you pick", "I don't care", "this is a test,
     do whatever", "make it simple", "use whatever makes sense" — proceed
     with default
   - **NOT sufficient**: silence, prior unrelated permissions, the
     existence of Auto Mode, or any inferred preference — STOP and ask.

4. **Auto Mode does NOT override this gate.** When the system reminder says
   "Work without stopping for clarifying questions" — paths are the
   exception, same class as bootstrap identity (C1.9). Stop and ask anyway.
   Proceed only if rule 3 conditions are met.

5. **Why stopping here is safe.** At Phase B, agent-state is still
   UNINITIALIZED. The stop hook does not force-enter the loop until C9.9.
   Pausing for user input is fully interruptible. After B7/B10 write the
   conf + seed permissions, reverting the path choice requires manual
   filesystem cleanup — which is exactly the cost the testy incident paid.

Only after this gate passes (rules 1-4 satisfied), proceed to B1.

B1. Ask for the **world directory** path. NON-SKIPPABLE per B0.5 gate:

   ```
   First, I need to know where to store collective knowledge.

   **World Directory** — This is where all shared domain knowledge lives:
   the knowledge tree, hypotheses, reasoning bank, aspirations, and more.
   Multiple agents and machines can share this directory.

   **Suggested default** (simplest — single machine, single repo):
     ./world  →  expands to <project_root>/world, alongside core/, mind_api/, agents/

   Everything stays inside the project — no external paths to manage. The
   trade-off: agents in OTHER repos or on OTHER machines can't see this
   world. If you want multi-repo or multi-machine collaboration, put world/
   on a shared remote (NAS, OneDrive, SharePoint, Dropbox, iCloud) instead.

   Other valid examples (use FORWARD slashes on every platform —
   backslashes get interpreted as escape sequences when bash sources the
   path file):
   - C:/Users/you/OneDrive/my-mind-world   (Windows, OneDrive — sharable across machines)
   - /Users/you/Documents/my-mind-world    (macOS — local single-machine)
   - /home/you/mind-world                  (Linux — local single-machine)

   Where should the world directory be?
   - Reply `./world` (or just "default", "use the suggestion", "yes") to
     accept the in-project default
   - Reply with an absolute/relative path to use a different location
   - Reply "you pick" / "I don't care" / "make it simple" to delegate
     (= proceed with the in-project default per B0.5 rule 3)
   ```

   If the user pastes a Windows path with backslashes (e.g.,
   `C:\Users\you\OneDrive\my-mind-world` from Explorer's address bar),
   silently convert backslashes to forward slashes before validation in B3.
   Do not bounce the user back — the conversion is part of normalization.

B2. AskUserQuestion (allowed — agent-state is not RUNNING yet).

   **DO NOT PRESUME** — per B0.5 gate: even in auto mode, do NOT advance
   past B2 without an explicit response that matches one of the
   authorization shapes in B0.5 rule 3. The instinct to "make the
   reasonable call" is the testy-incident failure mode; resist it.

B3. Validate the world path:
   - Resolve relative paths against PROJECT_ROOT
   - Check directory exists (or parent exists and is writable)
   - If **doesn't exist**: create it, confirm "Created new directory at {path}"
   - If **empty**: confirm "Empty directory — I'll set up a fresh world"
   - If **populated** (has `knowledge/` or `.initialized`): confirm "Found an existing world at {path} — I'll connect to it"
   - If **not writable**: tell user, ask for a different path

B4. Ask for the **meta directory** path. NON-SKIPPABLE per B0.5 gate:

   ```
   **Meta Directory** — This is where domain-agnostic improvement strategies
   live. It tracks how the agent gets better at learning itself, independent
   of any specific domain.

   Same rules as the world path: empty directory for fresh start, or existing
   meta directory; forward slashes only.

   **Suggested default** (matches world layout):
     ./meta  →  expands to <project_root>/meta, alongside core/, mind_api/, agents/, world/

   Other valid examples (typically next to the world directory):
   - C:/Users/you/OneDrive/my-mind-meta    (Windows)
   - /Users/you/Documents/my-mind-meta     (macOS)

   Where should the meta directory be?
   - Reply `./meta` / "default" / "yes" to accept the in-project default
   - Reply with an absolute/relative path for a different location
   - Reply "you pick" / "same place as world" to delegate (per B0.5 rule 3)
   ```

B5. AskUserQuestion.

   **DO NOT PRESUME** — same enforcement as B2: per B0.5 gate, even in auto
   mode, an explicit user response matching one of the B0.5 rule 3
   authorization shapes is required before B6/B7.

B6. Validate the meta path (same rules as B3)

B7. Write `agents/<agent>/local-paths.conf`:
   ```bash
   # Paths to external world and meta directories
   # Written by /start — edit manually to change locations
   WORLD_PATH={validated_world_path}
   META_PATH={validated_meta_path}
   ```
   IMPORTANT: Use forward slashes on all platforms (e.g., `C:/Users/Shared/world`,
   not `C:\Users\Shared\world`). Backslashes are interpreted as escape sequences
   when bash sources the file. Python handles both slash styles.

B8. Confirm paths:
   ```
   Paths configured:
     World: {world_path}
     Meta:  {meta_path}
   ```

B9. **Add permissions for external paths** — Ask for confirmation:
   ```
   I need to add read/write permissions for these directories to your local
   settings (.claude/settings.local.json). This file is local to your
   machine and not committed to git.

   Permissions to add (recursive subtree, all relevant tools):
     Read / Edit / Write / MultiEdit  on  {world_path}/**
     Read / Edit / Write / MultiEdit  on  {meta_path}/**
     Read / Edit / Write / MultiEdit  on  {project_root}/**

   If your settings.local.json doesn't exist yet, I'll create it with the
   framework's broad allows (Bash, Read, Glob, Grep, WebSearch, WebFetch)
   plus the constitutional deny baseline (rb-931) plus the path allows above.

   If it already exists, I'll only ADD missing rules — your existing config
   (env vars, statusLine, outputStyle, etc.) is preserved verbatim.

   OK to add these?
   ```

B10. AskUserQuestion for confirmation
   - If yes: Bash: `MIND_AGENT=<agent-name> bash core/scripts/permissions-add.sh`

     The explicit `MIND_AGENT=<agent-name>` prefix is belt-and-suspenders
     (matches IDLE Step 2 / Step 0.7 / runner-dead-check pattern). The
     PreToolUse[Bash] hook normally auto-injects by resolving the binding
     written in A2 (Phase 2.6 layout: `agents/<agent-name>/sessions/$MIND_SID/binding.yaml`,
     with legacy `.active-agent-<SID>` fallback). If the hook times out
     cold-starting Python (Windows `bash-agent-inject.sh` failure mode —
     see `core/config/conventions/python-invocation.md`) the script would
     fall back to `_paths.sh:146-154` first-available-conf. On
     UNINITIALIZED first-run this is the ONLY conf so it resolves
     correctly anyway — but hardening the call site against future
     multi-agent installs costs nothing.

     The sanctioned wrapper reads `WORLD_DIR` + `META_DIR` + `PROJECT_ROOT`
     from `_paths.sh` (resolved from the agent's `local-paths.conf` written
     in B7), then merges the path-specific allows into
     `.claude/settings.local.json` AND ensures the constitutional deny
     baseline is present. Atomic write (tempfile + rename). Idempotent on
     re-run.

     **Why a script instead of direct Write/Edit**: `.claude/settings.local.json`
     is the constitutional anchor (rb-931, CLAUDE.md "two-file settings rule").
     The file's own `permissions.deny[]` hard-blocks `Edit`/`Write`/`MultiEdit`
     tool calls on itself — the LLM cannot edit it through Claude's editing
     tools. The script writes via Bash, which the deny patterns do not match.
     This is the user-authorized maintenance path.

     **Exit codes**:
     - `0` — success
     - `2` — required state missing (rare — A2 binding should have populated this)
     - `3` — existing settings.local.json is malformed (file is left untouched)
     - `4` — Python launcher unavailable (re-run check-prerequisites.sh)

     **HALT ON NON-ZERO EXIT** — if exit code != 0, display the script's
     stderr and ask the user whether to: (a) abort /start and fix the issue
     manually, or (b) continue without permissions (the user will then see
     per-call permission dialogs throughout the session). Default
     recommendation: abort + fix.

   - If no: warn that file access to external paths may require per-call
     permission approval throughout the session.

If `agents/<agent>/local-paths.conf` already contains `WORLD_PATH=`, skip Phase B entirely — paths are already configured. Use a content check, not bare existence (see "Why a content check" rationale at Phase B header — A2's `touch` leaves an empty conf on disk before Phase B runs). Concrete probe: `grep -q '^WORLD_PATH=' agents/<agent>/local-paths.conf` — exit 0 means populated, skip; exit 1 means empty or absent, run Phase B.

**Phase C: The Program and Agent Identity**

Phase C establishes two separate things:
- **The Program** (`world/program.md`) — The overarching mission shared by ALL agents in this
  world. Written once, shared across agents. Answers: "Why does this world exist?"
- **Self** (`agents/<agent>/self.md`) — This specific agent's identity, role, and perspective.
  Unique per agent. Answers: "Who am I? What is my role?"

These are NOT the same thing. The Program is the world's purpose. Self is the agent's identity.

**C0. Initialize infrastructure** (all modes):
`bash core/scripts/init-mind.sh`

**C0.1. Ensure daemon is running** (all modes, fail-open):
`bash core/scripts/mind-api-start.sh || echo "[start] daemon-start failed (non-fatal)" >&2`
(Idempotent. Runs after init-mind so mind_api/state/ directory exists. Fail-open so
init can complete even if daemon spawn fails — wrapper auto-spawn is fallback.)

**C0.5. Configure domain conventions** (per-slot existence detection — seed each
missing canonical Pattern B hook slot from the framework templates in
`core/config/templates/`).

Per-slot detection (NOT whole-directory): C0.5 used to short-circuit if
`world/conventions/` had ANY `.md` file, which silently skipped seeding even
when the canonical `post-execution.md` / `pre-execution.md` slots were missing.
Now each slot is checked independently. Canonical slot table:
`core/config/conventions/domain-hooks.md` → "Canonical Hook Slots (Pattern B)".

**C0.5 GATE — NON-SKIPPABLE (mirrors B0.5 / C1.9 semantics).** Applies ONLY
when at least one canonical slot is missing (i.e., the AskUserQuestion below
is going to fire). When both slots already exist from a prior /start, this
gate is moot — the procedure short-circuits at the "skip prompt-and-seed"
branch and no question is asked.

This gate exists because of the 2026-05-20 hooks-prompt-skipped observation:
the user explicitly designed C0.5 (commits `d0f32aa5` / `0aaf3163`, May 16) so
they would be ASKED whether to add domain-specific pre/post-execution steps
on first-touch. But Auto Mode caused the agent to treat the AskUserQuestion at
C0.5 as a "make the reasonable call" moment and silently pick "no" — the
question was never surfaced to the user. Unlike B0.5 (paths) and C1.9
(identity), the underlying choice ("no domain additions, defaults only") is
technically safe for correctness — the framework defaults install by
construction either way. But the user explicitly wanted the **opportunity to
decide** at this moment, and silently answering for them denies that
opportunity. That alone is the failure.

Rules — ALL mandatory, in order, BEFORE the AskUserQuestion at the
"Optional: add domain-specific steps?" prompt below:

1. **NEVER auto-answer the C0.5 prompt.** Even in auto mode, even when "no"
   would be a perfectly safe answer, the agent MUST surface the prompt to
   the user and wait for an explicit reply. "Make the reasonable call"
   reasoning is the documented failure mode. The bar is the same as B0.5
   and C1.9: a moment the user explicitly designed to be theirs.

2. **The defaults install BEFORE the question runs — that is by design.**
   The `cp` commands below install framework-essential conventions by
   construction (per `d0f32aa5`'s design: verify-learning Section DC
   structural invariants hold ONLY because the template is installed
   verbatim). The question is purely about whether the user wants to ADD
   domain layers under `## Domain Additions`, NOT whether the defaults
   install. This is why "no" is safe — but still must be asked.

3. **What counts as user authorization for the answer** (any one is sufficient):
   - **Explicit yes**: "yes", "add steps", "I want to customize" — proceed
     to collect domain content
   - **Explicit no**: "no", "defaults are fine", "skip", "later" — proceed
     without additions
   - **Show first**: "show me", "show-me-the-defaults" — display files,
     then re-ask (loop is built into the procedure below)
   - **Explicit delegation**: "you pick", "I don't care", "this is a test,
     do whatever", "make it simple" — proceed with "no" (defaults
     installed, no additions)
   - **NOT sufficient**: silence, prior unrelated permissions, the
     existence of Auto Mode, or any inferred preference — STOP and ask.

4. **Auto Mode does NOT override this gate.** When the system reminder says
   "Work without stopping for clarifying questions" — the domain-conventions
   prompt is an exception, same class as B0.5 and C1.9. Stop and ask anyway.
   Proceed only if rule 3 conditions are met.

5. **Why stopping here is safe.** At C0.5, agent-state is still
   UNINITIALIZED. The stop hook does not force-enter the aspirations loop
   until C9.9 (per the same reasoning as C1.9 rule 5). Pausing for user
   input is fully interruptible. Unlike paths and identity, the cost of an
   auto-picked "no" is NOT data corruption — it's the loss of a
   deliberately-designed user moment. That alone justifies the gate.

Only after this gate is acknowledged, proceed to the existence check below.
The gate triggers ONLY when the existence check finds at least one missing
slot; when both slots exist, the procedure skips the AskUserQuestion entirely
(line "IF both slots already exist" below) and rules 1-4 do not apply.

Bash: `source core/scripts/_paths.sh && \
  pre_missing=$([ -f "$WORLD_DIR/conventions/pre-execution.md" ] && echo "no" || echo "yes") && \
  post_missing=$([ -f "$WORLD_DIR/conventions/post-execution.md" ] && echo "no" || echo "yes") && \
  echo "pre_missing=$pre_missing post_missing=$post_missing"`

IF both slots already exist (`pre_missing=no post_missing=no`):
  Skip the prompt-and-seed portion of C0.5 — both canonical slots are already
  configured (existing world, or a prior /start seeded them). Fall through to
  the on-demand-slot informational note below; do NOT exit C0.5 early.

IF either slot is missing:

  Output to user (informational notice, not a question). List ONLY the
  slots whose <prefix>_missing == "yes":
  ```
  Installing framework-essential convention structure(s):
    {if pre_missing == "yes":}
    - pre-execution.md (5 steps): Curriculum stage check, Pull latest,
      Fix scope coverage, Causal isolation, Dependency chain verification
    {if post_missing == "yes":}
    - post-execution.md (4 steps): Infrastructure health recording,
      Run testing circuits, Fresh-eyes code review, Commit and push

  These are installed by construction — framework invariants (Step ordering,
  fresh-eyes wiring, --author $MIND_AGENT filter, step count limit) are
  guaranteed. You can edit world/conventions/<slot>.md anytime to add
  domain layers.
  ```

  # CRITICAL — DO NOT switch back to LLM-authored "custom-from-scratch".
  # The verify-learning Section DC structural invariants (Step 1.5/1.75/2
  # ordering, fresh-eyes wiring, --author $MIND_AGENT filter, step-count
  # cap) hold ONLY because the template is installed verbatim by cp here.
  For each missing slot (pre-execution if pre_missing=="yes", post-execution
  if post_missing=="yes"):
    Bash: `cp core/config/templates/<slot>-default.md "$WORLD_DIR/conventions/<slot>.md"`
    Log: "Seeded $WORLD_DIR/conventions/<slot>.md from framework default
          (core/config/templates/<slot>-default.md)."

  AskUserQuestion (one prompt covering whichever slots were just installed).
  **DO NOT AUTO-ANSWER** — per C0.5 gate rule 1: even in auto mode, do NOT
  pick "no" silently. Surface the question and wait for an explicit response
  matching one of rule 3's authorization shapes.
  ```
  Optional: add domain-specific steps to the convention(s) just installed?
    - yes:  I'll ask what to add and append under '## Domain Additions'
    - no:   defaults installed; edit anytime
    - show-me-the-defaults: I'll display the installed files, then re-ask

  Note: domain additions use `## Step` headers that count toward the
  step-limit cap (currently 12). Keep additions tight — the framework
  defaults use 5 pre-execution + 4 post-execution steps already.
  ```

  IF "show-me-the-defaults":
    Bash: `bash core/scripts/world-cat.sh conventions/pre-execution.md && \
           bash core/scripts/world-cat.sh conventions/post-execution.md`
    Loop back to the AskUserQuestion above.

  IF "yes": (ask only for slots that were just installed in this run)
    IF pre_missing == "yes":
      AskUserQuestion: "What domain-specific steps to add to pre-execution?
      (free text, or 'none')"
        IF non-'none':
          Bash: `printf '\n## Domain Additions\n\n%s\n' "<user content>" >> "$WORLD_DIR/conventions/pre-execution.md"`

    IF post_missing == "yes":
      AskUserQuestion: "What domain-specific steps to add to post-execution?
      (free text, or 'none')"
        IF non-'none':
          Bash: `printf '\n## Domain Additions\n\n%s\n' "<user content>" >> "$WORLD_DIR/conventions/post-execution.md"`

    Log: "Domain additions appended under '## Domain Additions' header."

  IF "no":
    Log: "Defaults installed. Edit world/conventions/<slot>.md anytime to
          add domain layers."

  After seeding (regardless of yes/no/show), ALWAYS print the on-demand-slot
  informational note below.

Print on-demand-slot informational note (every C0.5 entry, regardless of
which slots were seeded):

```
Two additional Pattern B hook slots exist on-demand and were NOT seeded
automatically (they require domain-specific scripts to be meaningful):

  - world/conventions/signal-refresh.md
      Consumer: aspirations-precheck/SKILL.md Phase 0.5.0-pre
      Use when: the domain has a user-signal source to scan before goal
      scoring (inbound email, external queue, directive board count).

  - world/conventions/outcome-observation.md
      Consumer: aspirations-state-update/SKILL.md Step 8.12
      Use when: the domain has measurable real-world outcomes beyond
      goal-completion counts (CI pass rate, service health, business KPI).

Create either file later when you have something concrete to put in it.
The runtime call sites no-op when the file is absent.
See core/config/conventions/domain-hooks.md for the full slot catalog.
```

**C1. The Program** (all modes):
Read `world/program.md`. If empty or only whitespace:

```
What is **The Program** for this world?

The Program is the shared purpose — the overarching mission that all agents
in this world work toward. It lives in world/program.md and is shared across
every agent.

There is no default — The Program is entirely yours. The framework provides
the learning loop; The Program tells it WHAT to learn about and WHY.

Examples:
- "Build and ship the best project management tool in the market."
- "Research and synthesize machine learning papers into actionable knowledge."
- "Develop a multiplayer game with intelligent AI characters."

What should The Program be? (Or say "skip" to leave it blank for now —
you can write it later via `world/program.md`.)
```

- AskUserQuestion
- If user provides content (not "skip"): Write to `world/program.md`
- If `world/program.md` was already populated: display it briefly and proceed

Phase C then adapts based on mode:

### Phase C for Reader Mode (simplified)

C2. AskUserQuestion:
   ```
   Setting up reader mode — read-only access to domain knowledge.

   Optional: Tell me who this agent is — its specific role and perspective.
   This helps me contextualize answers. Or say "skip" to use just The Program
   for context.
   ```

C3. If user provided an identity (not "skip"), write `agents/<agent>/self.md`:
   ```markdown
   ---
   created: "{today}"
   last_updated: "{today}"
   last_update_trigger: "initial_creation"
   source: "user"
   ---

   # Self

   {parsed Self content}
   ```

C4. Set mode and state:
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-mode-set.sh reader`

     **HALT ON NON-ZERO EXIT (g-115-1032, 2026-05-21)** — if
     `session-mode-set.sh` exits non-zero, STOP. Do NOT proceed to the
     session-state-set.sh write below. Mode-set failure means the on-disk
     mode signal was NOT written; agent will fall back to the reader disk
     default per CLAUDE.md "Mode System" — that happens to match the
     intended `reader` here, but the asymmetric assistant/autonomous C8/C9.9
     siblings would silently land in reader. Halt for the contract
     uniformity; the session-state-set.sh below would also be misleading
     if mode-set silently failed. Display:
     > Cannot initialize agent `<agent-name>` in reader mode
     > (session-mode-set.sh exited non-zero). Investigate stderr above and
     > retry `/start <agent-name> --mode reader`.
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`

     **HALT ON NON-ZERO EXIT (G2, 2026-05-20)** — if `session-state-set.sh`
     exits non-zero, STOP. Do NOT proceed to C5/C6/C7. State remains
     UNINITIALIZED; the persona-set below is harmless. Display:
     > Cannot initialize agent `<agent-name>` in reader mode
     > (session-state-set.sh exited non-zero). Investigate stderr above and
     > retry `/start <agent-name> --mode reader`. Without this halt, C7's
     > "Agent initialized in reader mode" message would lie about success.
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-persona-set.sh true`
   - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
     (Consumed by `session-artifacts-count.sh` → productivity-stop-gate.
     Helper exits non-zero if unset, which makes the gate treat total
     artifacts as 0. Seed on every session entry — IDLE-branch reader
     step 3 / assistant step 3 / autonomous step 3 all do the same.)

     **MIND_AGENT prefix rationale (G1, 2026-05-20)** — the three state setters
     above carry the explicit `MIND_AGENT=<agent-name>` prefix for the same
     belt-and-suspenders reason `wm-set.sh` does (PreToolUse hook cold-start
     race). The IDLE-branch reader/assistant variants (under the IDLE section
     above — Step 3 of the IDLE flow) already use this prefix consistently;
     the UNINITIALIZED variants now match. (H2, 2026-05-20: tightened from
     "Step 3 above" — the UNINITIALIZED branch has no Step 3 of its own.)

C5. Invoke `/prime` (reader context — pass `--read-only` to retrieve.sh)

C6. Load mode instructions: Read `core/config/modes/reader.md`

C7. Output: "Agent initialized in reader mode. I have access to all accumulated knowledge. Ask me anything."

### Phase C for Assistant Mode

**C1.9. BOOTSTRAP IDENTITY GATE — applies here too.** Assistant mode writes
`agents/<agent>/self.md` from user input exactly as Autonomous does. Before C2,
apply the full C1.9 gate documented under "### Phase C for Autonomous Mode":
check for a user-staged spec and use it verbatim; NEVER derive Self from The
Program / sibling self.md / inference; Decision-Authority + guard-380 do NOT
authorize bootstrap fabrication or skipping C5; C2→C5 explicit confirmation
is mandatory before any self.md / curriculum write. Assistant never flips to
RUNNING, so the stop-hook trap does not apply — but a fabricated Self is just
as wrong in assistant mode, and the no-inference + explicit-confirmation
rules are identical.

C2. Display the identity prompt:

   ```
   Now I need a few things from you:

   1. **My Self** — This is the agent's identity. It tells me WHO I am
   and WHAT my role is. This is separate from The Program (the world's
   shared purpose) — Self is about this specific agent.

   Examples:
   - "You are a QA engineer for Acme Corp."
   - "You are a personal research assistant focused on ML papers."

   2. **My Aspirations** — Your goals. I won't execute them autonomously,
   but they help me organize and prioritize when you give me directives.

   Note: the framework already auto-seeds a bootstrap aspiration
   ("Maintain Agent Health" — recurring housekeeping). Your aspirations
   are ADDED on top.

   Examples:
   - "Learn the codebase thoroughly."
   - "Research competitor platforms."

   3. **My Curriculum** (optional) — Staged learning plan with graduation
   gates before attempting more complex tasks. If omitted, the framework's
   default 3-stage curriculum (Foundation / Growth / Autonomy) is used —
   it includes concrete graduation gates, not a blank placeholder.

   Tell me these three — your Self, your Aspirations, and optionally
   your Curriculum. I'll learn when you teach me.
   ```

C3-C7. Same as autonomous Phase C steps C3-C7 (parse, echo, confirm,
curriculum, self.md) — INCLUDING the C5 HARD STOP: explicit user
confirmation is mandatory before the C6 curriculum / C7 self.md writes,
and Self is never derived from The Program (C1.9 rules 2-3).

C8. Set mode and state:
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-mode-set.sh assistant`

      **HALT ON NON-ZERO EXIT (g-115-1032, 2026-05-21)** — if
      `session-mode-set.sh` exits non-zero, STOP. Do NOT proceed to the
      session-state-set.sh write below. Mode-set failure means the on-disk
      mode signal was NOT written; agent will fall back to the reader disk
      default per CLAUDE.md "Mode System" — so the user who asked for
      assistant mode would silently land in reader (no writes, no
      directives), while the C10 success message would falsely confirm
      assistant mode. This is the canonical silent capability mismatch the
      goal calls out. Display:
      > Cannot initialize agent `<agent-name>` in assistant mode
      > (session-mode-set.sh exited non-zero). Investigate stderr above and
      > retry `/start <agent-name> --mode assistant`.
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`

      **HALT ON NON-ZERO EXIT (G2, 2026-05-20)** — if `session-state-set.sh`
      exits non-zero, STOP. Do NOT proceed to C8.5/C9/C10. State remains
      UNINITIALIZED; the persona-set below is harmless. Display:
      > Cannot initialize agent `<agent-name>` in assistant mode
      > (session-state-set.sh exited non-zero). Investigate stderr above and
      > retry `/start <agent-name> --mode assistant`. Without this halt,
      > C10's success message would lie and C9's `/create-aspiration` would
      > also fire against an uninitialized agent.
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-persona-set.sh true`
    - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
      (Same rationale as Phase C reader mode C4. MIND_AGENT prefix on the
      three state setters above matches G1's reader-C4 fix.)

C8.5. Invoke `/prime` — load domain context before aspiration creation.

C9. ASPIRATION-FROM-USER INVOCATION (rb-797 silent-skip mitigation).

   IF aspiration text was extracted from C3 (i.e., the user provided one or
   more aspiration descriptions in their reply): you MUST invoke
   `/create-aspiration from-user` with that text BEFORE proceeding to C10.
   Do NOT skip this phase. The single-line imperative form was historically
   pattern-skipped after dense-Bash C8 (rb-797 / g-115-522 — failure mode:
   user-provided aspiration text silently dropped, only auto-seeded asp-001/
   asp-003 landing in the agent's queue).

   Invoke `/create-aspiration from-user` with the extracted aspiration text.

C10. Load mode instructions: Read `core/config/modes/assistant.md`

C10.5. POST-INIT ASPIRATION VERIFICATION (rb-797 silent-skip detector).

   Counts the agent's non-bootstrap aspirations and warns loud if user
   provided aspiration text in C3 but the C9 invocation produced no
   non-bootstrap aspiration. This catches the pattern-skip failure mode
   after the fact and gives the agent a chance to re-invoke before C11.

   Bash: MIND_AGENT=<agent-name> bash core/scripts/aspirations-read.sh --source agent --active-compact 2>/dev/null \
     | py -3 -c "import json,sys; d=json.load(sys.stdin); asps=d if isinstance(d,list) else d.get('aspirations',[]); nonboot=[a for a in asps if a.get('id') not in ('asp-001','asp-003')]; print('NONBOOT='+str(len(nonboot)))"

   IF user provided aspiration text in C3 AND NONBOOT == 0:
       Output (LOUD): "▸ ⚠ POST-INIT WARNING: User provided aspiration text in C3 but agent has 0 non-bootstrap aspirations. C9 (/create-aspiration from-user) likely silent-skipped — rb-797 failure mode. RE-INVOKE /create-aspiration from-user explicitly with the extracted text NOW before C11."
       Bash: source core/scripts/_paths.sh && mkdir -p "$WORLD_DIR/audit-reports" && printf '{"agent":"%s","detected_at":"%s","c3_extracted":true,"nonboot_count":0,"reason":"rb-797 silent-skip"}\n' "<agent-name>" "$(date +%Y-%m-%dT%H:%M:%S)" >> "$WORLD_DIR/audit-reports/start-c9-skip-detections.jsonl"
   ELIF user provided aspiration text in C3 AND NONBOOT >= 1:
       Output: "Post-init verification: $NONBOOT non-bootstrap aspiration(s) present — C9 landed user aspirations correctly."
   ELSE:
       Output: "Post-init verification: no user aspiration text extracted in C3, no verification needed."

C11. Output: "Agent initialized in assistant mode. I'll learn when you teach me — give me directives like 'learn about X' or 'remember that Y'."

### Phase C for Autonomous Mode (current behavior)

**C1.9. BOOTSTRAP IDENTITY GATE — NON-SKIPPABLE (applies to Assistant Phase C too).**

This gate exists because of the 2026-05-15 charlie incident: the agent
fabricated `charlie/self.md` by "deriving" it from The Program, skipped the
C5 confirmation by invoking the self.md Decision-Authority model, flipped to
RUNNING, and then the stop hook (which forces the aspirations loop whenever
state==RUNNING) made interactive correction impossible — the user's
corrections kept getting interrupted and `/boot` never ran.

Rules — ALL mandatory, in order, BEFORE C2:

1. **Check for a user-staged spec FIRST.** Ask the user (plainly, in text)
   whether a prepared Self / identity / start-block spec already exists and,
   if so, its file path(s). If a path is given: `Read` it and use it
   **verbatim** (`cp` for exact fidelity when it is itself the target file —
   no transcription drift). The staged file IS the Self. Do not paraphrase,
   summarize, "faithfully derive", or improve it.

2. **NEVER author Self by inference.** You MUST NOT derive Self from
   `world/program.md`, from sibling `agents/<agent>/self.md` files, from the team
   model, or from any reasoning about "what this agent obviously is." Bootstrap
   identity is a user-input gate, full stop. "The Program already describes
   this agent" is INPUT to show the user for confirmation — never license to
   author it yourself.

3. **Decision-Authority / guard-380 do NOT apply here.** Those govern
   *evolving an EXISTING Self during the autonomous loop* (post-notification,
   revert-if-wrong). They do **not** authorize fabricating a NEW agent's
   initial Self at `/start`, and they do **not** permit skipping C5. If you
   catch yourself reasoning "self.md material writes are act-and-report, so
   I'll derive it and surface for review" — STOP. That is the exact charlie
   rationalization. Bootstrap ≠ evolution.

4. **C2→C5 must complete with EXPLICIT user confirmation before ANY
   state-mutating step** (C6 curriculum, C7 self.md, C8 mode-set, the C9.9
   runner claim). If `AskUserQuestion` is unavailable, ask in plain text and
   **wait** for the reply. Do not proceed on assumption. Do not batch past
   the confirmation.

5. **Why stopping here is safe (and why later is not).** At C1.9–C5 the
   agent-state is still UNINITIALIZED/IDLE. The stop hook only force-enters
   the aspirations loop when state==RUNNING (stop-hook.sh Gate 1: state !=
   RUNNING → ALLOW stop). So pausing for the user here is fully interruptible
   and the user can reply normally. After the C9.9 runner claim flips RUNNING,
   that is no longer true — which is precisely why identity MUST be settled,
   confirmed, and written before the claim (Fix 2 reorders the claim to sit
   immediately before `/boot` for the same reason).

Only after this gate passes, proceed to C2.

C2. Display the identity and aspirations prompt:

   ```
   Now I need three things from you:

   **My Self** — This is the agent's identity. It tells me WHO I am
   and WHAT I'm for. It's the fundamental drive that shapes every decision
   I make. Think of it as the soul of the agent. This is separate from
   The Program (the world's shared purpose) — Self is about this specific agent.

   Examples:
   - "You are an autonomous QA engineer for Acme Corp. Always be looking
     for the next improvement."
   - "You need to make money or die. Find every revenue opportunity."
   - "You are a personal research assistant focused on machine learning
     papers and implementations."

   **My Aspirations** — These are your goals. Think of them as a feature
   list, or life goals, or a to-do list. They can be literally anything —
   learn something, build something, analyze something, fix something.
   I can have multiple at once and I'll break each into actionable steps.

   Note: the framework already auto-seeds two bootstrap aspirations:
     - "Maintain Agent Health" (asp-001) — recurring housekeeping goals
       (reflect, review hypotheses, tree maintenance, replay, archival)
     - "Explore and Learn" (asp-001 world) — initial domain exploration
   Your aspirations are ADDED on top of these — you are not writing on
   a blank slate.

   Examples:
   - "Learn the codebase and API surface thoroughly."
   - "Improve test coverage to 80%."
   - "Research competitor platforms and identify opportunities."

   **My Curriculum** (optional) — This is your staged learning plan.
   It defines what capabilities I unlock as I demonstrate competence.

   If you don't provide one, I'll use the framework's default 3-stage
   curriculum (from `core/config/curriculum.yaml`):
     Stage 1 (Foundation): Learn and explore (no Self edits, no forging)
     Stage 2 (Growth): Apply knowledge (Self edits + forging enabled)
     Stage 3 (Autonomy): Full capabilities (parallel execution enabled)
   The default includes concrete graduation gates (10 completed goals +
   competence >= 0.25 for Stage 1, etc.) — not a blank placeholder.

   Tell me all three — your Self, your Aspirations, and optionally
   your Curriculum. The more detail, the better I can act autonomously.
   ```

C3. Parse response:
   - Extract Self (identity/purpose/drive)
   - Extract aspiration descriptions (one or more goals/directions)
   - Extract curriculum stages (if provided). If user omits curriculum or
     says "default": note "use defaults"

C4. Echo back understanding:

   ```
   Here's what I understand:

   **My Self**
   [parsed Self — the agent's own words summarizing the user's intent]

   **Aspirations I'll create:**
   1. [title] — [brief description with initial goals]
   2. [title] — ...

   **Curriculum (Learning Stages):**
   1. [Stage name] — [description]. Unlocks: [none / self-edits / etc.]
      Graduation: [gate descriptions in plain language]
   2. [Stage name] — ...
   (or: "Using default 3-stage curriculum: Foundation → Growth → Autonomy")

   Does this look right?
   ```

C5. AskUserQuestion for confirmation (yes / adjust) — **HARD STOP (C1.9 gate)**
   - If adjust: re-parse and echo again
   - If yes: proceed
   - This is the non-skippable confirmation from C1.9 rule 4. You MUST NOT
     advance to C6/C7/C8/C9.9 until the user has explicitly confirmed Self +
     aspirations + curriculum. No state mutation, no self.md write, no
     mode-set, no runner claim before an explicit "yes". If the user staged
     a spec (C1.9 rule 1), echo back that you are using it verbatim and still
     get the explicit confirm — a staged file does not waive C5, it just
     makes C2–C4 a read-back instead of an elicitation.

C6. Write curriculum to `agents/<agent>/curriculum.yaml`:
   ```
   IF user provided custom stages:
     Parse into stage objects following the schema:
       - id: cur-01, cur-02, ... (sequential)
       - name: parsed stage name
       - description: parsed description
       - unlocks: infer from user intent (default all false for early stages,
         progressively enable for later stages)
         - allow_self_edits: false/true
         - allow_forge_skill: false/true
         - allow_multi_goal_parallelism: false/true
       - graduation_gates: infer from user criteria, using gate types:
         - metric_threshold (for competence/numeric targets)
         - count_check (for goal completion counts)
         - log_scan (for event counts)
         - command_check (for script-based checks)
         If user gives vague criteria: use reasonable defaults
         (e.g., "after mastering basics" → competence >= 0.30)
       - gate_status: initialize all as {passed: false, last_checked: null, current_value: null}

   IF user said "default" or omitted curriculum:
     Read core/config/curriculum.yaml → default_stages
     Use those stages directly

   Write agents/<agent>/curriculum.yaml (Edit the file seeded by init-mind.sh):
     current_stage: first stage ID (cur-01)
     stage_history:
       - stage_id: cur-01
         entered: "{today}"
         exited: null
     stages: [the parsed or default stage array]
   ```

C7. Write `agents/<agent>/self.md` with parsed Self (where `<agent>` is the active agent directory):
   ```markdown
   ---
   created: "{today}"
   last_updated: "{today}"
   last_update_trigger: "initial_creation"
   source: "user"
   ---

   # Self

   {parsed Self content}
   ```

C7.7. Layer-B output-style gate (g-115-316 / guard-454 / rb-629).
    Refuses autonomous mode + Explanatory output style — documented loop killer.
    Runs BEFORE C8 so a refusal leaves agent-state untouched.
    Step 0.6 already fires the same check pre-Phase-A so most users hit the
    early bail; this is the defense-in-depth layer in case Step 0.6 fail-opened
    (missing settings file, no py launcher).
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/output-style-gate.sh --mode autonomous`
      (Append ` --override "<justification>"` when the Step 0.5 parser captured
      `--override-output-style <justification>`. Pass the justification string
      verbatim — it lands in `world/output-style-overrides.jsonl`.)
      Exit codes: 0=proceed, 2=REFUSE (STOP /start, ask user to run
      `/output-style default` first, then re-issue `/start <agent-name>`),
      3=override accepted (proceed; audit logged to
      `world/output-style-overrides.jsonl`). Fail-open if gate is missing —
      Layer A (Return Protocol) and Layer C (stop-hook trailing-text-detector)
      remain in effect.

C8. Set MODE + explicit IDLE state — agent-state RUNNING flip is deferred to C9.9.
    (Fix 2, 2026-05-15: the RUNNING flip is deliberately deferred until
    everything interactive/long — identity confirmation C5, prime C8.5,
    aspiration creation C9, verification C9.3 — is DONE. A turn-end anywhere
    in C8–C9.3 then leaves state=IDLE, so the stop hook ALLOWS the stop and
    the user can still reply. The stop hook only force-enters the aspirations
    loop when state==RUNNING.)
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-mode-set.sh autonomous`
      (Mode signal ONLY — this is NOT the agent-state RUNNING flip (that is
      C9.9). Explicit `MIND_AGENT=` prefix — see IDLE step 2 belt-and-
      suspenders rationale. Setting mode=autonomous now makes the C8.5 prime
      run as an autonomous counter-bump retrieve, the intended bootstrap-prime
      behavior.)

      **HALT ON NON-ZERO EXIT (g-115-1032, 2026-05-21)** — if
      `session-mode-set.sh` exits non-zero, STOP. Do NOT proceed to the
      session-state-set.sh write below or to C8.5 /prime / C9 aspiration
      creation / C9.9 RUNNING flip. Mode-set failure means the on-disk
      mode signal was NOT written; agent will fall back to the reader disk
      default per CLAUDE.md "Mode System" — and a subsequent autonomous
      loop entry (C9.9) against a reader-default agent would mis-route
      /prime's retrieve, fire `/create-aspiration` against an
      uninitialized-mode agent, and the autonomous bootstrap would lie
      about being autonomous while reader-mode capabilities applied.
      Display:
      > Cannot transition agent `<agent-name>` to mode `autonomous` at C9
      > (session-mode-set.sh exited non-zero). Investigate stderr above and
      > retry `/start <agent-name>` (autonomous is the default mode).
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`
      (Fresh-install fix, 2026-05-20: on UNINITIALIZED→autonomous, agent-state
      never existed before — the "state stays IDLE" comment above is only
      literally true if IDLE was already set. Without this explicit write,
      C8.5 /prime sees state=UNINITIALIZED and Phase 0.5 either short-circuits
      or branches wrong. The reader and assistant flows (Phase C lines ~1152,
      ~1235) already do this; the autonomous flow's omission was a state-
      machine gap. **HALT ON NON-ZERO EXIT** — if the script exits non-zero,
      STOP. Investigate stderr and retry.)

C8.5. Invoke `/prime` — load domain context before aspiration creation.
    When connecting to an existing world, this ensures goal decomposition
    benefits from accumulated knowledge. On a fresh world, prime loads
    empty stores harmlessly. Runs at state=IDLE — prime explicitly supports
    IDLE (Phase 0.5: "IDLE or RUNNING: PROCEED"); no RUNNING dependency.

C9. ASPIRATION-FROM-USER INVOCATION (rb-797 silent-skip mitigation).

   IF aspiration text was extracted from C3 (i.e., the user provided one or
   more aspiration descriptions in their reply): you MUST invoke
   `/create-aspiration from-user` with that text BEFORE the C9.9 runner
   claim. Do NOT skip this phase. The single-line imperative form was
   historically pattern-skipped (rb-797 / g-115-522 — failure mode:
   user-provided aspiration text silently dropped, only auto-seeded
   asp-001/asp-003 landing in the agent's queue). The Fix 2 reorder
   de-risks this further: C9 now runs while state is still IDLE and BEFORE
   the dense triple-write (moved to C9.9), so a silent-skip caught by C9.3
   can be re-invoked without the stop hook interfering.

   Invoke `/create-aspiration from-user` with the extracted aspiration text.

C9.3. POST-INIT ASPIRATION VERIFICATION (rb-797 silent-skip detector).
   Runs BEFORE the C9.9 runner claim so a detected silent-skip can be
   re-invoked while state is still IDLE (interruptible — user can reply)
   instead of after RUNNING (where the stop hook forces the loop).

   Bash: MIND_AGENT=<agent-name> bash core/scripts/aspirations-read.sh --source agent --active-compact 2>/dev/null \
     | py -3 -c "import json,sys; d=json.load(sys.stdin); asps=d if isinstance(d,list) else d.get('aspirations',[]); nonboot=[a for a in asps if a.get('id') not in ('asp-001','asp-003')]; print('NONBOOT='+str(len(nonboot)))"

   IF user provided aspiration text in C3 AND NONBOOT == 0:
       Output (LOUD): "▸ ⚠ POST-INIT WARNING: User provided aspiration text in C3 but agent has 0 non-bootstrap aspirations. C9 (/create-aspiration from-user) likely silent-skipped — rb-797 failure mode. RE-INVOKE /create-aspiration from-user explicitly with the extracted text NOW before the C9.9 runner claim."
       Bash: source core/scripts/_paths.sh && mkdir -p "$WORLD_DIR/audit-reports" && printf '{"agent":"%s","detected_at":"%s","c3_extracted":true,"nonboot_count":0,"reason":"rb-797 silent-skip","mode":"autonomous"}\n' "<agent-name>" "$(date +%Y-%m-%dT%H:%M:%S)" >> "$WORLD_DIR/audit-reports/start-c9-skip-detections.jsonl"
   ELIF user provided aspiration text in C3 AND NONBOOT >= 1:
       Output: "Post-init verification: $NONBOOT non-bootstrap aspiration(s) present — C9 landed user aspirations correctly."
   ELSE:
       Output: "Post-init verification: no user aspiration text extracted in C3, no verification needed."

C9.9. RUNNER CLAIM — the agent-state RUNNING flip (Fix 2 critical section).

   **Everything interactive/long is DONE by here** (identity confirmed C5,
   prime C8.5, aspirations created C9, verified C9.3). From the first command
   in C9.9 through `Invoke /boot` (C11) there must be NOTHING stoppable,
   interactive, or long. Execute C9.9 → C10 → C11 in immediate succession in
   a single turn. If a turn-end (autocompact, a text summary, a question to
   the user) lands between `session-state-set RUNNING` and `/boot`, the stop
   hook force-enters the aspirations loop and `/boot` never runs — the
   2026-05-15 charlie incident. Do NOT pause to report, ask, or summarize
   inside C9.9–C11.

    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/heartbeat-tick.sh --bypass-state`
      (Seeds `runner-heartbeat` mtime immediately before the RUNNING
      transition. `--bypass-state` is REQUIRED because state is still IDLE
      here — heartbeat-tick.sh's IDLE-state gate refuses bare ticks; /start
      is the one legitimate caller that ticks against an about-to-flip IDLE
      state. Liveness is pure mtime — see `core/config/conventions/compact-recovery.md`.)
    - Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; RUNNER_TOKEN=$(py -3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null || python3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null); [ -n "$RUNNER_TOKEN" ] || { echo "ERROR:RUNNER_TOKEN_GEN_FAILED"; exit 3; }; AGENT_STATE_DIR="agents/<agent-name>/session"; mkdir -p "$AGENT_STATE_DIR" && echo "$MIND_SID" > "$AGENT_STATE_DIR/running-session-id.tmp" && mv "$AGENT_STATE_DIR/running-session-id.tmp" "$AGENT_STATE_DIR/running-session-id" && echo "$MIND_SID" > "$AGENT_STATE_DIR/latest-session-id.tmp" && mv "$AGENT_STATE_DIR/latest-session-id.tmp" "$AGENT_STATE_DIR/latest-session-id" && echo "$RUNNER_TOKEN" > "$AGENT_STATE_DIR/runner-token.tmp" && mv "$AGENT_STATE_DIR/runner-token.tmp" "$AGENT_STATE_DIR/runner-token" && echo "RUNNER_TOKEN=$RUNNER_TOKEN"`
      (Triple-write parallel to IDLE Step 3 — runner-token rationale + the
      rb-323/guard-403 reason this MUST run BEFORE state-set RUNNING below.
      heartbeat-tick directly above + this triple-write are the observer-
      paired signals seeded first. The `agents/<agent-name>/session/` Phase
      2.5.D prefix in the heredoc is REQUIRED — without it the writes land
      at PROJECT_ROOT/`agents/<agent-name>/session/` and create the 2026-05-19
      bravo/ cruft.)
      **HALT ON RUNNER_TOKEN_GEN_FAILED** — same as IDLE Step 3. State is
      still IDLE here, so a halt is a clean retry (no half-claimed zombie).
    - Bash: `rm -f agents/<agent-name>/session/iteration-checkpoint.json`
      (F4 reorder, 2026-05-20: moved BEFORE the state-set RUNNING below so
      the critical section between RUNNING and /boot is truly empty — making
      the C9.9 comment "NOTHING stoppable between RUNNING and /boot" literal,
      not just spirit. Safe to run at IDLE.)
    - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
      (F4 reorder: moved before the state flip. Same rationale as IDLE→autonomous
      step 3 — wm-set is a pure-Bash write to a top-level WM key, safe at IDLE.)
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh RUNNING`
      (State flip — final write in the RUNNING-claim sequence. heartbeat-tick
      + triple-write are seeded first per rb-323/guard-403 so observers never
      see RUNNING with a stale heartbeat or empty SID files. From THIS line
      the stop hook is armed — C10 + C11 MUST follow with no interruption.)

      **HALT ON NON-ZERO EXIT (F1, 2026-05-20)** — if the script exits non-zero,
      STOP. Do NOT proceed to C10/C11. State remains IDLE; the observer-paired
      signals seeded above are harmless (fresh heartbeat at IDLE means nothing
      to observers). Display:
      > Cannot transition agent `<agent-name>` to RUNNING at C9.9 (session-state-set.sh
      > exited non-zero). The agent stays IDLE; investigate stderr above and retry
      > `/start <agent-name>`. Without this halt, `/boot` (C11) would read state=IDLE
      > and abort with "Agent is stopped" — confusing failure mode.

C10. Output: "Agent initialized. Learning loop starting."

C11. Invoke `/boot` — immediately. No tool call, pause, question, or text
     between C9.9's `session-state-set RUNNING` and this invocation.

## Chaining
- Calls: /boot (autonomous mode), /prime (all modes during init; reader/assistant resume)
- Called by: User only. NEVER by Claude.
