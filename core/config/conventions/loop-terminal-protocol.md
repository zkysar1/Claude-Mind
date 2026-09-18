# Loop Terminal Protocol — enforcement layers, verification wiring, and incident record

Mechanism reference behind `.claude/rules/return-protocol.md` (and the
platform-fact half of `.claude/rules/schedule-wakeup-correctness.md`). The rules
keep the imperatives: sub-skills terminate with Bash, the orchestrator terminates
with `Skill(aspirations)` after `═══ ITERATION COMPLETE ═══`, the deadman
terminal PAIR, and re-arm FIRST on a resurrection or an autocompact resume. This
file carries the incident that produced the two-case split, how the contract is
verified and enforced at runtime, and the layered defense against the
Explanatory-output-style collision. The deadman design and its measured incident
traces (rb-4345 / g-115-2771 / g-115-5834) are in
`core/config/rationale/deadman-switch.md` and are NOT duplicated here. Loaded on
demand (`load-conventions.sh loop-terminal-protocol`); moved out of the rule on
2026-08-17 under g-115-6581 (context-window diet).

## 1. The Trap — the incident behind the two-case split (2026-04-23)

The failure mode observed on 2026-04-23 (alpha session 58) was:

1. Sub-skill /reflect finished its work and emitted a terminal Bash echo (correct — sub-skill case).
2. Orchestrator resumed, ran the four iteration-close phases successfully.
3. Orchestrator then produced a friendly text summary of what happened.
4. Orchestrator emitted a terminal Bash echo to "comply" with this rule.
5. **Turn ended.** No `Skill(aspirations)` call was made.
6. The Stop hook did not log a BLOCK for the runner SID — investigation
   (2026-04-24) against 1151 BLOCK entries for `agent=alpha` found zero
   entries for the active runner SID, indicating Claude Code does not fire
   the Stop event reliably when a turn ends with a text message and no
   pending tool call. The Stop hook therefore cannot be relied on as the
   sole enforcement layer for this contract.
7. Loop died silently — `agent-state` stayed `RUNNING`, `running-session-id`
   stayed set, but no new turn fired until the user sent a message.

Root cause: the previous version of this rule said only "terminate with Bash, not
text" — which made the Bash echo feel like the correct terminal action even at
iteration close. It was not. Bash is the sub-skill terminal; `Skill(aspirations)`
is the orchestrator terminal. Conflating them kills the loop.

## 2. Verification and runtime enforcement

`/verify-learning` enforces (1) with a dynamic grep: it iterates every
`.claude/skills/*/SKILL.md`, skips the exempt list above, and fails on any
remaining file that lacks `## Return Protocol`. New forged skills inherit the
requirement via the `.claude/skills/forge-skill` template (Step 3). The exempt
list is the single source of truth for (1) — edit it in
`.claude/skills/verify-learning/SKILL.md` if a genuinely new user-only skill
is added.

The orchestrator-vs-sub-skill split in rules (2) and (3) is enforced at
runtime by `core/scripts/iteration-close.sh` and `core/scripts/recurring-close.sh`,
which now print an explicit imperative as the terminal line of their stdout:
```
[iteration-close] ═══ ITERATION COMPLETE ═══
[iteration-close] NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your VERY NEXT tool call.
```
Emitted from the final line of `do_productivity_check()` in iteration-close.sh
and the final line of recurring-close.sh. The Stop hook
(`core/scripts/stop-hook.sh`) reads `iteration-checkpoint.json` at BLOCK time
and restates the phase-specific required next action in its decision payload.

## 3. Layered defense when combined with the Explanatory output style

The Explanatory output style mandates trailing `✶ Insight ─────` blocks
"before AND after writing code." That mandate collides with rule (2) above:
the trailing insight makes prose, not the tool, the last content of the
message, and the turn ends. Four screenshot-evidenced silent loop deaths on
2026-04-29 (rb-629, guard-454) all matched this shape.

The combination has four enforcement layers — the same pattern documented
under `capability-routing-enforcement` for honor-system rules:

| Layer | Defense | Artifact |
|-------|---------|----------|
| **A** — tactical (LLM remembers) | This rule + the tree node below | `.claude/rules/return-protocol.md`, `world/knowledge/tree/system/system-constraints-loop/return-protocol-vs-explanatory-style.md` |
| **B** — automated gate | `/start` refuses mode=autonomous + Explanatory style at session entry | `world/scripts/output-style-mode-guard.sh` (Layer-B gate, exit 2 refused / 3 override+audit) |
| **C** — preventive observability | Stop hook flags trailing-text patterns post-hoc with specific diagnostic | `world/scripts/trailing-text-detector.py` (4 sub-patterns: insight_block HIGH, phase_summary HIGH, next_step_narration MEDIUM, trailing_prose LOW) |
| **D** — recurring audit | 24h transcript scan via the detector, files Investigate on hit | `g-115-315` (recurring under asp-115, interval 24h) |

Wiring goals: `g-115-316` (HIGH, Idea — Layer-B into /start Phase 0.6),
`g-115-317` (MEDIUM, Idea — Layer-C into stop-hook.sh).

See `world/knowledge/tree/system/system-constraints-loop/return-protocol-vs-explanatory-style.md`
for the full incident shape, decision procedure, and diagnostic signature.

## 4. ScheduleWakeup platform facts and the deadman's fail-safe property (moved from `schedule-wakeup-correctness.md`)

Verified platform facts the design rests on (canary, 2026-06-21, dev
session):

- A tool batched AFTER ScheduleWakeup in the same response DOES execute
  (so `Skill(aspirations)` after the arm runs — pending live re-validation
  that the Skill specifically *re-enters* the loop, "Q5").
- ScheduleWakeup is turn-terminal (ends the turn after the batch) — which
  is why the arm must be at the TERMINAL, paired with Skill, never early
  (an early arm would truncate the iteration).
- A scheduled wakeup DOES fire after a text-only turn-end and re-invokes
  the agent (the resurrection primitive — proven end-to-end).

The gate (`schedule-wakeup-gate.py`) already passes the `<<autonomous-loop-dynamic>>`
sentinel (`is_bad_slash_prefix` returns False), so the deadman call is
approved unconditionally. guard-511 carries the matching carve-out.

Fail-safe property: if the live Q5 re-validation shows `Skill(aspirations)`
does NOT re-enter after ScheduleWakeup, the worst case is a SLOW loop
(`delaySeconds`/iteration, driven by the wakeup) — still alive and still
self-arming — NOT a dead loop. The change cannot make survival worse than
the status quo.

### 4.1 One harness contract — the imperative is spelled in Claude Code's tool names, and the vessel implements that contract (2026-09-18)

The framework targets exactly ONE harness contract: Claude Code's. Every terminal
imperative (the stop hook's BLOCK reason, `═══ ITERATION COMPLETE ═══`, the
recurring-close proceed text, the PostToolUse reminder) says `Skill(aspirations)
with args='loop'` and `ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>",
delaySeconds=600)` — the same bytes on every box. A vessel that hosts a Mind
implements that contract rather than being detected: Zak-Code names the tool on
the hook wire the way Claude Code does (ADR-0071), delivers a skill a turn-end
veto names and parses both spellings (ADR-0187), and exposes Claude Code's tool
names to the model (ADR-0190). The framework never branches on harness identity
for BEHAVIOUR; the `CLAUDECODE` / `ZAKCODE_SESSION` markers are provenance
labels (judge provenance, the confidence ledger, skill evaluation) and nothing
more.

Superseded: on 2026-09-17 the imperatives were briefly spelled per harness
through a vocabulary lookup (`_harness_caps.skill_call` and friends, keyed on
the env markers). Reverted 2026-09-18 on the operator's ruling — a framework
that knows which vessel it runs on yields a promoted copy that behaves
differently per box and is wrong by construction on any harness it has not
named. The measured failure it addressed (a small model answering the
imperative in prose) is closed on the vessel side instead, where it belongs.

Extensions the vessel understands may ride in a framework payload only when
they are ADDITIVE and never load-bearing: the reducer BLOCK carries
`"wakeup": {"prompt": "<<autonomous-loop-dynamic>>", "delay_seconds": 600}`
unconditionally (Zak-Code ADR-0102; precedent g-353-74's parked/closed lines),
because Claude Code drops keys it does not define without error.

## 5. Origin of the slash-prefix rule (2026-05-18)

Discovered 2026-05-18 from zeta session f1f3066e: four consecutive
ScheduleWakeup calls with `prompt: "/aspirations loop"` over four hours
(08:54 / 09:47 / 10:41 / 11:07 UTC), each followed ~2 min later by the
"can only be invoked by Claude" rejection. Layer D audit of 18 transcripts
showed zeta was the only agent doing this — alpha, delta, and echo all
used `<<autonomous-loop-dynamic>>` or natural-language correctly. The
pattern is easy to drift into once it survives in context, so the gate
protects against future occurrences in any agent.
