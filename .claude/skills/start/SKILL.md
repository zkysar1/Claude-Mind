---
name: start
description: "Creates or resumes an agent in reader (read-only), assistant (user-directed), or autonomous mode (perpetual loop), handling full initialization for new agents (Self, program, paths, aspirations, curriculum) and state transitions for existing ones. USER-ONLY — Claude must NEVER invoke /start. Fires only when the user types /start {agent-name} [--mode {mode}]. Enforces the one-autonomous-session-per-agent invariant and supports observer sessions alongside running loops. Auto-recovers zombie sessions (state=RUNNING + stale heartbeat + no pending obligations) inline so /start {name} just works after a crash; --recover is reserved for the --force override path."
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
/start <agent-name> --reducer-only     # Refuse the automatic second-body join on rc=4
```

# Rationale: core/config/rationale/start-preflight-and-recovery.md — The body role is DERIVED, not declared (user direc

On resume (agent already exists):
```
/start                                 # Resume in current mode
/start --mode <mode>                   # Switch mode and resume
```

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

**Step 0.5: Parse Mode + Recovery Flags** — Extract the following from positional arguments:


The flag parser must run flag extraction BEFORE positional extraction so `/start --recover` (no agent name) binds to the current session's agent rather than being misinterpreted as `/start <agent-name=--recover>`.

**Step 0.6: Output-style preflight (autonomous mode only)** — When the parsed mode is `autonomous` (the default), read `.claude/settings.local.json` for the active `outputStyle` and warn the user IMMEDIATELY if it's set to `Explanatory`. The C7.7 Layer-B gate fires the same check later, but only AFTER all the long Phase A/B/C work (path prompts, permissions, init-mind, conventions, The Program, identity). Failing at C7.7 wastes ~30+ min of user investment. This preflight surfaces the collision before any state mutation.

Bash: `$(command -v py >/dev/null && echo "py -3" || echo python3) -c "import json,pathlib;p=pathlib.Path('.claude/settings.local.json');s=(json.loads(p.read_text(encoding='utf-8')).get('outputStyle') or '').strip().lower() if p.exists() else '';print(s)" 2>/dev/null`

(`py` is Windows-only; bare `py -3` died rc=127 on the first Linux deployment, 2026-08-24. py preferred where present — Windows `python3` can be the Store stub.)

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

# Rationale: core/config/rationale/start-preflight-and-recovery.md — The helper checks 6 signals (state == RUNNING, hea


   **IF exit code 0**: proceed to cleanup below.

   **IF exit code 2**: print "Refusing to recover: `runner-dead-check.sh`

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
   signals (--force):" followed by the helper's stderr per-condition list, then
   append the JSON audit record with the race-safe locked append below:

   ```bash
   AGENT_NAME="<agent-name>" HELPER_JSON='<full stdout JSON from runner-dead-check.sh>' py -3 -c "
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

- Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-claim.sh release --agent <agent-name> || true`
  DDB claim release with the crashed session's OLD on-disk runner-token
  (2026-07-07 bravo dual-runner follow-through). A crashed runner leaves its
  DDB row RUNNING; local recovery flips only LOCAL state, so without this
  release the fresh acquire below is held hostage by its OWN stale row for
  up to OWNERSHIP_STALE_SECONDS (~65 min post-calibration). MUST run BEFORE
  manifest-clear — `runner-token` is `recovery_action: clear`, so the old
  token is deleted by the next step. Token-conditional and idempotent: if a
  peer machine already stale-broke and re-claimed, the old token no longer
  matches and this is a no-op — it can never steal a peer's claim. Fail-open
  (`|| true`): a DDB hiccup must never block recovery.

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
# Rationale: core/config/rationale/start-preflight-and-recovery.md — Manual override: clear the recovery-circuit-breake

- On success: Output: "Recovered crashed runner session. Cleared stale signals and session files (manifest-driven). Proceeding with normal start."

Fall through to Step 1 — state is now IDLE, which routes to the IDLE branch below (which respects `--mode`, default autonomous). This reuses existing cleanup semantics instead of duplicating them.

**Authorization note**: `/start --recover` is the third authorized caller of `session-state-set.sh` alongside the existing `/start` (IDLE → RUNNING) and `/stop` (RUNNING → IDLE) paths. The cleanup above is a targeted /start sub-path; see `.claude/rules/user-interaction.md` Script-Level Restrictions.

**Step 1: Check Requested Agent's State** — The agent name comes from the `/start <name>` argument.
Check THIS agent's state specifically:

Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-get.sh`


**Step 1.5: UNINITIALIZED Drift-Warning Probe** — Defensive check for the
inlined-helper drift class: when Step 1 returns `UNINITIALIZED`, the agent dir
might genuinely not exist OR `session-state-get.sh` might carry a stale inlined
`_APD` constant relative to `_paths.sh`. Probe before trusting the verdict:

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

# Rationale: core/config/rationale/start-preflight-and-recovery.md — This is the Layer-A tactical defense (loud diagnos

## Behavior by Current State

### RUNNING (agent-state contains "RUNNING")


Branch on the **requested mode** (parsed in Step 0.5):

#### RUNNING + requested mode is `autonomous` (or no `--mode` flag)

Two scenarios produce RUNNING-on-disk:
  1. **Live runner** — another terminal is actively running the loop. This
     second terminal JOINS it as a Worker Body; it never ends at a refusal.
  2. **Zombie** — the previous session crashed without `/stop`; no live runner.

Distinguish them via the 6-condition zombie gate.
# Rationale (WHY inline instead of calling helper + WHY each condition): core/config/rationale/start-recovery-ceremony.md
**SSOT: `core/scripts/runner-dead-check.sh` — conditions MUST stay in sync.**

**Every probe call MUST use the explicit `MIND_AGENT=<agent-name>` prefix.**

Probe condition 2 (heartbeat):

Bash: `MIND_AGENT=<agent-name> bash core/scripts/heartbeat-stale.sh`

**IF output is `fresh`** → live reducer runner detected (scenario 1). This second
terminal activates as a **Worker Body** (asp-306 / g-306-73 one-mind-two-bodies):
do NOT stop here, do NOT print a refusal, and DO NOT auto-recover the reducer —
that would clobber the active loop. Proceed to the **Worker Body Activation
Sequence** below. The two commands next are alternatives a HUMAN may type
instead; neither is this session's next step.

**To take over the reducer role instead** (reducer genuinely wedged despite fresh
heartbeat): `/start <agent-name> --recover --force`

**To open a read-only/assistant observer window**: `/start <agent-name> --mode reader`
or `/start <agent-name> --mode assistant`

**Worker Body Activation Sequence:**

W-pre. **Ex-Worker Same-Terminal Guard** (g-306-210 — the RUNNING-branch half of
    Step 0-pre2 below, which sits in the IDLE branch and so never reached this path).

    **This step must run BEFORE W0** — not at W1 as first proposed. A refusal is
    only side-effect-free when it fires before the destructive write (guard-1813).

    Bash: `test -f "agents/<agent-name>/sessions/$MIND_SID/working-memory.yaml" && echo "EX_WORKER_FORK_PRESENT" || echo "no-fork"`

    IF output is `EX_WORKER_FORK_PRESENT`: STOP. Do NOT proceed to W0. Display
    the same fresh-terminal message as 0-pre2.


    IF output is `no-fork`: continue to W0.

W0. **Bind this session** (worker variant — MUST NOT touch `running-session-id` or
    `latest-session-id`; those are reducer-owned):

    Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; bash core/scripts/sid-collision-check.sh "<agent-name>" "$MIND_SID" || { echo "ERROR:SID_COLLISION"; exit 2; }; bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent "<agent-name>" --mode autonomous --retire-legacy >/dev/null && echo "BOUND_WORKER:$MIND_SID"`

    **HALT ON EMPTY MIND_SID** — display the same error as IDLE Step 0 below.
    **HALT ON SID_COLLISION** — display the same error as IDLE Step 0 below.

W0.4. **Write Body manifest** `--role worker` (FORK-BODY Phase 1B — g-306-62):

    Bash: `bash core/scripts/body-manifest.sh write --sid "$MIND_SID" --agent "<agent-name>" --role worker >/dev/null || echo "[start-worker] body-manifest write failed (non-fatal)" >&2`


W1. **Fork canonical WM** (FORK-BODY Phase 1B — private WM snapshot for the worker):

    Bash: `mkdir -p "agents/<agent-name>/sessions/$MIND_SID" && cp "agents/<agent-name>/session/working-memory.yaml" "agents/<agent-name>/sessions/$MIND_SID/working-memory.yaml" 2>/dev/null && echo "WM_FORKED" || echo "[start-worker] WM fork failed — worker starts with empty WM (degraded but operational)" >&2`


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


**IF recent_block_rc != 1** → runner appears alive (rc=0) OR probe errored
(rc=2+). Either way DO NOT auto-recover. Output the same "live runner detected"
message as the heartbeat=fresh path. DONE. No state changes.

**IF recent_block_rc == 1** → no recent BLOCK; continue to conditions 3 and 4:

Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-signal-exists.sh stop-requested; echo "stop_req_rc=$?"`
Bash: `MIND_AGENT=<agent-name> bash core/scripts/background-jobs.sh has-pending; echo "bg_jobs_rc=$?"`


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

  Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-claim.sh release --agent <agent-name> || true`
  DDB claim release with the crashed session's OLD on-disk runner-token —
  same rationale as Step 0.7 (2026-07-07 bravo dual-runner follow-through):
  a crashed runner's DDB row stays RUNNING, and without this release the
  fresh acquire below is blocked by its OWN stale row for up to
  OWNERSHIP_STALE_SECONDS (~65 min). MUST run BEFORE manifest-clear
  (`runner-token` is `recovery_action: clear`). Token-conditional +
  idempotent — a peer's re-claimed row has a different token, so this can
  never steal a peer's claim. Fail-open.

  Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-manifest-clear.sh`
  Runs AFTER state-set IDLE succeeded; cleanup window now shows
  state=IDLE + sid present (mirrors aspirations-graceful-stop D1→D6).

# Rationale: core/config/rationale/start-observer-and-worker-activation.md — On success, output

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

# Rationale: core/config/rationale/start-observer-and-worker-activation.md — The observer session coexists with the autonomous

0-pre. **Ex-Worker Same-Terminal Guard** (g-306-210 — the observer half of Step
   0-pre2 below, which sits in the IDLE branch and so never reached a reader/
   assistant bind taken while the agent is RUNNING).

   "Observers never fork a WM" is true of what an observer WRITES and says
   nothing about what it INHERITS. `bash-agent-inject.py` keys `BODY_ROLE=worker`
   + `BODY_WM_PATH` on the fork file's EXISTENCE, not on this session's role, so
   an observer binding on an ex-worker SID is mislabeled a worker for its whole
   lifetime and every `wm-*.sh` write it makes lands in the dead Body's fork.
   Step 0.4 then writes `--role observer`, which RESETS that manifest to
   `role: observer` / `body_state: active` / `forked_wm_hash: null` — so the
   cleanup sweep's preserve path later stages a fork with no hash sidecar, and
   `body-merge` takes its documented degraded branch (no hash → the
   never-diverged short-circuit is skipped, so an orphan merges as if divergent)
   with observer scribble mixed in. Read-only in intent, corrupting in effect.

   Bash: `test -f "agents/<agent-name>/sessions/$MIND_SID/working-memory.yaml" && echo "EX_WORKER_FORK_PRESENT" || echo "no-fork"`

   IF output is `EX_WORKER_FORK_PRESENT`: STOP. Do NOT proceed to Step 0. Display
   the same fresh-terminal message as 0-pre2. Placement before Step 0 is
   load-bearing for the same reason as W-pre (guard-1813): the manifest reset at
   Step 0.4 is the destructive write, so a refusal after it protects nothing.

   IF output is `no-fork`: continue to Step 0.

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
# Rationale: core/config/rationale/start-observer-and-worker-activation.md — initial status=active record so this observer sess
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

# Rationale: core/config/rationale/start-runner-claim-and-body-fork.md — The explicit AYOAIAGENT= prefix is REQUIRED — the

     - Read the current on-disk mode: Bash:
       > Detected an autocompact-interrupted graceful stop and completed its
       > consolidation/handoff first so no learning is lost. Re-run
       > `/start <agent-name> [--mode <mode>]` to resume.
       and only the D7.1 checkpoint-clear was missed): Bash:

0-pre2. **Ex-Worker Same-Terminal Guard (msg-20260804-220643-alpha-5346)**

   A session whose SID previously ran as a WORKER Body cannot cleanly bind
   here in ANY mode — and must NEVER become the reducer. The per-SID fork
   file `agents/<agent-name>/sessions/$MIND_SID/working-memory.yaml`
   survives wind-down BY DESIGN (generalize-down gates re-merge on manifest
   `body_state`, not file absence; the only unlink in body-merge is the
   staged-copy set, a different file family), and `bash-agent-inject.py`
   exports `BODY_ROLE=worker` + `BODY_WM_PATH` on that file's EXISTENCE for
   EVERY Bash call in this session — the hook itself documents the hazard at
   its export site. Proceeding to the IDLE→RUNNING flip would produce a
   MISLABELED REDUCER that reports success: state flips RUNNING, the DDB
   claim is acquired, "Agent resumed" prints — but reducer-only writes stay
   suppressed and reducer WM writes land in the fork (measured live
   2026-08-04, alpha on DESKTOP-O91DLK2, SID 301a45f2: `BODY_ROLE=worker`
   verified in the live environment of the wound-down session). Reader/
   assistant binds inherit the same mislabel, so the refusal is mode-wide.
   **Mode-wide is a claim about SCOPE, not about this step's reach** — THIS
   step guards only the IDLE branch. The two other binding paths carry their
   own copy of the probe, placed ahead of their own first destructive write:
   RUNNING-worker at `W-pre` and RUNNING-observer at `0-pre` (both g-306-210).
   The UNINITIALIZED branch is deliberately UNGUARDED, and that covers BOTH of
   its fork-capable paths — Phase A-0 transplant-resume and the cross-box worker
   (which forks unconditionally: `write_manifest` sets `fork_needed = True`
   whenever `reducer_sid == "remote"`). Neither can meet a pre-existing fork on
   a FIRST bind, because reaching one requires a clone carrying a
   `sessions/<SID>/working-memory.yaml` whose SID collides with a fresh
   session's on the destination box, and SIDs are per-machine per-session. Every
   SUBSEQUENT bind on a cross-box worker box lands in the IDLE branch — the box
   stays IDLE by design and never writes `running-session-id` — so THIS step is
   what guards the re-activation case there. Stated rather than built; if a
   transplant is ever observed carrying live session dirs, this is the
   assumption to revisit first.
   The refusal is deliberate — there is NO safe in-place cleanup: deleting
   the fork from inside the mislabeled session is a reducer-only operation
   performed by a non-reducer, with unmerged divergence as the stake.

   Bash: `test -f "agents/<agent-name>/sessions/$MIND_SID/working-memory.yaml" && echo "EX_WORKER_FORK_PRESENT" || echo "no-fork"`

   IF output is `EX_WORKER_FORK_PRESENT`: STOP. Do NOT proceed to Step 0.
   Display:
   > ⚠ This terminal's session (SID `$MIND_SID`) previously ran as a WORKER
   > Body for `<agent-name>`. Its fork file still exists, and the Bash hook
   > keys `BODY_ROLE=worker` on that file for the lifetime of this session —
   > so this session cannot become the reducer (or bind cleanly in any mode).
   >
   > Open a NEW terminal and run `/start <agent-name>` there — a fresh SID
   > has no fork file. Any unmerged divergence from the worker run is safe:
   > its `body-manifest.yaml` records the state, and a
   > `closed-pending-merge` body is merged automatically at the next
   > reducer consolidation.
   DONE.

   IF output is `no-fork`: continue to Step 0.

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
# Rationale: core/config/rationale/start-runner-claim-and-body-fork.md — initial status=active record for THIS session so i
   Bash: `TSID="$MIND_SID" TAGENT="<agent-name>" TMODE="<target-mode>" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_open; write_open(sid=os.environ["TSID"], agent=os.environ["TAGENT"], mode=os.environ["TMODE"], started_by="claude-code")' >/dev/null 2>&1 || true`


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
# Rationale: core/config/rationale/start-runner-claim-and-body-fork.md — (Ensure runtime daemon is up before any wrapper ca
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
   - (DDB-heartbeat ordering, g-328-31: the first `heartbeat-tick.sh` — which
     under own-cloud ALSO fires the DDB `runner-claim.sh heartbeat` — is
     deliberately DEFERRED to AFTER the DDB acquire below (and before the RUNNING
     flip). A DDB heartbeat run BEFORE the acquire, using a leftover runner-token
     from a prior session whose release did not confirm, refreshes a STALE claim's
     `heartbeat_at` and defeats the acquire's §5 stale-lock-break — pinning this
     /start at rc=4 on its own stale claim. See the heartbeat-tick step just
     before `session-state-set.sh RUNNING` below.)
   - Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; RUNNER_TOKEN=$(py -3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null || python3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null); [ -n "$RUNNER_TOKEN" ] || { echo "ERROR:RUNNER_TOKEN_GEN_FAILED"; exit 3; }; AGENT_STATE_DIR="agents/<agent-name>/session"; mkdir -p "$AGENT_STATE_DIR" && echo "$MIND_SID" > "$AGENT_STATE_DIR/running-session-id.tmp" && mv "$AGENT_STATE_DIR/running-session-id.tmp" "$AGENT_STATE_DIR/running-session-id" && echo "$MIND_SID" > "$AGENT_STATE_DIR/latest-session-id.tmp" && mv "$AGENT_STATE_DIR/latest-session-id.tmp" "$AGENT_STATE_DIR/latest-session-id" && echo "$RUNNER_TOKEN" > "$AGENT_STATE_DIR/runner-token.tmp" && mv "$AGENT_STATE_DIR/runner-token.tmp" "$AGENT_STATE_DIR/runner-token" && echo "RUNNER_TOKEN=$RUNNER_TOKEN"`
     (Canonical runner-claim: writes THREE files atomically — `running-session-id`, `latest-session-id`, and `runner-token` — into `agents/<agent-name>/session/`. The Phase 2.5.D `agents/` parent prefix MUST be in the heredoc path; without it, the writes land at `agents/<agent-name>/session/` at PROJECT_ROOT (the 2026-05-19 bravo/ cruft incident — the L1 hook only gates Write/Edit, not Bash heredoc writes, so a missing `agents/` prefix silently creates a directory at the wrong root). The first two files hold the Claude Code SID (routing identity used by stop-hook). The third is a FRAMEWORK-OWNED UUID4 (uniqueness identity) — protects against Claude Code reusing a session_id across windows via `claude --continue` / `--resume`. With the token, every BLOCK and watchdog event records the runner-instance identity, so a SID-collision shows up as "same SID, different runner-token" in `core/logs/stop-hook.log` and watchdog events instead of silent corruption. The 2026-05-12 cross-binding incident was invisible to forensics without this signal. DO NOT split these writes into separate Bash commands — the triple-write is the atomic unit. DO NOT remove the `RUNNER_TOKEN_GEN_FAILED` halt; without a token, the loop runs with no uniqueness anchor. Per rb-323/guard-403, observer-paired signals MUST be seeded BEFORE the state-set RUNNING below — same race rb-323 identified for heartbeat-tick. If RUNNER_TOKEN_GEN_FAILED here, state stays IDLE (clean retry); if state-set ran first, state would be RUNNING with no SID files and Path B would have to recover.)

     **HALT ON RUNNER_TOKEN_GEN_FAILED** — if output contains `ERROR:RUNNER_TOKEN_GEN_FAILED`, STOP. Both `py -3` and `python3` failed to generate a UUID. Display to the user:
     > Cannot start agent `<agent-name>`: the framework-owned runner-token could not be generated (Python unavailable). Check that `py -3` or `python3` works; the runner-token is required for SID-collision detection.

# Rationale: core/config/rationale/start-runner-claim-and-body-fork.md — - DDB runner-claim acquire (single-runner lifecycl
     Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-claim.sh acquire --agent <agent-name>; echo "ACQUIRE_RC=$?"`

     **ACQUIRE_RC=4 + `reducer_only` → HALT and display the refusal.**
     **ACQUIRE_RC=4 otherwise →

     > Reducer for `<agent-name>` is alive on `<machine_id>` (heartbeat <age>s) —
     > joining as a SECOND BODY from this box. This worker executes goals; the
     > reducer keeps encode/reflect/consolidate. To move the reducer here
     > instead: /stop <agent-name> on <machine_id>, then /start here. To refuse
     > the auto-join next time: /start <agent-name> --reducer-only.

     Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-claim.sh status --agent <agent-name>`

     > Cannot start `<agent-name>` in autonomous mode — another machine holds a live
     > runner claim (DDB session-lock).
     > Holder: <machine_id>, heartbeat <age>s old (threshold <stale_after>s).
     > `--recover --force` will NOT help from this box: Step 0.7 precondition 1
     > requires the LOCAL `agent-state` to read RUNNING, and `agent-state` is
     > `sync_tier: machine_local`, so on THIS box it reads IDLE and recovery exits
     > "Nothing to recover". The RUNNING-observer branch is unreachable cross-box for
     > the same reason — it reads local state that the owning box's RUNNING never
     > reaches.
     > The three real options:
     >   /start <agent-name>                 — bare re-issue auto-joins as a SECOND
     >                                          body from this box (executes goals;
     >                                          the reducer keeps
     >                                          encode/reflect/consolidate)
     >   /stop <agent-name> on <machine_id>  — then re-issue /start here to move the
     >                                          reducer to this box
     >   wait out OWNERSHIP_STALE_SECONDS (~65 min) and re-issue /start, which then
     >   reclaims the stale claim via the acquire's §5 stale-lock-break.




     **CW-pre — delete ALL THREE reducer-shaped files FIRST, before anything else.**
     Bash: `AGENT_STATE_DIR="agents/<agent-name>/session"; rm -f "$AGENT_STATE_DIR/running-session-id" "$AGENT_STATE_DIR/latest-session-id" "$AGENT_STATE_DIR/runner-token"; echo "CW_PRE_CLEARED"`

     **CW0 — display holder identity** (informational; rc=4 already proved liveness):
     Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-claim.sh status --agent <agent-name>`
     Show the user which box holds the reducer they are joining.

     **CW0.5 — bind the session.** Same as the reducer path's W0:
     Bash: `bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent <agent-name> --mode autonomous --retire-legacy`
     Bash: `bash core/scripts/sid-collision-check.sh <agent-name> "$MIND_SID"; echo "SIDCOL_RC=$?"`
     **HALT on an empty `$MIND_SID`, on `SIDCOL_RC=2` (collision — either
     `ERROR:SID_COLLISION` cross-agent or `ERROR:SID_COLLISION_SAME_AGENT`), and on
     `SIDCOL_RC=1` (script/usage error).** Only rc=0 continues.

     **CW1a — force-fresh the canonical WM from the shared store.** The fork must
     start from the reducer's LATEST push, not this box's read-through cache:
     Bash: `bash core/scripts/backend-cat.sh cat agents/<agent-name>/session/working-memory.yaml > agents/<agent-name>/session/working-memory.yaml.fresh && mv agents/<agent-name>/session/working-memory.yaml.fresh agents/<agent-name>/session/working-memory.yaml && echo "CW1A_FRESH_OK"`
     (The `cat` SUBCOMMAND is required — `backend-cat.sh <path>` with no subcommand
     exits 2 (usage). Verified live on this box: the subcommand form returns rc=0 and
     74020 bytes; the bare form returns rc=2 and zero. Under own-cloud `cat` routes to
     `read_authoritative_bytes`, a PURE to-memory read of the shared store that never
     mutates the local mirror — which is exactly why the redirect+`mv` in the command
     above is what actually refreshes the mirror; `cat` alone would print fresh bytes
     and leave the stale file in place.
     Under own-cloud the local tree is a read-through CACHE — a file this box has
     never read does not materialize locally, and one it read hours ago is stale
     (guard-980). Forking a stale mirror would hand the reducer a merge baseline that
     never existed on either box, silently mis-attributing every counter delta.
     Write-to-temp-then-`mv` so a failed fetch cannot truncate the local WM. If this
     step fails, HALT — proceeding would fork from the stale cache, which is the one
     outcome this step exists to prevent.)

     **CW1b — write the worker body manifest** (forces the fork):
     Bash: `bash core/scripts/body-manifest.sh write --sid "$MIND_SID" --agent <agent-name> --role worker --reducer-sid remote`

     **CW2 — session telemetry** (fire-and-forget; identical idiom to W0.5 above,
     Bash: `TSID="$MIND_SID" TAGENT="<agent-name>" TMODE="worker" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_open; write_open(sid=os.environ["TSID"], agent=os.environ["TAGENT"], mode=os.environ["TMODE"], started_by="claude-code")' >/dev/null 2>&1 || true`
     (`|| true` — telemetry must never abort an activation.)

     **CW3** — `Skill(worker-loop)`. Do NOT set `agent-state` RUNNING, do NOT write
     `running-session-id`/`runner-token`, do NOT touch the runner claim, and do NOT
     run `/boot`. State after CW3: `agent-state` IDLE, no reducer-shaped files here.

   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/team-state-update.sh --field "agent_status.<agent-name>.current_focus" --value "\"\"" || true`
     (Clear stale current_focus from the previous session's shutdown — without this,
     a partner reading team-state.yaml sees the prior session's "session ended" or
     stale focus value indefinitely. Convention coordination.md:275 is retrospective
     ("set on completion"), so we can't write a prospective "starting" — clearing to
     "" is the convention-aligned signal of "no completion yet this session". The
     first aspirations-state-update or aspirations-consolidate write populates
     current_focus with the first real completion. Fail-open with `|| true` so a
     team-state write failure never blocks the RUNNING transition — stderr is NOT
     suppressed, so write errors surface. ORDERING (g-115-4653): this is the FIRST
     shared/synced write in the sequence and MUST stay below the DDB acquire above.
     It was previously the first write of the whole autonomous branch, so an rc=4
     refusal blanked the LIVE owning box's focus before ever discovering it had lost
     the claim.)
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/team-state-update.sh --field "agent_status.<agent-name>.session_ended" --value "false" || true`
     (Clear stale session_ended from the previous /stop — g-240-72. Without this,
     a partner reading team-state.yaml sees session_ended=true for a live agent
     and may make wrong concurrency assumptions. /stop sets session_ended=true on
     graceful exit; the field then persists until the next /start. We clear here
     instead of removing the field so the absence-vs-false distinction stays
     unambiguous to partner readers. Same fail-open semantics as current_focus
     above — write failure must not block the RUNNING transition. Same g-115-4653
     ordering constraint: shared/synced, so it MUST stay below the acquire.)

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
# Rationale: core/config/rationale/start-runner-claim-and-body-fork.md — (F4 reorder, 2026-05-20: moved BEFORE the state-se
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
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/heartbeat-tick.sh --bypass-state`
     (FIRST heartbeat — seeds `runner-heartbeat` mtime AND stamps team-state
     `last_active` NOW, and under own-cloud ALSO fires the DDB
     `runner-claim.sh heartbeat`. MOVED here from before the triple-write
     (g-328-31) so it runs AFTER the DDB acquire above and BEFORE the RUNNING
     flip below. Ordering rationale: the DDB heartbeat MUST NOT precede the
     acquire — a heartbeat carrying a leftover token from a prior session
     refreshes a STALE claim's `heartbeat_at`, defeating the acquire's §5
     stale-lock-break and pinning the next /start at rc=4 (stale-self-claim).
     Acquiring first lets §5 reclaim the genuinely-stale claim; THIS heartbeat
     then refreshes the just-acquired claim with the fresh token from the
     triple-write. Still precedes the RUNNING transition to close the
     observer-probe race (state=RUNNING with a stale heartbeat/last_active) per
     rb-323/guard-403 — both observer-paired signals (heartbeat here, triple-write
     above) are seeded before the flip. `--bypass-state` is REQUIRED because state
     is still IDLE here; the gate in `heartbeat-tick.sh` refuses bare ticks against
     IDLE (the `heartbeat_without_running` desync class, alpha 2026-05-13
     cbb27ab3). DO NOT add a separate `team-state-update.sh ... last_active` line;
     it duplicates the write heartbeat-tick just performed. On an acquire HALT
     (rc=4) above, this heartbeat never runs — a failed acquire leaves no
     heartbeat side-effect, which is the point.)
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

# Rationale: core/config/rationale/start-runner-claim-and-body-fork.md — The UNINITIALIZED first-boot ceremony (Phase A-0 t

**Read `core/config/start-uninitialized-ceremony.md` and follow it in order**




**Read `core/config/start-phase-c.md` and follow the section matching the mode
confirmed above** (Reader / Assistant / Autonomous) — it holds the full first-boot
sequence for all three modes.

## Chaining
