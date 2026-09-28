# Rationale: The Vessel Recipe as an External Writer of `agent-state` and `persona-active`

Referenced from `.claude/rules/user-interaction.md` (Script-Level Restrictions) and
`core/config/conventions/stop-signal-writers.md` entry 7 (the 2026-09-27 exception,
moved there from `.claude/rules/stop-hook-compliance.md` rule 2 by g-353-151).
Implemented out-of-repo, in the vessel's provisioning recipe
(`ops/mind-sidecar/scripts/bootstrap.sh`, env-server repo). Plan of record: tree
node `mind-birth-contract`, item 8. Filed and measured by g-377-31; step (b) made
claim-aware by g-377-35 (env-server PR #614) and stop-aware by g-377-37 (PR #617).

## What it writes, and when

| Step | When | Writes | Condition |
|---|---|---|---|
| 3.5 (a) | ONCE per agent, at landing | `init-agent.sh`, a planted `agents/<id>/self.md`, then `session-state-set.sh IDLE` (UNINITIALIZED → IDLE) | `agents/<id>/.initialized` absent |
| 3.5 (b) | EVERY vessel boot, before the units start | `session-persona-set.sh false`; then, IF `agent-state` reads RUNNING, `session-state-set.sh IDLE` and `rm` of `loop-active`, `stop-requested`, `stop-loop` | the run judged DEAD by the liveness gate below (all three writes), and the file reading RUNNING (the last two) |

Both steps call the seed's own scripts, as the workspace user with
`MIND_AGENT=<id>` (the seed's G2 rename of `MIND_AGENT`). Neither writes the
files directly.

## Why it is authorized

Step (a) is the `/start` Phase A-0 twin. That transplant path is already
authorized in `user-interaction.md` as `/start` sub-path (4). A served Mind has
no human to answer `/start`'s first-boot ceremony, so the recipe lands the agent
the way a transplant does. Then the provisioning script can queue `/start <id>`
as the Mind's first word.

Step (b) plays `/stop`'s part for a vessel that died without running `/stop`:

- `persona-active=true` survives on the shared mount. The proxy's readiness
  predicate would then read the Mind as READY through the next boot's wake
  window, before this boot's `/start` has run.
- A stale RUNNING from a Run vessel turns every later `/start --mode assistant`
  into an observer session, which never flips persona.

## Why it needed a gate: the invariant did not hold

Step (b)'s own comment rested on "Never on a live one — nothing is running yet":
one vessel per mind, and nothing live at boot. Measured by g-377-31 on
2026-09-27 from env-server `origin/dev` 4804a666 (source only), the invariant
fails two ways.

1. **Same host.** `provision-env.sh` runs bootstrap unconditionally. Its own
   (b4) block calls the script "re-runnable against a LIVE box" and gates only
   its pointer rotation on `systemctl is-active mind-serve@${ENV_ID}`. A
   re-provision over a live Run vessel therefore demotes the live runner's
   `agent-state` and clears its `persona-active` and loop and stop signals. The
   resident's next loop entry reads IDLE and stops.
2. **Cross host.** One Body R8 (tree `one-body-pearl` §2.6) puts every body of
   a character in ONE mind directory on shared storage. It also allows more
   than one body at once (the framework's one-mind-two-bodies model). A second
   vessel that boots that mind heals the first one's live RUNNING state.

The framework makes the same RUNNING → IDLE transition only behind a liveness
gate: `recovery-gate.sh`'s six conditions and `/start`'s zombie gate. The recipe
gates on one condition, and that condition is a last-known status field, not a
live one.

**Live occurrence: NOT measured. Mechanism: proven from source.**

## The liveness gate (g-377-35, g-377-37)

Step (b) now writes nothing unless the run is judged DEAD by every check below.
Any one of them saying "maybe live" skips all three writes. A live run's
`stop-requested` can be the vessel sidecar's graceful stop in flight
(`vessel-sidecar-stop-caller.md`), so it is never cleared under one.

1. **Same host:** `mind-serve@${ENV_ID}` is not active. This is the (b4)
   `is-active` predicate, reused.
2. **Runner lease:** no FRESH lease, meaning `runner-claim.sh status` is not
   rc 0. The lease is read only when the workspace daemon already answers
   (`rt_is_up`). Boot must never spawn a daemon, and sourcing `_runtime.sh`
   spawns nothing.
   - **Measured, from source only:** a vessel workspace runs
     `STORAGE_BACKEND=local` (bootstrap.sh's `.env.local` block). It is
     unpacked from a seed tarball with no git remote, and the local claim store
     needs git plus a remote (`_local_claim_store`,
     `mind_api/src/endpoints/admin.py`). So the lease answers REFUSE on a
     vessel today.
   - REFUSE is logged as zero signal, never read as "absent".
   - The lease answers only whether the REDUCER holds the claim (guard-6475).
     A worker Body without a live reducer stands itself down by design, so the
     gate does not look for Bodies.
3. **Shared-mount liveness files:**
   - `heartbeat-stale.sh` does not print `fresh`. It reads
     `runner_heartbeat.stale_minutes` itself, so no threshold is copied here.
   - `execution-diary.jsonl` is at least 15 minutes old. This is the literal
     from `recovery-gate.sh` condition 2.7. Keep the two in step.

**Weaker than the recovery gate, and why that is acceptable.** Only a `fresh`
heartbeat counts as life here. An `absent` one does not block the heal, where
`recovery-gate.sh` refuses to act on `absent` alone. That is acceptable for two
reasons:
- a live framework loop ticks the heartbeat every iteration
  (`heartbeat-tick.sh`, loop Phase -0.5), and the diary is the independent-writer
  signal once the loop records goal phases. A read-only census of the shared
  mount (g-377-35, 2026-09-27, ~83 served-mind session dirs) found ~30 carrying
  `runner-heartbeat`, their diary mtimes within ~3 minutes wherever both files
  exist, and 4 with a heartbeat but no diary. So on a vessel the heartbeat is the
  load-bearing half of check 3. No mind was live at the census, so it shows what
  served loops leave behind, not a live run's cadence;
- the recipe demotes a run it cannot see, so it errs toward the framework's own
  crash windows rather than inventing tighter ones.

**Trade-off, named.** A vessel rebooted within those windows of its
predecessor's death keeps the stale RUNNING and `persona-active=true`, as before
the recipe existed. The framework's own recovery gate waits out the same
windows.

### Checks 4-6, for a RUNNING mind (g-377-37)

alpha's fresh-eyes review of #614 (`msg-20260927-140117-alpha-2970`) found two
`runner-dead-check.sh` conditions missing that a second body CAN read: 2.5
(a stop-hook BLOCK in the last 5 minutes) and 3 (`stop-requested`). A
usage-limited run keeps BLOCKing while its diary freezes for hours (rb-12120),
and a graceful stop in flight is exactly the `stop-requested` the heal deleted.
For a RUNNING mind the heal therefore also writes nothing while any of these
holds:

4. `heartbeat-stale.sh` did not answer `stale` or `absent` with rc 0. A broken
   probe is not a dead run (rb-762). The old `$(… || true) = "fresh"` counted
   it toward dead.
5. `runner-recent-block.sh <agent>` is not rc 1. This is the seed's own probe.
   It runs after the 15-minute diary check, so its own diary signal (5 minutes)
   cannot fire, and rc 0 means a BLOCK line for this agent: one under 5 minutes
   old, or one whose timestamp could not be parsed. The parse-failure branch
   fails toward alive with no age bound, so it needs `python3` on `FW_PATH`
   (rb-9404).
   - The log is `core/logs/stop-hook.log` in the shared workspace.
   - The seed manifest excludes `core/logs/`, so the every-boot `cp -a` refresh
     cannot clobber it.
   - zak-code runs the seed's `stop-hook.sh` as its Stop hook (`"Stop"` →
     `TURN_END`).
6. `stop-requested` is younger than `STOP_INFLIGHT_MIN` (15). The check is
   age-bounded because an interrupted stop strands the signal:
   - A served stop that overruns its grace is interrupted.
   - The env-server then terminates the instance inside its 420 s receipt wait
     (+30 s duration-cap backstop), so an older signal is a dead run's leftover.
   - Unbounded, the signal would latch the heal off, and `/start`'s zombie gate
     refuses on the same condition (rb-7672).
   - The bound is twice that 450 s ceiling. The harness pins it against both
     source constants.

A non-RUNNING mind keeps checks 1-3 for its persona reset. False is the value
`/stop` converges to, and `/start` re-arms it, so erring toward the reset is
the safe direction there.

**Not the script itself (guard-2601).** Calling `runner-dead-check.sh` from a
vessel boot widens it from same-box to cross-box. So each sub-probe was read at
the seed's source commit (Claude-Mind `add64b8`, the published
`mind-seed.tar.gz`, 2026-09-27):
- **No daemon.** None spawns one or sources `_runtime.sh`.
- **Conditions 1, 2, 2.5, 2.7 and 3 are scope-correct.** They read the shared
  mount.
- **Condition 4 is box-local.** `background-jobs.py has-pending` gates on
  `pid_alive`, which is `os.kill` on the CALLER's box.
- **Condition 5 is box-local, and it reads a false death.** Its `runner_proc`
  probe reads the shared-mount `<pid>:<starttime>` stamp but checks the
  caller's `/proc`. From a second body, a live runner reads `runner_proc_dead`,
  and in the script that satisfies condition 2 whenever the heartbeat is absent.

So the recipe composes the scope-correct probes instead, and the script's
header lists the recipe as a partial mirror.

The real seed probes were also run against a staged, vessel-shaped workspace:
- `heartbeat-stale.sh` printed one token with rc 0 for `absent`, `fresh` and
  `stale`.
- `runner-recent-block.sh` returned 0 only for a fresh BLOCK of this agent.
  `stop-hook.sh` writes three BLOCK line shapes: the main one, `gate=worker-net`
  and `gate=stop-unfinished`. The probe's `agent=<id>` match accepts either end
  of line or a following space, so it covers all three. The match was
  re-checked against the seed's writer lines in the g-377-37 review.
- It returned 1 for an old BLOCK, another agent's BLOCK or no log, and 2 with no
  argument.

**Residual, named.** A run asleep inside ONE turn can still read dead from a
second body. An in-turn provider retry that is not stopping ends no turn, so
it writes no BLOCK and no diary line. Its heartbeat still counts as life until
the last tick is `runner_heartbeat.stale_minutes` (60) old. The heal therefore
reads it dead only once that tick is 60 minutes old AND the diary is 15 minutes
old. After that, its only positive evidence is the runner process, which is
box-local. The same-box recovery gate sees it through condition 5; a second
body cannot.

**Tested.** `ops/mind-sidecar/tests/test-heal-claim-aware.sh` runs the real
block.
- **Now:** 17 cases, 68 checks. The harness against the pre-g-377-37
  bootstrap.sh fails 28 checks, exactly in the new live-signal cases and the
  bound pin.
- **Before (g-377-35):** 7 cases, 31 checks. The pre-g-377-35 script failed 19
  of those 31.
- **Mutation proof:** `mutation-proof-test.sh` sabotaged the merged file's
  RUNNING gate into `return 1`, which skips checks 4-6. The run went
  GREEN → RED → GREEN, and the restore was byte-verified. The bash harness
  writes no JUnit XML, so the tool could not attribute the kill. The same
  mutant, run through the harness's `BOOT_OVERRIDE`, fails 27 checks: every
  assertion of the six live-signal cases, plus the two that assert the BLOCK
  probe is consulted.

**Lanes.** A merge to env-server `dev` publishes the sidecar to the
`mind-sidecar-deployments/dev/` channel only. PROD vessels pull `latest/` from
`main`, and keep the old heal until the dev→main promotion.

## Counting

- **`session-state-set.sh` callers outside `/start` and `/stop`.** The recipe is
  the fourth, after the three counted in `stop-hook-compliance.md` rule 2. It is
  the second out-of-repo writer, after the vessel sidecar.
- **`stop-requested` WRITERS: unchanged at four.** Step (b) REMOVES
  `stop-requested` and `stop-loop`; the recipe never sets either.
- **`session-persona-set.sh false`:** `/stop`, and this recipe's every-boot
  reset.

## When to revisit

- When a vessel gains a claim store (an own-cloud backend, or a git remote on
  the workspace), the lease stops answering REFUSE and becomes a real signal.
  Re-measure check 2 then.
- When `recovery-gate.sh` changes its 15-minute diary literal, change the
  recipe's.
- When `runner-dead-check.sh` gains or changes a condition, decide it for the
  recipe too. Its header lists the recipe as a partial mirror.
- When the env-server's stop receipt wait or the provisioner's reserve rises,
  re-size `STOP_INFLIGHT_MIN`. The harness pin fails first.
- When the recipe's step numbers move, re-anchor both rule lines. They cite
  step numbers, not line numbers, for exactly this reason.
