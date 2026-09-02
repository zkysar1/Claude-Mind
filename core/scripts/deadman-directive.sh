#!/usr/bin/env bash
# deadman-directive.sh — the SINGLE source of the deadman terminal-pair directive.
#
# WHY THIS EXISTS (): the deadman net was written for the REDUCER only.
# A WORKER Body had none — `.claude/skills/worker-loop/SKILL.md` contained zero
# mentions of ScheduleWakeup/deadman/the sentinel — so a worker turn that ended on
# trailing TEXT instead of its terminal `Skill(worker-loop)` call was dead
# PERMANENTLY, with nothing to re-invoke it. The signature would be a dead LOOP
# inside a live PROCESS, which is why no process-liveness check catches it.
#
# THE GAP IS STRUCTURAL, NOT WITNESSED. It rests on the grep (worker-loop/SKILL.md
# had 0 mentions of ScheduleWakeup), and no worker text-death has ever been
# observed.  was originally filed citing a cc-08/foxtrot outage; that
# attribution was RETRACTED before work began — cc-08 had lost its Claude Code
# login. The tell is `CTX: 0%/0% [fresh]`: a text-death PRESERVES context, so a
# fresh context means a session restart, not a return-protocol violation. Do not
# use cc-08 as the regression scenario, and do not expect this net to prevent an
# auth-loss stall — ScheduleWakeup cannot fire a turn with no valid login.
#
# guard-2676 (no-transcription contract) governs how the fix had to be built: a
# capability added to the worker loop must be a scoped CALL to a shared
# component, never reducer steps transcribed into worker-loop/SKILL.md — a
# transcription drifts silently the next time the component evolves. No shared
# component existed (three emitters — iteration-close.sh, recurring-close.sh,
# iteration-close-reminder.py — each read `deadman-disabled` and spelled the pair
# out independently), so per that guardrail's own instruction ("if none exists,
# extract one first") this is it.
#
# THE WORKER PAIR IS NOT THE REDUCER PAIR. Copying it would be both FORBIDDEN and
# INERT, which is the whole reason this script branches on role rather than
# emitting one string:
#   - the only sentinel the runtime resolves, `<<autonomous-loop-dynamic>>`,
#     resolves to the AUTONOMOUS (reducer) loop instructions. A worker entering
#     those violates guard-517/guard-463 (NEVER Skill(aspirations) from a worker).
#   - and it would not even run: the aspirations loop requires agent-state
#     RUNNING, while a worker box is IDLE by design, so the resurrected turn
#     would refuse at Phase -1.5 rather than resume.
# The worker therefore arms a NATURAL-LANGUAGE prompt. That is explicitly
# sanctioned by .claude/rules/schedule-wakeup-correctness.md, and it clears the
# gate: `_swakeup_predicate.is_bad_slash_prefix` refuses only prompts that START
# with "/" and whose first token is not "/loop" — a prompt with no leading slash
# returns False (allowed). No runtime sentinel registration is needed.
#
# THE REDUCER ROLE WAS RETIRED, NOT FORGOTTEN (, 2026-08-06). It shipped
# implemented and with ZERO callers — iteration-close.sh, recurring-close.sh and
# iteration-close-reminder.py each still spell their own pair out — and the
# decision goal that inherited it chose RETIRE over WIRE on three measurements:
#
#   1. guard-2676 IS ALREADY SATISFIED. Read its action_hint: it governs how a
#      WORKER capability is built ("if none exists, extract one first"), and its
#      step 2 is about worker INERTNESS. The worker branch below IS the call site
#      that guardrail was about. One caller satisfies it; a second was never the
#      requirement.
#   2. WIRING WOULD HAVE BEEN A DOWNGRADE. iteration-close.sh:2732 carries the
#      rb-4345 single-shot-net lesson in full ("Emitting Skill(aspirations) ALONE
#      keeps THIS iteration alive but leaves the NEXT one unprotected"; "this call
#      is MANDATORY, do NOT omit it"). The retired reducer branch was one terse
#      sentence carrying neither — weaker than both the live emitters AND the
#      worker branch below. Replacing incident-earned text with a summary of it
#      is a regression wearing a refactor's clothes.
#   3. A MACHINE CONSUMER KEYS ON THE EMITTERS' LITERAL TEXT.
#      iteration-close-reminder.py carries TWO regexes anchored on the emitters'
#      `[iteration-close]` / `[recurring-close]` PREFIX:
#        - `_DEEP_RECURRING_RE` (L129) matches
#          `\[recurring-close\]\s+OUTCOME=deep\s*[—\-]+\s*NEXT ACTION REQUIRED`.
#          This sits INSIDE the very block a wiring would replace
#          (recurring-close.sh:1000-1010), and this script emits `[deadman]` and
#          none of those tokens — so wiring would have silently downgraded the
#          deep-recurring branch to the generic reminder. DECISIVE.
#        - `_ITERATION_COMPLETE_RE` (L163) gates whether ANY reminder fires at
#          all. A MINIMAL wiring (move only the pair text, leave the
#          `═══ ITERATION COMPLETE ═══` line at recurring-close.sh:993) would
#          NOT break it. Recorded anyway, and deliberately not overstated: it
#          shows the reminder keys on that prefix in two independent places, so
#          the blast radius of relocating emitter text into a `[deadman]`-prefixed
#          component is wider than the one regex that decided this.
#      Either way this is the green-while-inert class the whole deadman effort
#      exists to fix: nothing fails when the detector stops matching.
#
#      PREMISE CORRECTION — the goal that decided this () was filed
#      stating that "guard-1347 and guard-1162 pin the exact ITERATION COMPLETE
#      marker text those three emitters print". Read directly, they do not: both
#      are Layer-A BEHAVIOURAL backstops telling the LLM not to emit the deadman
#      pair on a false-positive hook reminder, and guard-1162's own status note
#      records that the hook now self-gates. The risk the filing pointed at is
#      REAL but lives in the two regexes above, not in those guardrails. Named
#      here because a decision recorded against the wrong artifact would send the
#      next reader to re-read two guardrails that say nothing about this.
#
# WHAT WOULD MAKE THE EXTRACTION WORTH REDOING (all three, not any one):
#   a. the three emitters converge on one prefix and one phrasing, or this script
#      grows a --prefix parameter;
#   b. BOTH iteration-close-reminder.py regexes move to a shared predicate module
#      the way _swakeup_predicate.py already does for the wakeup gate, so the
#      emitted text and its matchers cannot drift apart;
#   c. the reducer directive carries the FULL rb-4345 text, so wiring is not a
#      downgrade.
# Until then the duplication that remains is the sentinel string and the 600s
# delay — cheap, and visible in four places rather than hidden in one.
#
# Usage:
#   deadman-directive.sh --role worker
# Prints the directive on stdout. Exit 0 always (advisory; never blocks a loop).

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

ROLE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        # guard-1224: `shift 2` with $#==1 returns non-zero WITHOUT shifting, so a
        # flag passed last ($1 with no value) re-processes forever — a real hang,
        # not a usage error. This is the form _runtime.sh:84 documents.
        --role)    ROLE="${2:-}";        shift $(( $# >= 2 ? 2 : 1 )) ;;
        *) shift ;;
    esac
done

# `--role reducer` is refused with its OWN message rather than the generic usage
# line. A caller reaching for it is almost certainly mid-way through wiring one of
# the three emitters, and "usage: --role <worker>" would read as a typo and send
# them to re-add the branch. Say it was deliberate and where the reasoning lives.
if [[ "$ROLE" == "reducer" ]]; then
    echo "deadman-directive.sh: --role reducer was RETIRED (g-306-241) — it had zero callers and wiring it would have DOWNGRADED the live imperative and broken iteration-close-reminder.py's regex. The three reducer emitters own their own text deliberately. See this script's header for the three conditions that would make re-extraction worthwhile before you re-add it." >&2
    exit 2
fi

if [[ "$ROLE" != "worker" ]]; then
    echo "usage: deadman-directive.sh --role worker" >&2
    exit 2
fi

AGENT_DIR="$(agent_dir "${MIND_AGENT:-}")"

# Opt-out is AGENT-level for both roles: a box whose operator disabled the net
# disabled it for every Body on that box. Same path the three reducer emitters
# read, so the flag keeps exactly one meaning.
if [[ -n "$AGENT_DIR" && -f "$AGENT_DIR/session/deadman-disabled" ]]; then
    echo "[deadman] DISABLED (deadman-disabled present) — terminal action is Skill(worker-loop) alone."
    exit 0
fi

DELAY=600

# THE RESURRECTION PROMPT CHECKS CLOSURE FIRST, AND ON THE DURABLE RECORD
# (2026-08-09, cc-08 04:39->04:49). The original prompt branched on the
# body-closing SENTINEL's existence and re-armed BEFORE checking. Both halves
# were wrong for a closed Body: stop-hook Phase 2B CONSUMES the sentinel on a
# genuine close (body-manifest.py close_body_on_genuine unlinks it on every
# genuine-close branch), so a COMPLETED close and a close that never happened
# produce the identical observation — the branch read the first as "resume".
# And re-arm-first on a closed Body re-schedules the net every firing, a
# permanent ~600s zombie cycle. The durable closure record is
# body-manifest.yaml body_state; the sentinel remains only as the
# close-in-flight secondary. Re-arm-first (rb-4345) still governs the LIVE
# branch: one manifest read is the only exposure before the net is restored,
# against a certain infinite re-fire on the closed branch the old ordering
# produced. The stop-hook stands down on the same manifest read
# (gate=worker-net-body-closed), so the closed branch's text-free turn-end is
# ALLOWed rather than trapped.
#
# `parked` IS RESUMABLE AND MUST NOT BE READ AS CLOSED (). The predicate
# above is "anything other than active", written when the only non-active states
# were terminal — so adding `parked` to VALID_STATES silently recruited this
# prompt into wedging the exact Body the park exists to keep alive: it would
# decline to resume AND decline to re-arm, which is a permanent stop with no
# wakeup left, i.e. the durable close that  removed, reintroduced through
# the net. Reachable whenever a park turn dies before arming its own 3600s poll,
# leaving the previous unit's 600s net in the slot. So the branch is now an
# explicit CLOSED-SET test, not a not-active test: a state this prompt has never
# heard of resolves toward RESUMING (recoverable) rather than stopping dead.
# THE NET FIRES ON HEALTHY BODIES, AND THE PROMPT HAD NO BRANCH FOR IT
# (). The 600s net is re-armed at every terminal pair, so it fires
# whenever a SINGLE work unit runs longer than the delay — witnessed first-hand
# 2026-08-26 (alpha worker, cc-08, during : suite alone 40 min, unit
# ~55 min). "Never fires on a healthy loop" holds only for units SHORTER than
# the delay, and a deep-code unit routinely is not. So the firing is correct and
# the DEFECT IS THE INSTRUCTION: body_state was active, the prompt said
# Skill(worker-loop), and Phase 1 SELECT hands out the top eligible UNCLAIMED
# goal — skipping the in-progress goal this Body had CLAIMED and abandoning it
# with uncommitted edits and the claim still held. That goal then sits
# in-progress exactly like the 360-of-361 stranded claims  exists to
# fix. It was avoided ONLY because the worker noticed its own live claim and
# chose to finish; nothing in the prompt told it to look.
#
# The branch keys on claimed_by_sid, not claimed_by, per guard-1460: another
# SESSION of the same agent can legitimately hold a claim, and the SID is what
# separates "my abandoned unit" from "a peer instance's live work". The query
# shape is probe-verified, not assumed (aspirations-query.sh --goal-field
# claimed_by_sid <sid> --full returned exactly the one live claim). --goal-field
# is EXACT-match, which is correct for a SID and would be wrong for a prefix.
#
# SCOPE IS WORKER-ONLY IN THIS SCRIPT, verified at source: --role reducer was
# retired () with zero callers, and the three reducer emitters own
# their own text. Whether they carry the same gap is a separate question —
# probe before widening, do not assume symmetry.
cat <<EOF
[deadman] WORKER NET ARMED — your terminal response MUST be EXACTLY these TWO batched tool calls, in this order:
  (1) ScheduleWakeup(prompt='Your worker Body loop may have stopped without reaching its terminal Skill(worker-loop) call. FIRST read agents/<agent>/sessions/<your-SID>/body-manifest.yaml — the DURABLE closure record (the body-closing sentinel is CONSUMED by the stop-hook on a genuine close, so its absence proves nothing). IF body_state is one of closed-pending-merge / merged / closed-stale, OR the body-closing sentinel EXISTS (a close is in flight): this Body closed deliberately — do NOT resume and do NOT re-arm; end the turn with a Bash echo saying so (the stop-hook stands down on the closed manifest). IF body_state is parked: this Body is PARKED awaiting a reducer, which is RESUMABLE and is NOT a close — re-arm this same wakeup FIRST at delaySeconds=3600, then run py -3 core/scripts/body-manifest.py park-due --sid <your-SID> --agent <agent> (the park orbit backs off exponentially on consecutive parks, g-357-51): IF it exits 1 the full re-poll is not yet due — the re-arm you just made is the ONLY other call; end the turn with a Bash echo naming the seconds remaining (its last stdout line) and no Skill call. IF it exits 0 (or errors) resume by calling Skill(worker-loop) (its Phase 0.5 poll decides resume-or-re-park; do not decide that here). IF body_state is active, or is any value not named above: re-arm this same wakeup FIRST (natural-language prompt, delaySeconds=${DELAY}) so the net is restored before any work that could fail. THEN, BEFORE re-entering, check whether YOU STILL HOLD A CLAIM: run bash core/scripts/aspirations-query.sh --goal-field claimed_by_sid <your-SID> --full and read the rows. IF a row comes back with a non-terminal status (pending / in-progress / blocked), the net fired MID-UNIT on a perfectly healthy Body and that unit is YOURS and unfinished — RESUME THAT UNIT DIRECTLY and do NOT call Skill(worker-loop), whose Phase 1 SELECT offers only UNCLAIMED goals and would hand you a DIFFERENT one while your half-finished unit keeps its claim and its uncommitted edits on disk. Key on claimed_by_sid, NEVER on claimed_by: another SESSION of this same agent can hold a claim (guard-1460), and the SID is the only field that separates them. --goal-field matches EXACTLY, which is what you want here because a SID is a whole value, not a prefix. IF that query returns no rows you hold nothing — resume by calling Skill(worker-loop). NEVER call Skill(aspirations) — that is the reducer-only re-entry (guard-517/guard-463).', delaySeconds=${DELAY})
  (2) Skill(worker-loop) — the PRIMARY re-entry and the LAST call.
Both calls, every work unit. Skill ALONE keeps THIS unit alive but leaves the NEXT one unprotected against a silent text-death.
EOF
exit 0
