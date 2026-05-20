# Blocked-Sleep Recovery — Extracted Phase -0.5e

Loaded from `.claude/skills/aspirations/SKILL.md` (Phase -0.5e) on demand.
Common iterations (no blocked-sleep timer) never load this file — the SKILL.md
stub runs the cheap sentinel `idle-tick.sh` inline and only loads the digest
when the recovery body is actually needed.

Two distinct load points in the SKILL.md stub, with different preconditions:

- **Branch A entry** — `idle-tick.sh` emitted an `=== IDLE TICK ===` directive.
  `idle_tick_stdout` holds the directive; `wm-read.sh blocked_sleep_until`
  has NOT been called (the stub skipped it — the directive is sufficient).
- **Branch B entry** — `idle-tick.sh` returned empty AND the stub then ran
  `wm-read.sh blocked_sleep_until`, which returned a non-null ISO timestamp
  (checkpoint wake, residual, expired, or corrupt). `blocked_until_value`
  holds that timestamp; `idle_tick_stdout` is empty.

**DO NOT re-invoke `idle-tick.sh` or `wm-read.sh blocked_sleep_until` inside
this digest.** `idle-tick.sh` recomputes `REMAINING` on every call (a second
invocation would produce a different directive — see `idle-tick.sh` L56-82);
`wm-read.sh` mutates `accessed_at`, distorting WM aging. Each branch uses
only the input named for it above.

---

## Branch A — idle-tick emitted a directive

The directive in `idle_tick_stdout` names the exact Bash command to run.
Issue it verbatim. CRITICAL: always `interruptible-sleep.sh` (not plain
`sleep`) — the 1s stop-signal check is what lets `/stop` respond within
seconds instead of waiting up to 30min for the sleep to exit.

`idle-tick.sh` caps each sleep at 600s (`LIGHT_PRECHECK_CAP`). The directive
includes `CHECKPOINT_SLEEP=` (0=final, 1=midway) so Branch B on the next
entry knows whether to run a light precheck before re-entering backoff.
Persist CHECKPOINT_SLEEP to WM so the flag survives the turn boundary.

```
Parse CHECKPOINT_SLEEP from idle_tick_stdout (line containing "CHECKPOINT_SLEEP=")
echo '"{checkpoint_sleep_value}"' | Bash: wm-set.sh last_checkpoint_sleep
Bash: core/scripts/interruptible-sleep.sh {sleep_duration_from_directive} (run_in_background=true)
RETURN
```

Turn ends. Harness exit-notification re-enters `/aspirations loop`. On
re-entry, Phase -0.5e runs again: if the timer expired, Branch B clears it
and the skill continues normally; if autocompact fired mid-sleep, idle-tick
re-emits a fresh directive for the new remaining.

---

## Branch B — post-sleep residual / checkpoint-wake / expired-timer

Possible causes at entry (stop-signal and no-timer cases are already handled
by the caller, so neither reaches this branch):
- (c) residual ≤60s — let idle-tick cap decided this, sleep inline;
- (d) sleep just completed — naturally, via wake-on-signal (exit 2), or by
  hitting the 600s cap (CHECKPOINT_SLEEP=1 on the prior cycle).

```
Bash: wm-read.sh last_checkpoint_sleep
checkpoint_flag = parsed (defaults to "0" if unset or null)

IF valid ISO timestamp in blocked_until_value:
    remaining_seconds = max(0, seconds_until(parse(blocked_until_value)))

    # Case (d-midway): previous cycle was a 600s checkpoint and the timer
    # is still in the future. Run a LIGHT precheck pass (below) then
    # re-enter backoff for the residual. This is the session-47 fix for
    # "backoff silently disables framework health checks."
    IF checkpoint_flag == "1" AND remaining_seconds > 60:
        Output: "▸ Light-precheck checkpoint — {remaining_seconds}s still remaining"
        # ─ LIGHT PRECHECK PASS ─
        # CRITICAL: every Bash call below MUST be a real script in
        # core/scripts/. Do NOT insert `aspirations-precheck.sh` —
        # aspirations-precheck is a Skill, not a script. Loading the
        # full precheck skill here would defeat the "cheap idle path"
        # design. If completion-runner coverage is needed during
        # backoff, extract a granular script first; don't reference
        # a non-existent file.
        Bash: session-signal-exists.sh stop-requested
        IF exit 0: continue to full skill body (Phase -1.4 will handle it)
        # Capability recheck sweep — aged [user]-routed blockers whose
        # failure_reason now matches an agent-provisionable skill get
        # auto-cleared, with an Investigate goal filed for the lapse.
        Bash: blocker-recheck.sh --max-age-hours {config.blocker_age_hours} --apply
        # If any of the above cleared the blocker, known_blockers will
        # show resolution != null. Re-evaluate: do we still need to sleep?
        Bash: wm-read.sh known_blockers --json
        # g-251-08: cooldown_active distinguishes a productivity-cooldown
        # wake (productivity-stop-gate.sh _write_blocked_sleep_until) from a
        # B7-backoff wake. Both reuse blocked_sleep_until, but only B7-backoff
        # represents an unresolved blocker — productivity cooldown is paced
        # rest after a low-score iteration, NOT a blocker. Without this read,
        # the proactive_escalation block below fires a "still blocked" email
        # whenever any pre-existing known_blocker happens to be present at
        # the cooldown wake (rare but observed). Cleared on every blocked_sleep_until=null path.
        Bash: wm-read.sh cooldown_active --json
        IF all blockers have resolution != null:
            Output: "▸ Light-precheck cleared all blockers — resuming normal loop"
            echo 'null' | Bash: wm-set.sh blocked_sleep_until
            echo 'null' | Bash: wm-set.sh last_checkpoint_sleep
            echo 'null' | Bash: wm-set.sh cooldown_active
            # Fall through to Phase -0.5d / iteration body
        ELSE:
            # ─ PROACTIVE HEARTBEAT (C2: relocated from B7.1) ─
            # Notify user on every wake if blocker is still unresolved AND
            # no notification has gone out recently. Cooldown via
            # proactive_escalation_log WM slot (same as original B7.1).
            # Previously B7.1 only fired once per session (on the first
            # all-blocked fall-through); now it fires on every wake during
            # backoff, subject to blocker_age_hours cooldown.
            #
            # g-251-08 SUPPRESSION GATE: skip the notification when the wake
            # was triggered by productivity-cooldown rather than B7-backoff.
            # The cooldown is paced rest, not blocker waiting — surfacing the
            # pre-existing blockers at every cooldown wake spams the user
            # with messages they've already seen during the actual B7 path.
            # Telemetry-only: log the suppressed firing so audit can spot
            # incorrect suppressions if the flag ever drifts.
            IF cooldown_active is true:
                Output: "▸ Cooldown wake during productivity-paced rest — suppressing 'still blocked' notification (g-251-08)"
                # Continue to "still blocked" backoff fall-through, but skip
                # the notification block entirely.
            ELIF config.proactive_escalation.b7_notify:
                Bash: wm-read.sh proactive_escalation_log --json
                last_b7 = entry where blocker_id == "_all_blocked"
                IF last_b7 is null OR hours_since(last_b7.sent_at) >= config.proactive_escalation.blocker_age_hours:
                    Bash: wm-read.sh known_blockers --json
                    blocker_summary = build from unresolved blockers (id + reason + age + unblocking_goal)
                    Notify the user about the sustained blocker.
                    (Check world/forged-skills.yaml for a skill whose triggers match "notify the user"
                    and invoke it with a blocker-category payload:
                      subject: "Still blocked — {len(unresolved)} active blocker(s), agent waiting"
                      message: "During light-precheck on backoff wake:\n{blocker_summary}\n\nHighest-leverage user action: {most_impactful}"
                    If no matching skill is registered, fall back to a participants: [agent, user] goal via
                    aspirations-add-goal.sh. Never block on notification failure.)
                    echo '{"blocker_id":"_all_blocked","sent_at":"{now}"}' | Bash: wm-append.sh proactive_escalation_log
            # Still blocked. Re-enter backoff for the residual remaining.
            # Reset checkpoint flag (idle-tick will re-set it if needed).
            echo 'null' | Bash: wm-set.sh last_checkpoint_sleep
            Output: "▸ Blocker persists — re-entering backoff for residual {remaining_seconds}s"
            Bash: core/scripts/interruptible-sleep.sh {min(remaining_seconds, 600)} (run_in_background=true)
            RETURN

    # Case (c): true residual (≤60s per idle-tick gate).
    ELIF remaining_seconds > 0:
        Output: "▸ Blocked-sleep residual {remaining_seconds}s — sleeping inline"
        Bash: interruptible-sleep.sh {remaining_seconds}
        exit_code = $?
        IF exit_code == 2:
            Output: "▸ Wake-on-signal (blocker-cleared or pq-resolved) — breaking backoff"
            echo 'null' | Bash: wm-set.sh blocked_sleep_until
            echo 'null' | Bash: wm-set.sh last_checkpoint_sleep
            echo 'null' | Bash: wm-set.sh cooldown_active
            # Fall through to full iteration body — state changed.

    # Case (d-final): timer expired.
    ELSE:
        Output: "▸ Blocked-sleep timer expired — proceeding"
ELSE:
    Output: "▸ Blocked-sleep slot corrupt ('{blocked_until_value}') — clearing"

# Unconditional cleanup for every path that falls through (RETURN paths
# above skip this, which is correct — they want to re-enter backoff).
# g-251-08: also clear cooldown_active so the next iteration's cooldown
# sees a clean flag (the productivity-stop-gate writer sets it to true
# atomically with blocked_sleep_until on each cooldown).
echo 'null' | Bash: wm-set.sh blocked_sleep_until
echo 'null' | Bash: wm-set.sh last_checkpoint_sleep
echo 'null' | Bash: wm-set.sh cooldown_active
```
