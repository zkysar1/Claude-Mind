---
name: stop
description: "Stops the autonomous learning loop gracefully: writes stop-requested for Phase -1.4, waits for in-flight obligations (verify, state-update), consolidates session state via /aspirations-consolidate, drops the agent to assistant mode by default (or reader with --reader), and transitions state to IDLE. USER-ONLY — Claude must NEVER invoke /stop. Fires only when the user types /stop <agent-name> [--reader]. Agent name is REQUIRED — refuses with an error and a list of available agents otherwise. Preserves handoff.yaml for the next session to resume."
triggers:
  - "/stop"
conventions: [session-state, handoff-working-memory]
minimum_mode: any
revision_id: "skill-bootstrap-stop-ae82e1"
previous_revision_id: null
---

# /stop -- Stop the Autonomous Learning Loop

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

## Syntax

```
/stop <agent-name>          # Stop the named agent + drop to assistant (reconciliation-ready)
/stop <agent-name> --reader # Stop the named agent + drop to reader (read-only, walking away)
```

**Agent name is REQUIRED.** A bare `/stop` (no positional argument) is refused with a
clear error and the list of available agent directories. This prevents the cross-session
"wrong-agent stop" failure mode where a session's `.active-agent-<SID>` binding has
been silently overwritten or cleared (e.g., NO_AGENT window, or another `/start` rebinding
the SID), leaving `/stop` no safe default. Requiring the explicit name also enables stops
from any terminal — a side benefit, not a workaround. (Incident: 2026-04-24 — bare `/stop`
typed in a NO_AGENT window stopped the wrong agent because the binding fell back to a
different active agent's session.)

After stop, the agent lands in **assistant** mode by default so the user can immediately
reconcile state (mark a missed goal, edit a tree node, add a guardrail) without a mode
switch. Pass `--reader` for the read-only safe floor when walking away.

**Step 0: Load Conventions** -- `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded -- proceed to next step.

**Step 0.5: Parse arguments** — flag-parsing runs before positional-parsing so `/stop --reader` (no agent-name) is not mis-interpreted as `/stop <agent-name=--reader>`.

1. **Flag parsing**: If any argument is the literal string `--reader`, set `target_mode = "reader"`. Otherwise `target_mode = "assistant"`. This determines where the agent lands after the stop completes. Unknown flags are ignored (user will see the default output and can re-try).

2. **Agent name resolution (REQUIRED)**: Take the first argument that does NOT start with `--` as `<agent-name>`. The positional argument is mandatory — there is NO fallback to current session binding.

   a. **No positional argument present** → REFUSE with the available-agents list, then DONE (no state mutation, no signal write):
      Bash: `ls -d */session/agent-state 2>/dev/null | awk -F/ '{print $1}' | paste -sd ", " -`
      Output: `"Error: /stop requires an explicit agent name. Usage: /stop <agent-name> [--reader]. Available agents: <list-from-bash>. (Why mandatory: the prior 'use current session binding' default silently stopped the wrong agent on 2026-04-24 when the binding had been overwritten. Explicit names also enable stops from any terminal.)"`
      DONE.

   b. **Positional argument present but agent directory missing** → REFUSE with the same available-agents list:
      Bash: `ls agents/<agent-name>/session/agent-state 2>/dev/null` (check existence)
      IF missing:
          Bash: `ls -d */session/agent-state 2>/dev/null | awk -F/ '{print $1}' | paste -sd ", " -`
          Output: `"Error: Agent '<agent-name>' not found or has no session state. Available agents: <list-from-bash>."`
          DONE.

   c. **Positional argument present and agent directory exists** → rebind this session to the named agent: overwrite `.active-agent-<SID>` with `<agent-name>` so the PreToolUse[Bash] hook auto-injects `MIND_AGENT=<agent-name>` on subsequent calls. If you need a deliberate cross-agent probe inside a single command (e.g., reading a third agent's state), write `MIND_AGENT=<other> <cmd>` explicitly — the hook preserves explicit overrides.

**Step 1: Check State** -- Bash: `session-state-get.sh`
(Step 0.5 has already rebound this session to `<agent-name>`, so the PreToolUse hook auto-injects `MIND_AGENT=<agent-name>` and this read targets the correct agent.)

## Behavior by Current State

### RUNNING

Graceful two-phase stop. The agent finishes its current iteration's obligations
(verify, state-update, learning checks) before stopping. No learning is lost.

**How it works**: This skill sets a `stop-requested` signal but does NOT change state
to IDLE. It then deterministically chains into the aspirations loop as its final action
(Step 5), so Phase -1.4 runs inside the same user turn: in-flight obligations from the
iteration checkpoint complete, then the full stop sequence (IDLE, consolidation, cleanup,
target mode) runs to completion. The Stop hook's BLOCK path remains as a safety net if
the in-turn chain is interrupted, but the normal path no longer depends on it — typing
`/stop` now self-completes without the user having to prompt "continue".

1. Write target mode (ALWAYS — runs before the idempotent guard so a user who types
   `/stop <agent-name>` and then `/stop <agent-name> --reader` can change their mind
   before Phase -1.4 reads the file. `target_mode` comes from Step 0.5 flag parsing.):
   Bash: `echo "<target_mode>" > agents/<agent>/session/stop-target-mode`

   **Do not move this below the idempotent guard** — Phase -1.4 depends on this file
   existing when it runs (no fallback in D7). Any new caller of `session-signal-set.sh
   stop-requested` outside `/stop` MUST also write `stop-target-mode` first.

2. Idempotent guard (do not re-set an existing signal, but still chain the loop):
   Bash: `session-signal-exists.sh stop-requested`
   IF exit 0 (signal already exists):
       A previous /stop set the signal but graceful-stop D3 never cleared it (if D1
       had run, state would be IDLE and Step 1 would have routed us to the IDLE
       branch). Update the user, skip Step 3 to avoid a redundant set, and fall
       through to Steps 4 and 5 so this invocation still chains into the loop.
       Output: "Stop signal was already set — target mode updated to <target_mode>.
       Resuming graceful stop now."
       SKIP Step 3. Continue to Step 4.

3. Set the signal (only if Step 2 did not skip):
   Bash: `session-signal-set.sh stop-requested`

4. Output:
   "Stop requested — finishing current obligations and stopping now. You'll see progress
   updates as each step completes. Will land in <target_mode> mode."

5. **Chain into the aspirations loop — RUNNER SESSION ONLY.**

   Graceful-stop D1 writes `agent-state` and D7 writes `agent-mode`. Observer sessions
   (started via `/start <agent> --mode reader|assistant` while the loop was RUNNING)
   MUST NOT touch those files — that is the observer contract. So the chain is
   gated on session identity: only the session whose SID matches `running-session-id`
   runs graceful-stop in-turn. Observer sessions stop here; the runner's Stop hook will
   pick up the signal at its next iteration boundary.

   Runner detection compares `$MIND_SID` (exported on every Bash call by the
   PreToolUse hook — `core/scripts/bash-agent-inject.py`) against the saved
   `running-session-id`. MIND_SID is the ONLY authoritative "this session's
   SID" — reading stale per-agent files like `latest-session-id` as a proxy is
   what caused the 2026-04-20 hang (observer clobber desynced the two files
   and `/stop` routed the runner to the observer branch for 101 seconds).

   DO NOT add a heartbeat-fresh refresh-and-trust fallback here. The
   detection logic CANNOT distinguish "I'm the runner with stale running-
   session-id" from "another terminal is the runner ticking the heartbeat";
   heartbeat freshness only proves some session is running, not which one.
   Autocompact rotation is handled at SessionStart by session-save-id.sh's
   four-witness gate — if running-session-id is stale, that gate failed
   upstream. Fix the gate, do not add a defensive write here.

   Bash: `runner_sid=$(cat agents/<agent>/session/running-session-id 2>/dev/null | tr -d '\r\n'); if [ -z "$runner_sid" ] || [ "$MIND_SID" = "$runner_sid" ]; then echo "runner"; else echo "observer"; fi`

   IF output is "observer":
       # Two-way heartbeat probe (pure mtime):
       #   fresh → runner is alive; leave the signal for its next iteration.
       #   stale → runner crashed; route user to `/start --recover`.
       Bash: `bash core/scripts/heartbeat-stale.sh`

       IF output is "stale":
           Output: "Stop signal set, but runner session appears crashed (last
           heartbeat older than the staleness threshold in
           core/config/aspirations.yaml → runner_heartbeat.stale_minutes).
           The signal will not be picked up by the dead runner. Run
           `/start <agent-name> --recover` to force cleanup."
           DONE. Do not chain.

       # output is "fresh" — normal observer path
       Output: "Stop signal set. The runner session will pick it up at its
       next iteration."

       # Plan v1 step 0.10 (2026-05-19): clean up this observer session's
       # SID binding at PROJECT_ROOT so it doesn't accumulate as cruft.
       # The runner has its own binding (cleaned by graceful-stop D7.1);
       # this one was created by /start --mode reader|assistant from the
       # RUNNING branch and has no other cleanup path. Idempotent — rm -f.
       Bash: `rm -f ".active-agent-$MIND_SID"`
       DONE. The signal is set; runner takes it from here. Do not chain.

   IF output is "runner":
       Skill: `aspirations` with args `loop`

       The aspirations skill enters, Phase -1.4 detects `stop-requested`, delegates to
       `/aspirations-graceful-stop`, which runs GS-1 (checkpoint recovery — typically
       a no-op when stop is typed between iterations) then GS-2 D1–D7 (state → IDLE,
       set `stop-loop`, consolidate, session cleanup, then apply `stop-target-mode`
       and emit the final stop-complete message in one merged step). Everything runs
       to completion before the turn ends; no "continue" nudge required.

       **Why chain explicitly instead of relying on the Stop hook BLOCK?** The Stop
       hook still BLOCKs when state is RUNNING and `stop-loop` is absent, but in
       interactive mode the CLI does not reliably trigger a new model turn when the
       preceding turn ended in text-only output. A direct Skill invocation is a
       deterministic handoff — the model follows the chain in-turn rather than
       waiting for the harness to re-invoke it.

### IDLE (assistant or reader mode)
1. Check current mode: Bash: `session-mode-get.sh`
2. If current mode != `target_mode` (from Step 0.5):
   Bash: `session-mode-set.sh <target_mode>`
3. Output:
   IF target_mode == "assistant":
     "Mode set to assistant (reconciliation-ready). You can mark goals complete,
     edit tree nodes, or add guardrails without ceremony. `/start <agent-name>` resumes
     autonomous; `/stop <agent-name> --reader` for walk-away safety next time."
   ELSE (target_mode == "reader"):
     "Mode set to reader (read-only). `/start <agent-name> --mode assistant` to make
     edits; `/start <agent-name>` to resume autonomous."
4. **Clean up this session's SID binding** (plan v1 step 0.10, 2026-05-19): the
   binding file at PROJECT_ROOT/.active-agent-$MIND_SID was created by /start
   (or rebound by Step 0.5c above). The RUNNING branch's runner-session path
   cleans its binding via graceful-stop D7.1; the IDLE branch never reached
   D7.1 and accumulates a stale binding for each /stop. Delete it so the
   PROJECT_ROOT doesn't bloat with one file per stopped session. The next
   `/start` will re-create as needed. Idempotent — rm -f is safe even if a
   prior /stop already cleaned it.
   Bash: `rm -f ".active-agent-$MIND_SID"`

Note: `/stop <agent-name>` (no `--reader`) from IDLE-reader PROMOTES to assistant (the
post-stop default). This is intentional — slash commands imply active user presence, so
CLI defaults favor the active mode. Use `/stop <agent-name> --reader` to explicitly stay
in / drop to reader.

### UNINITIALIZED
Output: "Agent has not been started yet. Type `/start <name>` to begin."

## Chaining
- Sets: `stop-target-mode` file, `stop-requested` signal
- Calls (RUNNING branch, runner session only): `Skill: aspirations` with args `loop` as
  the final action, so Phase -1.4 runs in the same user turn and the graceful stop
  (D1–D7) completes before the turn ends. Observer sessions skip the chain and leave
  the work for the runner's Stop-hook re-entry path.
- Does NOT call: /aspirations-consolidate directly (that's reached via Phase -1.4 D4)
- Called by: User only. NEVER by Claude.
