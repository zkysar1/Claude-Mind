---
name: stop
description: "Stops the autonomous learning loop gracefully: writes stop-requested for Phase -1.4, waits for in-flight obligations (verify, state-update), consolidates session state via /aspirations-consolidate, drops the agent to assistant mode by default (or reader with --reader), and transitions state to IDLE. USER-ONLY — Claude must NEVER invoke /stop. Fires only when the user types /stop {agent-name} [--reader]. Agent name is REQUIRED — refuses with an error and a list of available agents otherwise. Preserves handoff.yaml for the next session to resume."
triggers:
  - "/stop"
disable-model-invocation: true
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
      Bash: `source core/scripts/_paths.sh && ls -d "$(agents_root)"/*/session/agent-state 2>/dev/null | awk -F/ '{print $(NF-2)}' | paste -sd ", " -`
      (Agent dirs live under `agents_root`, not the repo root. The old `*/session/agent-state` glob from the repo root matched nothing, so this refusal printed an empty agent list.)
      Output: `"Error: /stop requires an explicit agent name. Usage: /stop <agent-name> [--reader]. Available agents: <list-from-bash>. (Why mandatory: the prior 'use current session binding' default silently stopped the wrong agent on 2026-04-24 when the binding had been overwritten. Explicit names also enable stops from any terminal.)"`
      DONE.

   b. **Positional argument present but agent directory missing** → REFUSE with the same available-agents list:
      Bash: `ls agents/<agent-name>/session/agent-state 2>/dev/null` (check existence)
      IF missing:
          Bash: `source core/scripts/_paths.sh && ls -d "$(agents_root)"/*/session/agent-state 2>/dev/null | awk -F/ '{print $(NF-2)}' | paste -sd ", " -`
          Output: `"Error: Agent '<agent-name>' not found or has no session state. Available agents: <list-from-bash>."`
          DONE.

   c. **Positional argument present and agent directory exists** → rebind this session to the named agent: overwrite `.active-agent-<SID>` with `<agent-name>` so the PreToolUse[Bash] hook auto-injects `MIND_AGENT=<agent-name>` on subsequent calls. If you need a deliberate cross-agent probe inside a single command (e.g., reading a third agent's state), write `MIND_AGENT=<other> <cmd>` explicitly — the hook preserves explicit overrides.

**Step 0.6: Worker-Body short-circuit (g-306-125) — MUST run before Step 1.**

On a cross-box run this agent has TWO kinds of live Body: the REDUCER, which owns
the agent-wide state, and one or more WORKERS. Everything below Step 1 is an
AGENT-WIDE write — `stop-target-mode`, the `stop-requested` signal, `agent-state`,
`agent-mode`, the goal claim. A `/stop` typed on a WORKER box must perform none of
them. `stop-requested` is the sharp one: it is read by the REDUCER's Phase -1.4, so
setting it from a worker stops the wrong Body on a different machine while the user
believes they stopped only the box in front of them. A worker winds down when its
reducer stops (the reducer-liveness poll) or through the worker branch below, and
never through an agent-wide file.

This check sits ABOVE Step 1 rather than inside the RUNNING branch on purpose. The
IDLE branch writes agent-wide state too (`session-mode-set.sh`, Step 2 there), and a
worker whose reducer has already stopped reads state=IDLE — so a RUNNING-only guard
would leak the mode write.

Detection uses the body-WM predicate: `sessions/<SID>/working-memory.yaml` exists
ONLY for a non-reducer Body. That is the invariant `core/scripts/bash-agent-inject.py`
documents and itself routes on, so deriving it here locally keeps the two predicates
identical and unable to drift. `$MIND_SID` is this session's own SID and IS available
here, because every step in this skill is a Bash TOOL call and the PreToolUse hook
injects it — the same basis Step 5's runner detection already relies on. Do NOT
rewrite this as a `BODY_ROLE` env check: that variable happens to be present in THIS
context for the same reason, but it is absent in every non-Bash-tool hook, and keying
a rail on it is the inert-rail class `guard-2445` exists to prevent. COUPLING, stated
so it fails loudly: if a REDUCER ever gains a `sessions/<sid>/working-memory.yaml`,
this predicate misclassifies it as a worker and `/stop` becomes a no-op on the box
that owns the state.

THE EMPTY-`MIND_SID` CASE IS A THIRD ANSWER, NOT THE NEGATIVE ONE (g-115-9320,
guard-6178). Written as a single `[ -n "$MIND_SID" ] && [ -f ... ]`, this predicate
returns the ELSE label when the variable is merely ABSENT — so a guard that CANNOT
EVALUATE reports `reducer-or-single`, the branch that writes the AGENT-WIDE
`session/stop-requested` a worker is forbidden to touch. The asymmetry is what makes
it a defect rather than a default: the two branches do not have equal blast radii, so
guessing toward the destructive one converts a failed hook into the dangerous action.
MEASURED 2026-09-07 (DESKTOP-O91DLK2, SID 1c4a1179): the PreToolUse inject hook fired
with BOTH `MIND_SID` and `MIND_AGENT` empty, and this step printed `reducer-or-single`
for a Body whose `sessions/<SID>/body-manifest.yaml` reads `role: worker,
body_state: active`. Re-running with an explicit SID printed `worker`.
Per `guard-341` an empty `MIND_SID` means the hook did not fire and is an ERROR — so
the correct third answer is to REFUSE, never to recover by guessing. Do NOT "fix" this
by globbing `sessions/*/body-manifest.yaml` for an active worker: with more than one
Body on a box that re-introduces the same guess one layer down, and the operator can
supply the SID in one keystroke.

Bash: `if [ -z "$MIND_SID" ]; then echo "indeterminate"; elif [ -f "agents/<agent-name>/sessions/$MIND_SID/working-memory.yaml" ]; then echo "worker"; else echo "reducer-or-single"; fi`

IF output is "worker":

A worker `/stop` CLOSES this Body and LANDS this session (2026-10-07). It gives back
every claim the Body holds, hands this session's learning to the reducer and pushes its
commits to its own worker ref. Its last call rebinds this session to `target_mode`
(assistant, or reader with `--reader`) and writes `body-closing`, so the stop-hook runs
the ordinary genuine close: the working memory is staged and the Body becomes
`closed-pending-merge`. It does NOT park. A landed session is not a worker any more: its
Bash calls carry no worker env, its heartbeat stops, and `session-mode-get.sh` reports
`target_mode`, so the user keeps working in it. It never runs worker units again:
`/start` refuses a session whose fork file exists (W-pre / 0-pre2,
`EX_WORKER_FORK_PRESENT`), so `/start <agent-name>` in a FRESH terminal starts a new
Body. No step below writes an agent-wide session file.
# Rationale (WHY close instead of park, WHY land, and WHY this order): core/config/rationale/worker-stop-close.md

0. Skip what an earlier stop already did.
   Bash: `grep -Eqs "^mode:[[:space:]]*'?(reader|assistant)'?([[:space:]]|$)" "agents/<agent-name>/sessions/$MIND_SID/binding.yaml" && echo "landed" || { grep -Eqs "^body_state: '?(closed-pending-merge|merged|closed-stale|closed-graceful)'?[[:space:]]*$" "agents/<agent-name>/sessions/$MIND_SID/body-manifest.yaml" && echo "closed" || echo "open"; }`

   - `open`: continue with step 1.
   - `landed` or `closed`: this Body already closed. `closed` means it closed without
     landing: a stop from before the landing existed, or one whose landing write
     failed. Skip steps 1-7. They act on work the close already staged, and a relay
     written now would arrive after staging and be lost. Run steps 8-10. Step 9
     rebinds to this run's `target_mode`, so `/stop <agent-name> --reader` moves a
     landed session to reader mode.

1. Arm the SESSION-SCOPED stop signal so this turn can end (g-115-7309).
   FIRST: every later step is fire-and-forget, and a stop that cannot end its own
   turn is worse than one that skipped a step.
   Bash: `mkdir -p "agents/<agent-name>/sessions/$MIND_SID" && touch "agents/<agent-name>/sessions/$MIND_SID/stop-requested"`

   WHY THIS FILE AND NOT `session/stop-requested`: stop-hook valve #2
   (`worker-net-stop-requested`) reads the AGENT-WIDE file. This branch must never
   write that file, because the reducer's Phase -1.4 on another machine reads it.
   This file is read by the paired valve `worker-net-stop-requested-session`, is
   keyed to THIS SID, and never reaches the reducer (guard-4900 documents the trap;
   this step is its fix). worker-loop Phase -0-stop reads it too, so a wakeup that
   fires after the stop stands down instead of running a unit. This file does not
   close the Body; step 9 does.

2. Release everything this Body holds, then read back zero.
   Bash: `py -3 core/scripts/body-claims-release.py --agent <agent-name> --sid "$MIND_SID" || true`

   Read the one-line JSON verdict:
   - `nothing-held` or `released`: the read-back is clean.
   - `residue` (exit 1): it lists what is still held. Name it in step 10's output and
     do not loop on it.
   - `error` (exit 2): the claim query could not run. Report the claims as
     unverified, never as nothing held.
   No verdict fails the stop. This step runs BEFORE the learning pass, because any
   claim still held during step 3 keeps a goal locked away from the fleet for no
   benefit. Goal claims and team-state rows are released. Unit leases are left to
   expire; the script's docstring says why.

3. Hand this session's learning to the reducer.
   Skill: `encode-session` with args `--relay`

   Relay mode writes this Body's WM capture lanes and never a shared store (a worker
   that encodes is an Nth reducer). Step 9's close stages the WM, and the reducer
   replays the captures at its next generalize-down. A session with nothing to encode
   captures nothing, and that is the correct output. When the skill returns, continue
   with step 4: the stop is not finished.

4. Write this session's summary. This is the same call graceful-stop D6.5 makes, scoped
   to this SID.
   Bash: `bash core/scripts/session-summary-write.sh --sid "$MIND_SID" --agent <agent-name> --reason worker-stop || true`

5. Commit this session's churn, then push it to THIS BODY'S WORKER REF.
   Bash: `source core/scripts/_paths.sh && bash core/scripts/iteration-commit.sh --goal-id worker-stop --title "worker Body stop on this box" --outcome deep --type chore --repo "$PROJECT_ROOT" --session-sid "$MIND_SID" || true`
   Bash: `git status --porcelain`
   Bash: `bash core/scripts/iteration-push.sh --push-worker-ref || true`

   `source core/scripts/_paths.sh &&` IS LOAD-BEARING. `$PROJECT_ROOT` is unset in a
   bare Bash call, so `--repo` would pass empty, and the script would exit 1 naming
   all four flags you did pass (rb-9907). `--outcome deep` is load-bearing too:
   `routine` commits nothing.

   `--session-sid` limits the commit to this session's own edits outside
   `agents/<agent>/` (g-115-11148). encode-session's session-close commit uses the
   same scoping; relay mode skips that commit because this step makes it. Judge the
   result by `git status --porcelain`, never by the rc. An `ERROR: FOREIGN anchor
   refused` line is expected when the agent-wide `in_flight` row names the reducer's
   goal: it only skips the claim-time filter, and the `--session-sid` scoping still
   holds.

   `--push-worker-ref` pushes HEAD to `refs/workers/<agent>/<sid>` and stops there.
   It never pushes the shared branch, because merging into main is reducer-only
   (worker-loop Phase 3.8); the reducer merges the ref with `worker-ref-consume.sh`.
   This push runs before the rate limit applies, so it needs no zero flags. Do NOT
   add `--strict`: a network blip must not abort the stop.

6. Flush pending backend writes. Fire-and-forget.
   Bash: `bash core/scripts/owncloud-flush.sh || true`

   ⚠ **THIS DOES NOT PUSH THIS WORKER'S AGENT DIR, AND CANNOT** (g-115-9319). On a
   box that holds no live RUNNING claim for the agent, the flush prunes
   `agents/<agent>/` (guard-1579). A stopping worker is exactly that box. Read
   `pruned_agents=N` and the WARN line that names the agent. Do not read `pushed`: it
   counts other paths and proves nothing in either direction (guard-6254). The
   per-session state reaches the reducer through step 9 instead, because the genuine
   close stages the WM through its own writer.

7. Close this session's telemetry record, then remove the legacy SID binding.
   The worker got a WP1 `active` record at `/start` and never reaches the IDLE branch's
   WP2. Without this step its record stays `active` in the live-sessions query forever.
   guard-165: the SID and agent go through ENV, and the python source is single-quoted.
   Bash: `TSID="$MIND_SID" TAGENT="$MIND_AGENT" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_close; write_close(sid=os.environ["TSID"], agent=os.environ["TAGENT"], status="completed", ended_reason="user-stop")' >/dev/null 2>&1 || true`
   Bash: `rm -f ".active-agent-$MIND_SID"`

   Both run BEFORE step 9 on purpose. Until 2026-10-07 they ran after
   `body-manifest.py park`, and parked-body-gate.py denied them, so every stopped
   worker left its telemetry record open. Removing the legacy binding does not
   disarm step 9, because the stop-hook resolves this SID's agent from
   `sessions/<SID>/binding.yaml` first.

8. Load the rules of the mode this session lands in.
   Read: `core/config/modes/<target_mode>.md`

9. LAND this session, then CLOSE this Body. This must be the LAST call.
   Bash: `bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent <agent-name> --mode <target_mode> --started-by worker-stop >/dev/null || echo "[stop] could not land this session in <target_mode> mode; the Body still closes" >&2; touch "agents/<agent-name>/sessions/$MIND_SID/body-closing"`

   The binding write lands the session: from the next call on,
   `_session_binding.landed_mode_in` reads it as landed. It writes only this session's
   own binding. Never call `session-mode-set.sh` here: `agent-mode` belongs to the
   whole box. A failed write still closes the Body, and a later `/stop <agent-name>`
   lands the session through step 0.

   The stop-hook consumes `body-closing` when the turn ends:
   - `close-body-on-genuine` stages and pushes the WM and marks the Body
     `closed-pending-merge`.
   - `worker_close_in_flight_clear.py` then clears this Body's team-state rows a
     second time. That repeat is idempotent.

   It goes LAST because the WM snapshot is taken when the turn ends, so a WM write
   after it diverges after staging and is lost. It also goes last because it is the
   only step that cannot be undone. The landing shares its call, so no stop closes a
   Body without trying to land the session.

10. Output: `"Worker Body stopped and CLOSED on this box. Claims: <step-2 verdict, plus any residue it named>. The reducer was NOT signalled: its claim, canonical working memory and agent-wide session state are untouched. This Body's working memory, including the learning step 3 captured, is staged for the reducer's next merge, and its commits are on its worker ref for the reducer to merge into main. This session is now in <target_mode> mode and you can keep working in it, but it never runs worker units again: run /start <agent-name> in a fresh terminal for a new Body. To stop the whole agent, run /stop <agent-name> on the reducer box."`

    After a step-0 `landed` or `closed` run, output instead: `"This worker Body had already stopped and closed. This session is now in <target_mode> mode. For worker units, run /start <agent-name> in a fresh terminal."`

    If step 9 printed `[stop] could not land`, replace "This session is now in <target_mode> mode" with "This session could not be switched to <target_mode> mode; run /stop <agent-name> again to retry".

    This string must agree with steps 2, 5 and 9. The prose warning above and the
    string the operator actually reads are two separate artifacts, and fixing one does
    not fix the other (guard-4282). Until 2026-10-07 this string said the Body was
    PARKED and that "a later /start on THIS box resumes the SID". `/start` refuses that
    SID, so that claim was never true.

DONE. Do NOT continue to Step 1. Do NOT write `stop-target-mode`. Do NOT set the
AGENT-WIDE `session/stop-requested`. Do NOT call `session-mode-set.sh`. Do NOT chain
into the aspirations loop.

The word AGENT-WIDE is load-bearing and was added with step 1 (g-115-7309): this
clause used to read "Do NOT set `stop-requested`" unqualified, which now reads as a
prohibition on step 1 itself. The two files are different objects with opposite
blast radii — `session/stop-requested` is agent-wide and stops the REDUCER wherever
it runs; `sessions/<SID>/stop-requested` is scoped to this Body on this box. Only
the first is forbidden here.

IF output is "indeterminate": STOP. Do NOT continue to Step 1, and do NOT treat this
as the single-box case — that is the whole defect (g-115-9320). `MIND_SID` is empty,
which per `guard-341` means the PreToolUse inject hook did not fire; the role is
UNKNOWN, not "not a worker". Tell the operator verbatim:

> `/stop` cannot determine whether this session is a worker Body or the reducer:
> `MIND_SID` is empty, so the inject hook did not fire. Re-run with the SID set
> explicitly — `MIND_SID=<sid> MIND_AGENT=<agent> /stop <agent-name>` — where
> `<sid>` is the directory name under `agents/<agent-name>/sessions/` for this
> session. Proceeding blind would risk writing the agent-wide stop signal that
> stops the reducer on another machine.

Refusing is the safe direction here and a wrong guess is not: the operator recovers
in one keystroke, whereas the agent-wide branch stops a DIFFERENT Body on a DIFFERENT
box and nothing downstream detects it.

IF output is "reducer-or-single": continue to Step 1 unchanged. This is the ordinary
single-box case and behaves exactly as it did before this step existed. Note this now
means "evaluated, and this is not a worker" — it is no longer reachable by a failed
evaluation, which is what the `indeterminate` branch above took away from it.

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
       # Session-telemetry observer close (session-telemetry WP2, observer
       # variant, 2026-06-03): the observer got a WP1 `active` record at /start
       # (observer Step 0.5). It never reaches the IDLE branch's WP2 — Step 1
       # saw state=RUNNING (set by the runner), so /stop took THIS RUNNING
       # branch, not the IDLE branch. Without a close here the observer's record
       # would orphan as permanently-`active` and pollute the live-sessions
       # query. Placed FIRST in the observer branch so it fires for BOTH the
       # fresh and stale sub-paths below. Keyed on the OBSERVER's own $MIND_SID
       # (not the runner's running-session-id). status=completed,
       # ended_reason=user-stop. guard-165: SID/agent via ENV, python source
       # single-quoted. `py -3` (Bash-tool context). Fire-and-forget (|| true).
       Bash: `TSID="$MIND_SID" TAGENT="$MIND_AGENT" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_close; write_close(sid=os.environ["TSID"], agent=os.environ["TAGENT"], status="completed", ended_reason="user-stop")' >/dev/null 2>&1 || true`

       # Three-way heartbeat probe (pure mtime; g-357-51):
       #   fresh  → runner is alive; leave the signal for its next iteration.
       #   stale  → heartbeat file aged out; runner presumed crashed → route
       #            the user to `/start --recover`.
       #   absent → no heartbeat file on this box. NOT a crash verdict; the
       #            canonical gate decides (it needs positive death evidence).
       Bash: `bash core/scripts/heartbeat-stale.sh`

       IF output is "stale":
           Output: "Stop signal set, but runner session appears crashed (last
           heartbeat older than the staleness threshold in
           core/config/aspirations.yaml → runner_heartbeat.stale_minutes).
           The signal will not be picked up by the dead runner. Run
           `/start <agent-name> --recover` to force cleanup."
           DONE. Do not chain.

       IF output is "absent":
           Bash: `bash core/scripts/runner-dead-check.sh; echo "rdc_rc=$?"`
           IF rdc_rc == 0: same Output as the "stale" branch (crash confirmed
             by positive evidence). DONE. Do not chain.
           ELSE: Output: "Stop signal set. No heartbeat file exists on this box
             (absent, not stale) and the liveness gate does not read the runner
             as dead — the signal will be picked up at its next iteration."
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
3.5. **Finalize session telemetry** (session-telemetry WP2, 2026-06-03): write the
   durable close record for THIS (non-runner) session. The IDLE branch handles
   `/stop` of an assistant/reader/observer session — it never reaches the
   graceful-stop D6.6 close (that's the autonomous runner's path), so without
   this step assistant/reader sessions would have an open WP1 record that never
   closes. status=completed, ended_reason=user-stop, mode_at_end=<target_mode>.
   The record lives at world/telemetry/session-records/<agent-name>/$MIND_SID.json;
   the own-cloud sweep carries it to S3. Pure library module via `py -3 -c`
   (no .sh wrapper / no daemon dependency — works even if the daemon is dead).
   guard-165: SID/agent/mode pass through ENV VARS, python source single-quoted.
   $MIND_SID and $MIND_AGENT are present here — the hook auto-injects
   MIND_AGENT and the binding is not cleaned until Step 4 below. Fire-and-forget
   (|| true): telemetry must never block the stop.
   Bash: `TSID="$MIND_SID" TAGENT="$MIND_AGENT" TMODE="<target_mode>" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_close; write_close(sid=os.environ["TSID"], agent=os.environ["TAGENT"], status="completed", ended_reason="user-stop", mode_at_end=(os.environ.get("TMODE") or None))' >/dev/null 2>&1 || true`
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
- Sets NEITHER of the above on the **worker-Body path** (Step 0.6). On a worker box,
  `/stop` does the following in order:
  - arms its SESSION-SCOPED `sessions/<SID>/stop-requested` (g-115-7309), which fires
    the stop-hook's `worker-net-stop-requested-session` valve so the turn can end;
  - releases every goal claim and team-state row the SID holds;
  - runs `/encode-session --relay`;
  - commits, pushes to its worker ref, and closes its telemetry;
  - writes `body-closing` last, so the stop-hook's genuine close stages its WM and
    marks the Body `closed-pending-merge`.
  It never touches an agent-wide file. The reducer is not signalled and keeps running
  (g-306-125).
- Calls (worker path): `core/scripts/body-claims-release.py`, then `Skill: encode-session`
  with args `--relay`, and last `core/scripts/session-binding-write.sh`, which lands the
  session in `target_mode`.
- Calls (RUNNING branch, runner session only): `Skill: aspirations` with args `loop` as
  the final action, so Phase -1.4 runs in the same user turn and the graceful stop
  (D1–D7) completes before the turn ends. Observer sessions skip the chain and leave
  the work for the runner's Stop-hook re-entry path.
- Does NOT call: /aspirations-consolidate directly (that's reached via Phase -1.4 D4)
- Called by: User only. NEVER by Claude.
