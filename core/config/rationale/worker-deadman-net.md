# Rationale: The worker's loop edge and deadman net

Referenced from `.claude/skills/worker-loop/SKILL.md` Phase 5 (CONTINUE) and its
`## Return Protocol` section. The passages below are the skill's history of the
loop edge and the worker deadman net, moved here verbatim on 2026-09-23
(g-115-8214) so the skill fits under the 65,536 B injection ceiling. The terminal
shapes and their prohibitions stayed in the skill; what moved is how each one
was learned.

## Phase 5 — why the loop re-invokes itself

Phase 5 — CONTINUE (the loop edge; added 2026-08-03 after the gap fired live).
v1 said "the driver may re-invoke this loop" — but no driver exists; the worker
one-shotted after its first goal (g-315-518 soak, DESKTOP-O91DLK2). The loop
re-invokes ITSELF: the terminal tool call of a completed work unit is
Skill(worker-loop), which re-enters at Phase -0 (re-verifying worker identity —
guard-517/guard-463 class: role-gated re-entry) and runs the Phase 0.5
reducer-liveness poll before any new claim. NEVER Skill(aspirations) — that is
the reducer's full-loop re-entry. Every CLOSE path (an EXPIRED park — the one
sentinel writer — or a user stop) still ends the turn with a Bash call after
its sentinel work, exactly as before — self-continuation never overrides a
close edge. A PARK (Phase 0.5 rc=1, or Phase 1 no eligible goal) is not a
close and takes the third shape: it ends on
`ScheduleWakeup(<park-resume prompt>, 3600, noop=false, reason="park re-poll")` and nothing after it.

## Why a worker has its own deadman net (g-306-239)

**A worker has its own deadman net (g-306-239, 2026-08-06).** Until then it had
none: this file contained zero mentions of ScheduleWakeup, and the terminal-pair
in `.claude/rules/return-protocol.md` was written for the REDUCER alone. So a
worker turn ending on trailing TEXT — the exact failure the rule above exists to
prevent, and one the Stop hook cannot reliably catch (rb-629/guard-454: Claude
Code does not fire Stop on a text-only turn-end) — was dead PERMANENTLY, with
nothing to re-invoke it. The signature would be **a dead LOOP inside a live
PROCESS**, which no process-liveness check sees.

**WITNESSED 2026-08-18 — this paragraph said the opposite until then, and the
correction is the point.** From g-306-239's filing (2026-08-06) until now the
gap rested on the grep alone (this file had 0 mentions of ScheduleWakeup) and
this line read "**no worker text-death has ever been observed**". That is now
FALSE. An alpha WORKER Body on cc-07 (SID d1aec55b, goal g-250-351) died on a
trailing-text turn-end and was resurrected by its own net — so the failure mode
is real, the net WORKS, and neither half was known before. The net's LATENCY is
the residual defect: armed at `delaySeconds=600`, it delivered **6h49m** later.
That latency finding is owned by **g-115-6629** — do not re-file it. What this
file owes a reader is only the corrected fact: cite THIS incident, not cc-08.

## Why cc-08 is not the regression scenario (the auth-loss retraction)

The cc-08 retraction below still stands and is still load-bearing — a witnessed
text-death does not retroactively make an auth-loss stall into one, and the
`CTX: 0%/0% [fresh]` tell is how you tell them apart. g-306-239 was originally
filed citing a cc-08/foxtrot outage as the measured instance; that attribution
was **retracted
before any work began** — cc-08 had lost its Claude Code LOGIN and sat at an idle
prompt because it could not authenticate. The tell is `CTX: 0%/0% [fresh]`: a
text-death PRESERVES context, so a fresh context means a session restart, not a
return-protocol violation. Re-login resumed the loop immediately, which a
text-death would not do.

So: do NOT use cc-08 as the regression scenario, and do NOT expect this net to
prevent an auth-loss stall — ScheduleWakeup cannot fire a turn when the CLI has
no valid login. The sibling DETECTION gap (worker stalls are invisible to the
watchdog, whose `--tick` has exactly one caller in `iteration-close.sh`, which
workers skip) covers all stall causes including auth loss.

(The watchdog half of that sibling gap has since closed for box-level probes:
worker-loop Phase -0.2 now runs the tick, and the reducer's WorkerStallProbe
watches a worker's heartbeat carrier. See `worker-cycle-preamble.md`.)

## Why the worker arms a natural-language prompt, verified

Do NOT reach for the reducer's `<<autonomous-loop-dynamic>>` sentinel — it is
both forbidden and inert here. It resolves to the AUTONOMOUS loop instructions
(guard-517/guard-463 forbid a worker entering those), and the aspirations loop
requires agent-state RUNNING while a worker box is IDLE by design, so a
resurrected turn would refuse at Phase -1.5 rather than resume. The worker arms a
NATURAL-LANGUAGE prompt instead — sanctioned by
`.claude/rules/schedule-wakeup-correctness.md`, and it clears
`schedule-wakeup-gate.py`, whose predicate refuses only prompts STARTING with
"/" whose first token is not "/loop" (verified: the emitted prompt returns
`is_bad_slash_prefix == False`, against a `/aspirations loop` control returning
`True`).

## Why the directive lives in a script, and why its reducer branch was retired (g-306-241)

The directive text is NOT written out here on purpose. guard-2676 (the
no-transcription contract) requires a scoped CALL to a shared component —
`core/scripts/deadman-directive.sh` — so the delay, the opt-out flag, and the
closure-check-then-arm ordering live in exactly one place for this loop and
cannot drift from it the way a transcription would.

That component serves the WORKER only. It shipped with a `--role reducer` branch
that had zero callers, and g-306-241 RETIRED it rather than wiring the three
reducer emitters to it: `iteration-close.sh` carries the rb-4345 single-shot-net
lesson in full where the shared branch carried one terse sentence, and
`iteration-close-reminder.py` keys its deep-recurring branch on
`recurring-close.sh`'s literal emitted text, which this component never produced
— so wiring would have downgraded the live imperative AND silently broken that
detector. `--role reducer` now refuses at rc=2 with an explanation. Read this as
the scope of the guarantee above, not as a gap: guard-2676 governs how a WORKER
capability is built, and this call site IS that capability. The three conditions
that would make a shared reducer directive worth extracting are recorded in the
script's own header.

## The three terminal-shape cells, as the skill's table carried them

The skill's table keeps each shape's instruction and its load-bearing reason.
The full cells, with their history, were:

**A work unit (Phase 4 done, no close condition).** **The deadman PAIR** — run `bash core/scripts/deadman-directive.sh --role worker` and emit exactly the two batched calls it prints: `ScheduleWakeup(<natural-language resurrection prompt>, 600, noop=false, reason="deadman resurrection net")` THEN `Skill(worker-loop)` as the LAST call. `Skill(worker-loop)` is still the primary re-entry (NEVER `Skill(aspirations)` — reducer-only, guard-517/guard-463; and never a bare Bash echo — the pre-2026-08-03 text said "hand control back to its driver", naming a driver that does not exist, and the Body silently one-shotted after its first goal, g-315-518 soak). The ScheduleWakeup is a NET behind it, not a substitute.

**A PARK — Phase 0.5 rc=1 (reducer gone, g-306-291) or Phase 1 no eligible goal (supply gone, g-353-73), park not expired.** **`ScheduleWakeup(<park-resume prompt>, 3600, noop=false, reason="park re-poll")` ALONE, as the last call, with NO `Skill(worker-loop)` after it.** This is the one terminal shape that is neither the pair nor a bare echo, and the asymmetry is deliberate in both directions. No Skill: re-entering now would re-run the poll that just said "no reducer" and spin. No 600s net either — **the platform keeps ONE pending wakeup (replace-slot), so the park poll IS the net**; arming both would leave whichever came second, and a 600s worker-net firing on a parked Body is the wedge the resurrection prompt now branches for explicitly. A park turn that forgets to arm is indistinguishable from a close and needs exactly the human `/start` this change exists to remove — and in THAT case only, the previous unit's 600s net is still in the slot (unreplaced, precisely because this turn never armed over it) and becomes a real backstop rather than a nuisance: `deadman-directive.sh` teaches it to read `parked` as RESUMABLE and re-arm at 3600 (pinned by `test_worker_prompt_treats_parked_as_resumable_not_closed`). On a park that DOES arm correctly there is no 600s net left to worry about — the 3600s poll replaced it. The stop-hook worker-net stands down on the parked manifest (`gate=worker-net-body-parked`), so this turn-end is ALLOWed rather than BLOCKed into a sentinel ceremony that would durably close the Body.

**A close path — an EXPIRED park (the Phase 1 sentinel just touched) or a user stop.** Bash echo stating the close reason. **Do NOT arm the net here** — the turn genuinely ends; stop-hook Phase 2B consumes the sentinel and stages the WM. A net armed by the PREVIOUS work unit is still pending and will fire ~600s later; that firing is benign because THREE layers read the DURABLE closure record, `sessions/<SID>/body-manifest.yaml` `body_state`: the resurrection prompt checks it FIRST and declines to resume (and does not re-arm) when it is in the CLOSED SET — `closed-pending-merge` / `merged` / `closed-stale`, NOT merely "not `active`", since `parked` is non-active and resumable; Phase -0's closure gate refuses a work unit on the same read; and the stop-hook worker-net stands down on it (`gate=worker-net-body-closed`). The `body-closing` SENTINEL cannot serve this purpose — close-body-on-genuine CONSUMES it on every genuine-close branch, so after a completed close its absence is indistinguishable from "no close ever happened". (The pre-2026-08-09 prompt read exactly that as "resume", and the worker-net BLOCKed every post-close turn-end into a second sentinel ceremony — measured cc-08 04:39→04:49.) This is the worker's equivalent of the reducer's safe landing (whose resurrected turn self-aborts at Phase -1.5 on `agent-state != RUNNING`).

## Cross-references

- `.claude/skills/worker-loop/SKILL.md` Phase 5 and `## Return Protocol`
- `core/scripts/deadman-directive.sh` — the worker directive (its header records when a shared reducer directive would be worth extracting)
- `.claude/rules/return-protocol.md`, `.claude/rules/schedule-wakeup-correctness.md`
- `deadman-switch.md` — the reducer's net
- g-115-6629 — the net's delivery latency
- rb-629 / guard-454 — Stop does not reliably fire on a text-only turn-end
