#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-claim — daemon-aware wrapper (PR 9b).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Positional goal_id + optional agent_name + --cross-lane / --override-lane-pin
#   3. POST /v1/aspirations/claim?id=<goal_id>&agent=<name>
#        [&cross_lane=<reason>][&override_lane_pin=<reason>][&sid=..][&source=..]
#   4. On 200, print goal JSON to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Normalize --goal/--goal-id flag aliases → positional goal id (rewrites $@).
# SSOT for the dual-accept goal-id contract; verify-learning enforces that this
# wrapper sources the normalizer (12-wrapper coverage grep). Restored 2026-05-29
# — dropped by a prior daemon cutover, which silently broke dual-accept and the
# verify-learning normalizer-coverage check.
GOAL_NORMALIZE_TARGET=positional source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"

# --- Parse args -----------------------------------------------------------
GOAL_ID=""
AGENT=""
CROSS_LANE=""
DEVIATION=""
OVERRIDE_LANE_PIN=""
VERDICT_FILE=""
# (PASSTHROUGH array removed : it was written in three places and read
# in none — a vestigial leftover of the pre-daemon cutover. Its only live effect
# was the `-*` branch's shift-the-flag-only behavior, which is the defect fixed
# below, so it went out with that line rather than surviving as a write-only
# array that reads like it still forwards something.)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cross-lane)
            CROSS_LANE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-lane-pin)
            # Lane-pin gate escape hatch (). FORWARDED to the daemon,
            # which is where the gate runs — a wrapper-side gate is bypassable by
            # any direct endpoint POST (guard-742/guard-554: the live runtime code
            # behind a daemon-routed wrapper is the daemon's implementation).
            # The VALUE is the audited justification, not a boolean: it lands in
            # world/override-bypass-ledger.jsonl under gate 'lane-pin-gate'.
            OVERRIDE_LANE_PIN="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --source)
            # FORWARDED as of . Accepted since  for convention
            # symmetry (aspirations-select Chaining, aspirations-execute Inputs,
            # loop digest Phase 4 all tell callers to pass `--source
            # {world|agent}` to every downstream aspirations-*.sh) but then
            # DISCARDED, on the stated premise that "claim has no per-source
            # semantics — the daemon endpoint derives source from the goal-id."
            # That premise was never true: claim() hardcoded
            # `_resolve_paths(ctx, "world")` and refused agent-queue goals 400
            # rather than deriving anything. Accepting a flag and dropping it is
            # what let the loop digest's note stand — "the script's arg parser
            # does [support both sources], the endpoint does not."
            #
            # Both halves now do. An empty/absent value still sends nothing, so
            # the endpoint's own "world" default keeps every existing caller
            # byte-identical.
            SOURCE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --deviation)
            # Scorer Sovereignty Layer B (): the sanctioned-deviation
            # code the scorer-verdict gate requires when claiming a goal that is
            # NOT the scorer's fresh top pick. Consumed here (NOT passed through)
            # — the daemon claim endpoint has no per-deviation semantics; the
            # gate below is the only consumer.
            DEVIATION="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --verdict-file)
            # Scorer Sovereignty test seam (). The gate already owns
            # this flag, annotated `(tests)`; this branch is the missing link
            # between it and the only callers that need it. WITHOUT it a test
            # driving this wrapper in a tmp project_root still gets the gate's
            # default resolution -- `agent_state_dir(<agent>) / scorer-verdict.json`
            # through `_paths`, i.e. rooted at the REAL project root -- so the
            # gate reads the LIVE agent's verdict and refuses the claim against a
            # top pick that exists only in the live queue.
            #
            # The env-override route is closed BY DESIGN and was measured, not
            # assumed: `_paths.agent_dir()` explicitly does NOT honour
            # MIND_AGENT_DIR (guard-2985, documented in _paths.py), and
            # _paths.sh recomputes AGENT_DIR from MIND_AGENT on every source
            # (guard-2446). Name-based resolution leaves the path argument as
            # the only lever.
            #
            # Substituting a throwaway AGENT name so the gate finds no verdict
            # and fails open was considered and REJECTED under guard-2530: that
            # is a proxy whose equivalence ("a nonexistent agent has no verdict")
            # nobody restates, it goes stale silently the day the gate branches
            # on identity, and no live caller drives a synthetic agent through
            # this wrapper (guard-920). An explicit, readable path argument is
            # the visible dependency; take the visible one.
            VERDICT_FILE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        -*)
            # REFUSE unknown flags (). This branch used to do
            # `PASSTHROUGH+=("$1"); shift` — shifting the FLAG ONLY. The
            # orphaned VALUE then fell through to the positional branch below
            # and became agent_name. So a single typo (`--deviation-code X`
            # for `--deviation X`) silently minted a phantom fleet agent named
            # X: a team-state agent_status row in a fleet that has no such
            # agent, carrying an in_flight that release could never clear
            # (release looks up the REAL agent's row) and that then blocked
            # goal filing via goal-duplication-gate's partner_in_flight check.
            #
            # The --source branch above exists for exactly this failure (read
            # its comment: "phantom claimed_by=world row"). Enumerating one
            # KNOWN flag cannot cover the UNKNOWN flags that actually cause it,
            # which is why the hole re-opened. Refusing closes it.
            #
            # Safe to be strict: the accepted set is fully enumerated and small.
            # Verified 2026-07-28 across core/scripts, .claude/skills,
            # core/config, and world/scripts — no caller passes any flag but
            # --source and --deviation.
            echo "Error: unrecognized flag '$1'." >&2
            echo "  Accepted: --deviation <code> | --cross-lane <reason> | --source <world|agent> | --goal[-id] <id> | --override-lane-pin <reason> | --verdict-file <path>" >&2
            echo "  Usage: aspirations-claim.sh <goal-id> [<agent-name>] [--deviation <code>]" >&2
            exit 1;;
        *)
            if [ -z "$GOAL_ID" ]; then
                GOAL_ID="$1"
            elif [ -z "$AGENT" ]; then
                # Agent names are fleet identifiers, never prose. A value that
                # fails this pattern is almost always a justification sentence
                # mis-bound after `--deviation <code>` (/guard-4418:
                # the sentence reached the claim query as agent=<prose>, curl
                # exited 3 URL-malformed, and the retry loop misread it as
                # daemon trouble — six timed-out claims in one morning).
                # NOTE: --deviation takes ONLY the enum code. Justifications
                # go in the goal outcome / override ledger, not on the claim.
                if [[ ! "$1" =~ ^[a-z0-9_-]+$ ]]; then
                    echo "Error: agent-name positional looks like prose, refusing: '$1'" >&2
                    echo "  Agent names match ^[a-z0-9_-]+\$. If this text is a justification:" >&2
                    echo "  --deviation takes ONLY the enum code (no justification argument);" >&2
                    echo "  --cross-lane and --override-lane-pin are the flags that take a reason string." >&2
                    echo "  Usage: aspirations-claim.sh <goal-id> [<agent-name>] [--deviation <code>]" >&2
                    exit 1
                fi
                AGENT="$1"
            else
                # A third positional was previously dropped SILENTLY — the
                # unquoted-justification variant (`--deviation <code> multi
                # word reason`) would bind "multi" as a phantom agent and
                # swallow the rest. Refuse loudly instead.
                echo "Error: unexpected extra positional argument: '$1'" >&2
                echo "  goal-id and agent-name are already set (goal='$GOAL_ID', agent='$AGENT')." >&2
                echo "  Usage: aspirations-claim.sh <goal-id> [<agent-name>] [--deviation <code>]" >&2
                exit 1
            fi
            shift;;
    esac
done

# Default agent from env
if [ -z "$AGENT" ] && [ -n "${MIND_AGENT:-}" ]; then
    AGENT="$MIND_AGENT"
fi

if [ -z "$GOAL_ID" ]; then
    echo "Error: goal_id is required." >&2
    exit 1
fi
if [ -z "$AGENT" ]; then
    echo "Error: agent_name is required (positional or via MIND_AGENT)." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# --- Scorer Sovereignty Layer B gate () -------------------------
# BEFORE the daemon claim POST, refuse an UNSANCTIONED divergence from the
# scorer's fresh top pick. The gate reads the per-agent scorer-verdict sidecar
# (written by goal-selector.write_scorer_verdict); claiming a goal that is NOT
# top_goal_id requires --deviation <code> from a closed enum. The gate is
# FAIL-OPEN (missing/stale/malformed verdict -> allow), so a broken selector
# never wedges claiming. exit 2 = refused (distinct from, but same
# "claim-refused, pick again" meaning as, the daemon conflict exit 2 below).
GATE_RC=0
GATE_ARGS=(--agent "$AGENT" --goal-id "$GOAL_ID" --deviation "$DEVIATION")
if [ -n "$VERDICT_FILE" ]; then
    # Absent in every production call, so the gate's behaviour for production
    # callers is byte-identical to before this flag existed.
    GATE_ARGS+=(--verdict-file "$VERDICT_FILE")
fi
# shellcheck disable=SC2086  # rt_python_launcher is intentionally word-split (`py -3` on Windows)
$(rt_python_launcher) "$CORE_ROOT/scripts/scorer-verdict-gate.py" \
    "${GATE_ARGS[@]}" || GATE_RC=$?
if [ "$GATE_RC" = "2" ]; then
    exit 2
fi
# Any non-2 rc (0 allow / fail-open, or an unexpected gate error) proceeds —
# the gate must never block the claim on its own bug.

# --- Claim-announce board post () -------------------------------
# After a SUCCESSFUL claim, atomically announce it on the coordination board —
# the ONLY surface that survives cross-box store partitions (: on
# 07-09 alpha's aspirations/team-state writes never left cc-04, but its board
# posts did; the read-side  fix can only see claims that were POSTED).
# This folds the honor-system Phase-4 board-post.sh step into the claim itself
# so a claim can never land un-announced. Invariants:
#   - FAIL-OPEN: a post failure MUST NEVER fail the claim (the claim already
#     committed in the daemon) — log to stderr, return 0.
#   - Only the rc=0 SUCCESS paths call this; conflict/rejection (rc 2/1) never
#     reach here, so they post nothing.
#   - STALE AS WRITTEN, corrected : this said "Agent-queue goals carry
#     NO claim (claimed_by unset in the response) -> skip the announce". That
#     held while claim() answered agent-queue goals 400; since  it
#     honors &source=agent and stamps claimed_by on BOTH paths, so the skip
#     guard below no longer fires and agent-queue claims DO announce. The
#     behavior was NOT changed here — see the guard's own comment for why that
#     is an open decision rather than an oversight.
_post_claim_effects() {
    local goal_id="$1" agent="$2" response="$3"
    local extracted claimed_by title multiunit _rest
    # Extract claimed_by + title[:60] + a multi-unit marker from the claim
    # response, tab-separated. Fail-open on any parse error (empty -> skip).
    extracted="$(printf '%s' "$response" | $(rt_python_launcher) -c "
import json, re, sys
try:
    resp, _ = json.JSONDecoder().raw_decode(sys.stdin.read())
    g = resp.get('goal') or {}
    cb = (g.get('claimed_by') or '').strip()
    t = (g.get('title') or '').replace(chr(9), ' ').replace(chr(10), ' ')[:60]
    # : does this goal's own text instruct one-unit-per-pass? Same
    # trigger worker-loop Phase 2.95 states in prose, evaluated here so BOTH
    # orchestrators get it (see the advisory note below).
    _blob = (g.get('title') or '') + ' ' + (g.get('description') or '')
    # : the bare alternative 'multi-unit' was ANTI-CORRELATED with its
    # target and is now anchored to a DECLARATIVE use. Measured over 2,127 open
    # goals: 14 matched, 10 also matched an instructional phrase, and the 4 that
    # hinged on the bare form were false positives every one -- prose ABOUT the
    # mechanism (citing  by title, reporting the contract landed,
    # describing a multi-unit session, naming this remedy). Zero true positives
    # were lost. This is guard-2096's class: a text detector over a corpus that
    # documents its own findings re-flags every correction it causes, loudest on
    # the goals written about it -- so the FP rate GROWS as the mechanism earns
    # more goals. Declaring a goal multi-unit still fires ('this is a multi-unit
    # goal'); merely mentioning the word no longer does.
    _mu = re.search(
        r'one at a time|one per pass|one unit per|one PR (?:each|per)'
        r'|this (?:goal )?is (?:a )?multi[- ]unit|one[- ]by[- ]one',
        _blob, re.I,
    )
    print(cb + chr(9) + t + chr(9) + (_mu.group(0) if _mu else ''))
except Exception:
    print(chr(9) + chr(9))
" 2>/dev/null)" || true
    claimed_by="${extracted%%$'\t'*}"
    _rest="${extracted#*$'\t'}"
    title="${_rest%%$'\t'*}"
    multiunit="${_rest#*$'\t'}"
    # --- multi-unit claim advisory () -----------------------------
    # WHY HERE AND NOT IN A SKILL. The unit-level claim (unit-claim.sh) shipped
    # wired into the WORKER path only, as worker-loop Phase 2.95 — so a reducer,
    # which also executes goals through aspirations-execute Phase 4, had no
    # unit-claim step and the two could build the same unit concurrently. The
    # obvious fix is a second prose block in aspirations-execute/SKILL.md; this
    # is the same fix with two advantages and one cost avoided:
    #   * COVERAGE. Both orchestrators call THIS wrapper (aspirations/SKILL.md
    #     and worker-loop/SKILL.md), so one insertion covers both entry points.
    #     A per-skill insertion covers only the skill it is written into, and a
    #     guard enforced at one entry point says nothing about the other
    #     (guard-4376) -- which is exactly how the gap arose.
    #   * BUDGET. aspirations-execute/SKILL.md is in the `loop-skills` hot-path
    #     set under a RATCHET (may not exceed its size at HEAD), so a prose
    #     insertion costs a `size-budget-override:` trailer plus a ledger row,
    #     and is then paid on every loop iteration of every agent forever.
    #     core/scripts/*.sh is in no budgeted set: this costs zero hot-path
    #     bytes. Sizes at decision time: aspirations-execute/SKILL.md 77,977 B
    #     within a 1,106,805 B loop-skills set.
    # ADVISORY, NOT A GATE, deliberately. A wrapper-side GATE is bypassable by a
    # direct endpoint POST and belongs in the daemon (guard-742/guard-554); this
    # is a reminder printed to the caller that just claimed, and the caller
    # reading this stderr IS the actor that would acquire the unit claim.
    # It always fires on a match rather than first checking for a held unit
    # claim: the goal claim precedes the unit claim by construction, so at this
    # moment none is held, and skipping the extra subprocess keeps the claim
    # path cheap.
    if [ -n "$multiunit" ]; then
        printf '%s\n' "[aspirations-claim] ADVISORY (g-306-323): ${goal_id} names a per-unit protocol (matched \"${multiunit}\"). Before writing any code, NAME your unit and claim it:" >&2
        printf '%s\n' "    bash core/scripts/unit-claim.sh acquire ${goal_id} <unit-token>" >&2
        printf '%s\n' "  rc=1 means another Body holds that unit — pick a different one, never --force past a live holder. Release it when the unit ends. The goal claim is FREE between units, so nothing else records which unit is in flight." >&2
    fi
    # --- iteration-checkpoint anchor () --------------------------
    # Third instance of the same fold as the two blocks below: creation was
    # LLM-discretionary (ONE executable `loop-state-save.sh init` call site in
    # the whole repo — aspirations-select/SKILL.md Phase 2.95) while DELETION is
    # bash-enforced (iteration-close.sh `rm -f`). So a loop that selects by
    # calling goal-selector.sh directly instead of Skill(aspirations-select)
    # never anchors, and the checkpoint stays absent for the REST of the
    # session — every downstream reader then degrades silently and fail-open.
    # Measured on this box: 101 `update_against_missing_checkpoint` rows in
    # agents/<agent>/session/checkpoint-miss.jsonl.
    #
    # WHY source is "${SOURCE:-world}" and no longer the literal "world"
    # (). The old text asserted the goal was "a world-queue goal by
    # construction", because claim() hardcoded the world queue and answered
    # agent-queue goals 400 `agent_queue_goal`. That is no longer true: claim()
    # now honors `&source=agent` and runs the full session-scoped guard stack on
    # the agent queue. Leaving the literal here would have written an anchor
    # labelling an AGENT goal as source=world the moment that path went live —
    # a defect this wrapper would have introduced into every downstream
    # checkpoint reader, silently.
    #
    # THE LANDMINE BELOW IS STILL LIVE — the sequencing changed, the hazard did
    # not. Do NOT "finish the job" by dropping the loop digest's
    # `IF source==world` claim guard in the same change as an endpoint edit. The
    # digest is LLM-read markdown and takes effect on every agent's NEXT
    # iteration; mind_api/src is only picked up when the daemon recycles (there
    # is no autoreload — verified 2026-08-06). Flip both together and
    # agent-source iterations call a daemon that still 400s, which the digest
    # reads as "journal abort + LOOP_CONTINUE", silently halting the entire
    # recurring cadence (.. all live in the agent queue).
    # Correct order: land the endpoint -> commit (post-commit recycles the
    # daemon) -> confirm the LIVE endpoint accepts source=agent -> only then the
    # digest. Phase 2.95 therefore REMAINS load-bearing as the agent queue's
    # only serialization until that lands. Tracked by , which also
    # carries the fourth call site the original note missed: Phase 5.3's release
    # is world-guarded too, and a claim protocol with no matching release
    # strands a claim on every recurring cadence goal.
    #
    # ENSURE, not overwrite: writes only when no checkpoint exists or it
    # anchors a DIFFERENT goal. When Phase 2.95 already ran it wrote a RICHER
    # anchor (selector_score, skill, cross_agent_owner) and an unconditional
    # init here would silently downgrade it.
    #
    # Path resolution is delegated to loop-state-save.sh on purpose — this
    # wrapper does a skinny PROJECT_ROOT resolve and must NOT inline a 6th
    # AGENTS_PARENT_DIR copy (see the header note on the same constraint for
    # runner-token). FAIL-OPEN throughout.
    local cp_goal asp_num
    cp_goal="$(MIND_AGENT="$agent" bash "$CORE_ROOT/scripts/loop-state-save.sh" read 2>/dev/null \
        | $(rt_python_launcher) -c "
import json, sys
try:
    print((json.loads(sys.stdin.read() or 'null') or {}).get('goal_id') or '')
except Exception:
    print('')
" 2>/dev/null)" || true
    # Strip CR before comparing (fresh-eyes finding, this goal). On Windows the
    # whole round-trip is text-mode: loop-state-save.py `_atomic_write` opens with
    # os.fdopen(fd,"w") so the file gets \r\n, cmd_read prints it back through a
    # text-mode stdout, and the python above re-emits goal_id via print() — also
    # text mode. $( ) strips the trailing \n but NOT the \r, so cp_goal arrives as
    # "g-NNN-NN\r", never equals "$goal_id", and the ENSURE guarantee below
    # inverts into overwrite-every-claim — silently downgrading Phase 2.95's
    # richer anchor on exactly one platform. NOT reproducible off Windows (the
    # \r is added by the interpreter's stdout translation, not by any file
    # content a test can seed), so this is defensive by inspection rather than
    # test-pinned; stating that limit beats shipping a source-text-only test.
    cp_goal="${cp_goal//$'\r'/}"
    # asp-NNN from g-NNN-NN[-a]; asp-xw-<ts> from g-xw-<ts>-NN (both forms are
    # in loop-state-save.py SCHEMA's id patterns).
    case "$goal_id" in
        g-xw-*) asp_num="xw-$(printf '%s' "$goal_id" | cut -d- -f3)";;
        g-*)    asp_num="$(printf '%s' "$goal_id" | cut -d- -f2)";;
        *)      asp_num="";;
    esac
    if [ -n "$asp_num" ] && [ "${cp_goal:-}" != "$goal_id" ]; then
        printf '{"goal_id":"%s","aspiration_id":"asp-%s","source":"%s","phase":"selected","selected_at":"%s"}' \
            "$goal_id" "$asp_num" "${SOURCE:-world}" "$(date +%Y-%m-%dT%H:%M:%S)" \
            | MIND_AGENT="$agent" bash "$CORE_ROOT/scripts/loop-state-save.sh" init \
                >/dev/null \
            || echo "[aspirations-claim] WARN: iteration-checkpoint init failed for ${goal_id} (claim still succeeded)" >&2
        # stdout is dropped (the "anchored:" line is noise on a successful claim)
        # but stderr is deliberately NOT — init's own WARN names WHY it refused
        # (e.g. an aspiration_id that fails the SCHEMA pattern), and the generic
        # message above cannot. Swallowing both channels would leave a failure
        # visible but undiagnosable, which is the half-fix rb-5454 warns about.
    fi
    # --- in_flight / current_focus stamp () ----------------------
    # Folds the honor-system `team-state-in-flight.sh` step (documented in
    # coordination.md as "Phase 4 claim, before board post") into the claim
    # itself — the same treatment  gave the board announce below,
    # for the same reason: LLM-executed pseudocode drifts. VERIFIED 2026-07-26:
    # the setter had ZERO callers anywhere in the codebase (grep), so every
    # write depended on an LLM remembering the step — and execution was UNEVEN,
    # not uniformly absent. zeta stamped correctly at 19:14; foxtrot never did
    # (current_focus frozen 2h+ on an already-yielded goal across six claims).
    # An UNEVEN signal is worse for consumers than an absent one: a null
    # in_flight cannot be distinguished from "partner genuinely idle", so the
    # three readers below silently mis-answer instead of failing loud — the
    # aspirations-select partner-claim filter,
    # goal-pickup-coordination-check's partner-in_flight-GATED uncommitted-
    # collision probe (guard-741), and _cross_agent_attribution_filter's
    # "Source 1" concurrent-work timestamps.
    # Deliberately NOT gated on claimed_by (unlike the announce): an
    # agent-queue goal carries no world claim, but the agent is genuinely
    # working and the working tree is shared per-box, so partners still need
    # the liveness + uncommitted-ownership signal. FAIL-OPEN — a stamp failure
    # must never fail a claim that already committed in the daemon.
    MIND_AGENT="$agent" bash "$CORE_ROOT/scripts/team-state-in-flight.sh" \
        --agent "$agent" --goal-id "$goal_id" \
        --title "${title:-$goal_id}" --phase 4 \
        >/dev/null 2>&1 \
        || echo "[aspirations-claim] WARN: in_flight stamp failed for ${goal_id} (claim still succeeded)" >&2
    # --- claim-time diary breadcrumb () --------------------------
    # FOURTH instance of the same fold as the three blocks above, and the only
    # one that closes a claim-LOSS path rather than a drift path.
    #
    # stranded-claim-sweep's liveness predicate is "an execution-diary entry
    # exists after claimed_at". Every diary-append point in aspirations-execute
    # Phase 4 is CONDITIONAL (trivial_mode / non-empty inbound signals / infra
    # SKIP / Gate D, which is OFF by default / a size-or-duration threshold / a
    # probe surprise), and the unconditional phase_start/phase_end markers are
    # written by iteration-close -- i.e. at CLOSE, after the window has already
    # elapsed. So the uncovered window is exactly claim -> first close phase,
    # which for a deep goal is the ENTIRE execution. Measured first-hand
    # 2026-08-18 (zeta, cc-02): the sweep released  26 minutes into an
    # execution that had already opened AND merged a PR, verdict "stranded",
    # against a 5-minute stale threshold.
    #
    # THE ONE CLAIM-TIME WRITER THAT ALREADY EXISTED IS GATED THE WRONG WAY
    # AROUND, which is why the fix is a write rather than a threshold change:
    # scorer-verdict-gate.py emits its scorer_override entry only "on a
    # sanctioned deviation", so an agent that DEVIATES is protected while one
    # that takes the scorer's top pick -- the compliant and far more common
    # path -- writes nothing and is the exposed one. Measured on cc-08
    # 2026-08-18: three claims in one session all carried --deviation and so all
    # had a claim-time entry; their claim->close gaps were 19m27s and 12m39s,
    # both far past the 5-minute threshold, i.e. each would have been releasable
    # without it.
    #
    # WHY HERE AND NOT IN THE SWEEP. The alternative (teach the sweep a second
    # liveness signal) means another fail-safe KEEP branch -- exactly the shape
    # guard-4000 warns about, since a KEEP that is right at 5 minutes is wrong at
    # 10 days and an early-return branch cannot tell them apart because it never
    # reads the age. Writing the breadcrumb makes the EXISTING predicate true by
    # construction and adds no branch to the decision tree. The sweep's
    # read-through-cache hazard (guard-3992) is already handled on its own side
    # by the store-of-record diary probe.
    #
    # UNCONDITIONAL BY PLACEMENT: this sits ABOVE the `[ -z "$claimed_by" ]`
    # early return below, so it fires on agent-queue claims too -- the same
    # reasoning the in_flight block states for itself. Moving it below that
    # return silently re-opens the gap for one entire queue.
    #
    # NOT refused on a worker Body, verified before being relied on: cmd_append
    # exits 0 without writing when _is_observer_session() is true, and a worker's
    # MIND_SID does differ from the reducer's -- but that predicate reads the
    # LOCAL agents/<agent>/session/running-session-id, which a worker never
    # writes, so the absent file yields False. Probed through the production
    # function on cc-08 rather than re-implemented (guard-4323).
    #
    # FAIL-OPEN like its three siblings: a breadcrumb failure must never fail a
    # claim that already committed in the daemon.
    # The content literal is CLAIM_BREADCRUMB_MARKER in stranded-claim-sweep.py,
    # which skips it when judging execution activity (2026-08-30: counting it
    # made every claim look live forever). Reword both ends together.
    printf '{"entry_type":"observation","goal_id":"%s","content":"claim-time liveness breadcrumb for %s (source=%s) - g-115-6677"}' \
        "$goal_id" "$goal_id" "${SOURCE:-world}" \
        | MIND_AGENT="$agent" bash "$CORE_ROOT/scripts/execution-diary.sh" append \
            >/dev/null 2>&1 \
        || echo "[aspirations-claim] WARN: claim-time diary breadcrumb failed for ${goal_id} (claim still succeeded)" >&2
    # Skip when the response carried no claimed_by. READ THE NEXT SENTENCE BEFORE
    # RELYING ON THIS: the guard no longer separates agent-queue from world-queue
    # claims. It was written when claim() answered agent-queue goals 400 and only
    # world claims stamped claimed_by; since  claim() honors
    # &source=agent and stamps claimed_by on BOTH paths (aspirations_write.py
    # claim(), unconditional), so this predicate is now effectively always false
    # and agent-queue claims DO announce on the coordination board. Nothing edited
    # this line — its MEANING changed underneath it when the value it tests
    # changed. Whether announcing a private-queue claim is desirable is an OPEN
    # decision ( re-check): skipping it was justified as noise control on
    # a single-agent queue, which is still arguable, but that is a behavior change
    # and was deliberately not made here. Verified, not inferred ().
    [ -z "${claimed_by:-}" ] && return 0
    printf '%s' "Claiming ${goal_id}: ${title}" \
        | MIND_AGENT="$agent" bash "$CORE_ROOT/scripts/board-post.sh" \
            --channel coordination --type claim --tags "claim,${goal_id},${agent}" \
            >/dev/null 2>&1 \
        || echo "[aspirations-claim] WARN: claim-announce board post failed for ${goal_id} (claim still succeeded)" >&2
    return 0
}

QUERY="id=${GOAL_ID}&agent=${AGENT}"
# Session identity on the claim ( slice 1, ADDITIVE — records only,
# changes NO refusal behavior). Claims are identified by AGENT NAME alone, so
# two sessions of the SAME agent both "succeed" and neither is warned (observed
# live 2026-07-25: two sessions of one agent held the same world goal 16min
# apart; the second was one write away from creating duplicate credentials in an
# external service). Nothing session-scoped was even TRANSMITTED, so the
# endpoint could not tell them apart in principle. MIND_SID is injected into every Bash call by
# bash-agent-inject.py. Best-effort: an empty value is simply omitted, so a
# caller without it behaves exactly as before. Deliberately SID-only — this
# script does a skinny PROJECT_ROOT resolve and does NOT source _paths.sh (see
# the header), so reading the runner-token file would need a 6th inlined
# AGENTS_PARENT_DIR copy (CLAUDE.md tracks 5). If SID reuse across windows
# (--continue / --resume) later proves to matter here, add runner-token via the
# daemon side, which already has ctx.paths, rather than inlining a path here.
if [ -n "${MIND_SID:-}" ]; then
    QUERY="${QUERY}&sid=$(rt_url_encode "$MIND_SID")"
fi
if [ -n "$CROSS_LANE" ]; then
    ENCODED_CL="$(rt_url_encode "$CROSS_LANE")"
    QUERY="${QUERY}&cross_lane=${ENCODED_CL}"
fi
# Lane-pin override (). Omitted when empty, so a caller that never
# passes it sends a byte-identical request to what a pre-change daemon receives.
if [ -n "${OVERRIDE_LANE_PIN:-}" ]; then
    QUERY="${QUERY}&override_lane_pin=$(rt_url_encode "${OVERRIDE_LANE_PIN}")"
fi
# : forward the queue selector. Omitted when empty so the endpoint's
# "world" default applies and no existing caller changes behavior. Sending
# `&source=world` explicitly would also be harmless, but omitting keeps the
# request shape identical to what a pre-change daemon receives — which matters
# during the window between this commit and the daemon recycle.
if [ -n "${SOURCE:-}" ]; then
    QUERY="${QUERY}&source=$(rt_url_encode "${SOURCE}")"
fi

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/claim --query "$QUERY" 2>&1)" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        _post_claim_effects "$GOAL_ID" "$AGENT" "$RESPONSE"
        exit 0;;
    2)
        # T2.2: parity with CLI cmd_claim exit code. cross_lane_refused -> exit 2.
        # lane_pin_refused joins it (): same class — a routing-POLICY
        # refusal with a documented override — not a malformed-request error.
        if echo "$RESPONSE" | grep -qE '"(cross_lane_refused|lane_pin_refused)"'; then
            printf '%s\n' "$RESPONSE" >&2
            exit 2
        fi
        printf '%s\n' "$RESPONSE" >&2
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback. Try one
        # auto-spawn, then fail loud. See .claude/rules/no-python-cli-fallback.md.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/claim --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                _post_claim_effects "$GOAL_ID" "$AGENT" "$RESPONSE"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-claim.sh";;
    *)
        exit $rc;;
esac
