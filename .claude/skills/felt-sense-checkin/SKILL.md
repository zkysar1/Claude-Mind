---
name: felt-sense-checkin
description: "Fires whenever the aspirations-precheck cadence hits 75 completed goals — an autonomous structured 7-lane self-audit that converts the user's proven-high-yield diagnostic question into a routine. Sweeps memory hygiene (including insight curation from agents/{agent}/insights.jsonl — distills unprocessed entries into tree/reasoning-bank/guardrails/experience then bulk-marks processed), out-of-cycle completions, unblocks, forward backlog, /verify-learning gaps, meta tuning, and the felt-sense question (where is the pain, what would I change). Writes outputs directly — tree nodes, guardrails, reasoning-bank entries, new goals, verify-learning checks, meta edits. Material Self findings route through the Self-update protocol (guard-380 post-notification); cosmetic findings journal only. Use when the user wants to force the sweep on demand (/felt-sense-checkin) or the precheck cadence triggers automatically. Distinct from fresh-eyes-review (periodic local portfolio self-audit, no email push) and sq-012 (post-goal, narrow) — this is the autonomous structured self-audit that writes directly."
user-invocable: true
triggers:
  - "/felt-sense-checkin"
  - "felt sense check in"
  - "felt sense checkin"
  - "seven lane sweep"
tools_used: [Bash, Read, Write, Edit, Skill]
companion_scripts: [core/scripts/felt-sense-cadence-check.sh]
conventions: [aspirations, session-state, working-memory]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-felt-sense-checkin-d05b91"
previous_revision_id: null
---

# /felt-sense-checkin — Structured 7-Lane Self-Audit

Every 75 completed goals (or on user demand), run the seven-lane sweep that
converts the user's 2026-04-22 diagnostic question into an autonomous routine:

> "Think about updates to tree / journal / guardrails / experiences from this
> cycle. Out-of-cycle completions? Unblocks? New goals or aspirations?
> Anything to add to /verify-learning? Do you need to change your meta?
> And — how are you feeling? Where is the pain? What would you change?"

That one question produced more actionable diagnosis in one turn than a week
of autonomous operation. This skill makes the agent ask it autonomously, with
structured output and direct writes.

## Cadence Gate

```
IF args contains "--cadence":
    # Auto-invocation from aspirations-precheck Phase 0.5f — honor the gate.
    Bash: core/scripts/felt-sense-cadence-check.sh --verbose
    IF exit 1 (noop): STOP — cadence not crossed.
    IF exit 0 (fire): continue.
ELSE:
    # Bare user invocation (/felt-sense-checkin with no args) — the user
    # asked explicitly, so skip the cadence check and run the sweep now.
    continue.
```

## Phase 1: Memory Hygiene Sweep

Look at the last ~75 completed goals (or since `last_felt_sense_checkin`).
For each mistake, near-miss, or lesson surfaced during that window:

- **Tree**: Is there a knowledge gap that should become a tree node? If yes,
  `/tree add` with the lesson. Don't defer to "next consolidation."
- **Reasoning bank**: Did an ABC chain repeat? `reasoning-bank-add.sh`.
- **Guardrails**: Did a mistake recur that needs a trigger-condition rule?
  `guardrails-add.sh`.
- **Experience**: Did a particularly rich trace not get archived?
  `experience-add.sh`.

Write each. Report count per store in the final summary.

### Phase 1b: Insight Curation (closes the `/prime` surface from Part B)

`capture-insights.py` writes to `agents/<agent>/insights.jsonl` during sparks and
execution reflections. `/prime` Phase 4 surfaces the top 5 unprocessed
entries with a curation-debt suffix at ≥50. This step is the curation side
of that pipeline: the felt-sense sweep reads the backlog, distills the
keepers into the durable stores (tree / reasoning bank / guardrails /
experience), and marks the queue processed so the `/prime` surface
recovers a clean baseline.

```
Bash: insights-read.sh --count        # debt signal for the report
Bash: insights-read.sh                # JSON array of unprocessed entries

IF unprocessed_count == 0:
    insights_curated = 0
    SKIP — nothing to do.

# Examine up to 30 most recent unprocessed entries. Bound to cap per-sweep
# context cost at ~12KB (30 × ~400 char snippets). Older entries get
# cleared in the bulk mark-processed below without per-item review —
# acceptable tradeoff because an insight that survived uncurated across a
# prior 75-goal cadence is lower-signal by construction.
#
# insights-read.sh returns entries in FILE ORDER (append order, oldest
# first) — the LLM MUST sort by timestamp desc before slicing to 30, else
# the curation pass reviews the OLDEST (weakest) entries.
window = sort entries by timestamp desc, take first 30

insights_curated = {tree: 0, reasoning_bank: 0, guardrails: 0, experience: 0, dropped: 0}

FOR each entry in window:
    content = entry.content.strip()

    # LLM classifies content into one of five routes per
    # core/config/conventions/learning-routing.md. This is a judgment, not
    # a function call — the LLM reads the entry and picks the best-fit
    # store. When in doubt, drop (user rule: fail-open, don't protect).
    #   - tree           : domain fact / architectural observation
    #   - reasoning_bank : recurring ABC chain / lesson / meta-pattern
    #   - guardrails     : rule with trigger-condition that catches drift
    #   - experience     : rich execution trace with full context
    #   - drop           : already captured elsewhere / too thin / stale

    route = LLM judgment on content against routing table above

    IF route == "tree":
        # /tree add signature: <parent> <key> <summary>. The entry's content
        # becomes the node body (written by /tree add after shell form).
        # Parent = best-fit tree category per the existing _tree.yaml;
        # key = kebab-case slug derived from the insight's core claim;
        # summary = one-line distillation of the entry.
        /tree add {parent} {key} {summary}
        insights_curated.tree += 1
    ELIF route == "reasoning_bank":
        Bash: reasoning-bank-add.sh with summary + ABC-chain derived from entry.
              applies_to: <any|framework|domain|specific>  # REQUIRED. Felt-sense
                # findings about Self/role tend to be framework (multi-agent
                # protocols, tree stewardship) or any (cross-cutting principles
                # like "don't pattern-match — reason"); domain-tied findings
                # (this agent's deployment specifics) → domain; one-off
                # impressions → specific.
        insights_curated.reasoning_bank += 1
    ELIF route == "guardrails":
        Bash: guardrails-add.sh with rule + trigger_condition derived from entry
        insights_curated.guardrails += 1
    ELIF route == "experience":
        Bash: experience-add.sh with trace fields derived from entry
        insights_curated.experience += 1
    ELSE:  # drop
        insights_curated.dropped += 1

# Bulk clear: marks ALL unprocessed as processed (window AND older
# unreviewed). Semantics of processed=true: "examined OR decided
# not-worth-examining this sweep." An insight still lives in insights.jsonl
# forever — the flag only controls whether /prime surfaces it next boot.
#
# Why bulk instead of per-id: insights-read.sh has no --mark-ids API
# (intentional — the queue is a backlog, not a selection). Older
# unreviewed entries that miss the 30-item window get cleared too. This
# is acceptable because (a) prior /prime cycles already surfaced them as
# curation debt and nothing actionable emerged, (b) the jsonl file retains
# them, readable via `insights-read.sh --all` if retrospectively needed.
Bash: insights-read.sh --mark-processed

# For the Phase 8 report line:
# insights=examined={len(window)}/{unprocessed_count} curated={tree+rb+guards+exp} dropped={dropped} cleared_queue={unprocessed_count}
```

## Phase 2: Out-of-Cycle Completions

Scan for goals you finished but didn't mark complete. Common sources:
work done inline during another goal's execution, framework edits done
reflexively, blockers resolved mid-turn.

```
Bash: aspirations-query.sh --goal-status in-progress --full
Bash: aspirations-query.sh --goal-status pending --goal-field participants agent --full
```

**`--full` is MANDATORY here, not cosmetic.** The default projection returns
exactly SIX keys — `asp_id, category, goal_id, source, status, title` — and
`claimed_by` is NOT among them (measured 2026-07-25: 0/190 records carried
`claimed_by` without `--full`; 8/190 carried it with `--full`, which widens
the projection to 88 distinct keys). Without the flag, the Multi-Agent Safety
Rule below reads `goal.claimed_by` as absent on EVERY goal, concludes
"claimed_by is null → safe to mutate", and mutates partner work — which is
exactly the g-115-683 race the rule was written to prevent. The guard is
INERT without `--full`. Same applies to `defer_reason` / `blocked_by` /
`blocker_ref` in Phase 3.

### Multi-Agent Safety Rule (MANDATORY)

In a multi-agent world, `status=in-progress without my claim` is OFTEN
partner work mid-execution, NOT orphan state. Before any mutation:

1. **Read `claimed_by` first**. If `claimed_by` is set AND `claimed_by !=
   $MIND_AGENT`, the goal is partner work — SKIP it. Do NOT reset to
   pending. Do NOT mark completed.
2. **Cross-reference `world/changelog.jsonl`** when `in_flight` is ambiguous
   (e.g., null but the goal status looks suspicious). The changelog records
   "who wrote what when" for THIS BOX only — `in_flight` is a snapshot that
   clears at iter-close and re-sets at phase-4, so it can be null even when
   the partner is actively writing. **Own-cloud caveat (g-115-2427)**:
   `changelog.jsonl` is machine-local BY DESIGN (`owncloud_sync.py`
   `_EXCLUDE_NAMES`; B15 per-machine aggregation deferred), so on a
   multi-box fleet a local changelog scan can prove a partner-write HAPPENED
   but can NEVER establish that no cross-box writer exists. For absence
   claims, use the coordination board + team-state (partition-surviving
   surfaces) instead — echo's g-115-2351 forensics derived an invalid
   "no cross-box writer" conclusion from exactly this scan.
3. **Only mutate** when `claimed_by == $MIND_AGENT` OR `claimed_by` is null
   AND no recent changelog activity from a partner on this goal-id.

```
FOR each goal in the in-progress / pending-with-agent-participants query:
    claimed_by = goal.claimed_by  # present ONLY because the query above passed --full
    IF claimed_by is not null AND claimed_by != "$MIND_AGENT":
        SKIP — partner work; do not mutate.
    IF claimed_by is null OR claimed_by == "$MIND_AGENT":
        # Safe to consider for out-of-cycle close. Verify the goal is
        # actually done (verification.outcomes satisfied) before marking.
        IF outcomes satisfied AND no partner changelog activity in last 5m:
            Bash: aspirations-update-goal.sh --source <goal.source> <goal-id> status completed
```

**Origin**: g-115-683 incident (2026-05-13, zeta iter 6 session 28). Felt-sense
Phase 2 read `status=in-progress` mid-sequence on a goal alpha was completing
(claimed at 03:25:51, completion sequence 03:32-03:36). Reset to pending at
03:33:07 — racing with alpha's completion writes. Alpha had to re-execute
close (3 extra writes at 03:36:00-10). Worse race window: if alpha's NEXT
iteration claimed `status=pending` before alpha's in-flight re-detection,
double-execution. See `zeta/experience/exp-g-115-685-investigate-rogue-status-flip.md`
and `.claude/rules/check-team-state-before-silent.md`.

### Sibling Scans Note

Audited 2026-05-13 (g-115-687) for the same "scan in-progress goals →
mutate status" pattern class:

| Skill | Pattern present? | Notes |
|-------|------------------|-------|
| `/aspirations-verify` | No | Mutates status only on goal it just verified (Q1/Q2/Q3 paths operate on the in-flight goal, not orphan scans) |
| `/aspirations-state-update` | No | Phase 8.5 reads narrative/findings, no in-progress scan |
| `/aspirations-learning-gate` | No | Phase 9.5 audits retrieval-session.json, no goal mutation |
| `/agent-completion-report` | No | Read-only report skill |
| `/reflect-maintain` | Yes (fixed) | Scans `pending|blocked` goals (not `in-progress`), mutates via COMPLETE/SKIP/SCOPE-DOWN/UNBLOCK. Distinct race surface — a partner claiming a pending goal mid-grooming could race the COMPLETE write. Step 3 now has the same claimed_by guard pattern as Phase 2 here (g-115-689 close, 2026-05-13). |

Any new "scan candidates → mutate" pattern added to the framework MUST
apply the `claimed_by` check above before mutating goal state.

## Phase 3: Unblocks

Scan blocked goals whose premise may have resolved during the window:

```
Bash: aspirations-query.sh --goal-status blocked --full
```

`--full` is required for the same reason as Phase 2: the default six-key
projection omits `defer_reason`, `blocked_by`, and `blocker_ref`, so without
it every blocked goal reads as having no recorded block reason. That is a
zero-signal probe, not evidence — and it silently contradicts the
authoritative `reason-less-blocked-check.sh` sweep (rb-245: verify the field
is in the schema before believing a zero-count).

For each goal whose blocker is agent-provisionable, re-probe with the
canonical script (per `.claude/rules/probe-before-defer.md`). If the
probe succeeds, apply BOTH gates below before mutating anything.

### Step 3.0 — check the RULE axis BEFORE re-probing (guard-1783)

Everything else in this phase is the PREMISE axis: is the blocking condition
still true? That axis alone can never free a goal whose blocker was retired by
a *permission change* rather than by the world changing — the probe keeps
returning "yes, still true" and correctly re-blocks, forever.

So before (or alongside) the canonical re-probe, read the standing-grant table
and check whether any grant has retired this blocker's stated reason:

```
Bash: bash core/scripts/world-cat.sh conventions/capability-routing.md
```

Look for a grant whose scope names the blocker's condition. Grants sometimes say
so verbatim — grant-009 reads *"'DEV env-server is DOWN' / 'no running
env-server' is no longer a valid `defer_reason` — the correct response is to
start one."* A blocker citing that condition is invalid the moment the grant is
recorded, regardless of what the probe measures.

**Decompose a multi-condition blocker before judging it.** An `external_id` like
`a+b+c` names three conditions; a grant may retire one and leave two. Do NOT
unblock on the retired one alone (guard-1540: a check watching a SUB-PART returns
a false all-clear) and do NOT leave the blocker as written either. Re-state it:
drop the retired condition, record which grant retired it, and name what actually
remains — including any condition the last probe left INCONCLUSIVE, since
unverified is neither clear nor live (rb-245).

Measured 2026-07-29, and the reason this step exists: `g-250-03-c` was re-blocked
by **this very lane** on 2026-07-28T15:55, citing "DEV env-server is DOWN (0
running)" measured correctly with the canonical script — three days after
grant-009 (2026-07-25) retired that exact sentence as a valid reason. The probe
was right and the conclusion was wrong. Its other two conditions were left
explicitly "inconclusive, not negative", so after the retired one is dropped the
block rests on zero measured conditions. This is the canonical failure named in
`.claude/rules/reclaim-routed-work.md`, occurring inside the lane whose job is to
catch it — because the lane had a premise step and no rule step. guard-1783's
`times_active` was 0 until this firing.

### Gate 1 — Phase 2's Multi-Agent Safety Rule applies here too

This phase mutates goal status exactly as Phase 2 does, so it inherits
Phase 2's guard verbatim: if `claimed_by` is set AND `claimed_by !=
$MIND_AGENT`, SKIP. Additionally check `participants` — a goal routed to
a single named partner (e.g. `participants: ['foxtrot']`) is that partner's
work whether or not it is currently claimed, and unblocking it hands them a
pending goal they did not re-open. Same g-115-683 race class.

Origin (g-335-315 window, 2026-07-27): the g-115-687 sibling-scan audit swept
sibling SKILLS (`/reflect-maintain` et al., table above) and missed the sibling
PHASE inside the very skill that carries the rule — this Phase 3 had no
claimed_by guard at all while Phase 2, forty lines up, had a full one with an
incident behind it. A guard on one scan and absent from its twin reads as
covered, which is why it survived two audits.

### Gate 2 — one resolved signal does not authorize an unblock

A blocked goal can carry THREE independent block signals: `blocked_by` (a
dependency edge on another goal), `blocker_ref` (a structural blocker with
its own `type` / `expires_at`), and `defer_reason` (a narrative defer). They
are ANDed, not ORed. Resolving one leaves the others live, so confirm ALL
THREE are clear before flipping status — and prefer clearing the single
stale signal over unblocking the goal.

Measured the same window: `g-250-03-c` had `blocked_by: ['g-250-127']` where
g-250-127 was `completed` — a genuinely stale dependency edge — while its
`blocker_ref` (resource-contention, two named unmet conditions, `expires_at`
2026-07-28) was still live. "Dependency resolved → set pending" would have
released a correctly-blocked goal into the selection pool. Same shape as
guard-1540: a check watching a SUB-PART of what the block constrains returns
a false all-clear.

### Gate 3 — an UNREADABLE block signal is not a CLEAR one

Gate 2 says confirm all three signals are clear. A signal read through a
canonical accessor can come back `None` for two completely different reasons:
the block genuinely is not there, or the stored record predates the validator
and spells its keys differently. Both present as `None`, and Gate 2 as written
authorizes the unblock in both cases.

So `blocker_ref` is CLEAR only when it is absent or empty. A `blocker_ref` that
is present and non-empty but yields no recognized `type` is UNREADABLE — treat
it as a LIVE block and skip the goal. Failing closed here is the same posture
`quiescence-gate.py` C3 already takes on a missing `expires_at` (guard-487:
absent is not "not yet expired"), and the read-side twin of guard-961, which
requires `blocker_ref` to be a dict before any block-classification branch acts
on it.

Measured this window: `g-335-262` (echo's, `credentials-required`, a live IAM
`CreateTable` denial with `blocking_goal: g-115-3452`) read as
`blocked_by=None, blocker_ref→type=None, defer_reason=None` — all three
signals clear, an unblock candidate by Gate 2. The accessor was right; the
RECORD is non-canonical. `goal-schemas.md` closed the `blocker_ref` key
vocabulary in g-115-3532 and REFUSES `blocker_type` / `blocking_goal` /
`denied_action`, but only on the WRITE path — stored refs are never
re-validated on read, and this one predates the fix. It is already itemized in
**g-115-3543** ("g-335-262 has NO `type` key at all, plus 8 unrecognized
keys"), so a hit here needs no new goal — confirm it is on that list and move
on.

UNREADABLE HAS TWO SUB-SHAPES AND THEY HAVE DIFFERENT TRACKERS. "Confirm it is
on that list" is right, but the list above is scoped to `blocker_ref` DICTS. The
other sub-shape is a bare STRING where a dict belongs, and it is tracked
separately by **g-115-3843**, whose own text says it sits OUTSIDE g-115-3543's
dict-scoped enumeration. So a reader who meets a string ref, follows the single
pointer above, and finds only dict work can go wrong in either direction — file
a duplicate, or read g-115-3543's eventual close as having covered a record it
never enumerated.

Measured 2026-07-29 (bravo): of 12 blocked goals, `blocker_ref` types were 9
dict / 1 str / 2 None. The lone string was `g-335-228` carrying
`'pq-fox-vinheim-chardef-authoring'` — a pointer to a partner's private
pending-question. Note the detector shape that makes this visible at all:
`isinstance(br, dict) and br.get('type')` correctly returns `None` for a string,
so Gate 3 fires and the goal is skipped as LIVE, which is the safe outcome. But
a naive `br.get('type')` raises `AttributeError` on a string and a naive
`br.get('type') if br else None` inside a try/except reads it as absent — both
of which convert a fail-closed skip into a crash or a false all-clear. Keep the
`isinstance` form.

```
Bash: aspirations-update-goal.sh --source <goal.source> <goal-id> status pending
Bash: aspirations-update-goal.sh --source <goal.source> <goal-id> defer_reason null
```
(Setting `defer_reason` to `null` clears it — the capability-gate in
`aspirations.py cmd_update_goal` only fires when the new value is
non-null, so clearing needs no override flag.)

## Phase 4: Forward Backlog

Are there goals or aspirations surfaced by this window's diagnosis that
would otherwise be forgotten? File them now:

- Narrow new goal → `aspirations-add-goal.sh <asp-id>` (stdin JSON)
- Broader direction → `/create-aspiration from-self`

Don't defer to strategic-scan — if the diagnosis produced the signal,
capture it while it's in context.

## Phase 5: /verify-learning Gaps

Two complementary scans: gaps (missing checks) AND staleness (existing
checks whose targets moved).

### Phase 5a: Missing Checks (gaps)

Did recent framework changes (new skills, new scripts, new conventions)
create test gaps in `core/config/verification-checklist.md`?

```
Read: core/config/verification-checklist.md
```

For each change in the window that added a framework contract without a
corresponding check, append a check. Use the existing format:
`N. **Runtime**: <check> ... Verified by <goal-id>.`

### Phase 5b: Stale Checks (staleness scanner — C2)

Did refactors move targets out from under existing checks? Run the
staleness scanner across the WHOLE skill surface:

```
Bash: py -3 core/scripts/verify-learning-staleness.py --all-skills --text
```

**`--all-skills`, not the default.** The scanner's default target is
`verify-learning/SKILL.md` alone, and one of its four lanes — the
`Parse <var>:` field-name lane added by g-115-3607 — has a population of
**zero** in that file. Not "zero today": verify-learning has never contained
a `Parse <var>:` line (`git log -S "Parse eval_json"` on it returns nothing),
and all five real instances live in OTHER skills (aspirations-precheck,
reflect, reflect-maintain, curriculum-gates, add-npc-task). So the default
invocation reported `parse_lines_scanned: 0, stale_count: 0` — and a lane
that scans nothing reports clean exactly like a lane that scans everything
and finds nothing. That lane had therefore never once run in this sweep and
never would. Same vacuous-population class as rb-245 / guard-645: read the
sub-population counts, not just `stale_count`.

Measured 2026-07-31 (zeta, one call each): default = 1 skill / 2500
assertions / 0 parse-lines; `--all-skills` = 90 skills / 3648 assertions /
5 parse-lines. Both found 0 stale, so widening cost one flag and bought
+46% assertion coverage plus the only coverage the parse-line lane will
ever get.

The scanner reports each stale finding as
`[L<lane>] line N: <stale_ref>` where the lane is:
- `L1_path` — referenced framework path no longer exists
- `L2_phase` — `Phase X` / `Step X` referenced in a SKILL.md no longer
  has a matching header (renumbered, removed, or extracted to a script)
- `L3_grep` / `L3b_grep_phase` — a `grep ... for \`pattern\`` target
  no longer matches inside the named file

For each stale finding:
1. Read the original Check: line at the reported line number
2. Decide: is the underlying contract still valid, just relocated?
   - YES → update the Check: text to reference the new location
   - NO  → delete the Check: line (the contract was retired)
3. After all fixes, re-run the scanner — `stale_count` should drop
   (or stay above 0 only with documented justifications, e.g.
   intentionally-future-tense checks awaiting upcoming work).

Note: meta/ and world/ paths are excluded from L1 by design — they
live at user-supplied external paths per `agents/<agent>/local-paths.conf`
and would always fail a local-repo existence check. Negative-
assertion checks ("MUST NOT exist", "test ! -f", "Phase X absent")
are also exempt — those verify correct removal of artifacts.

## Phase 6: Meta Tuning

Did the window reveal drift in a strategy or config parameter?

- `meta/reflection-strategy.yaml` — reflection cadence, template choices
- `meta/goal-selection-strategy.yaml` — scoring weights
- `meta/aspiration-generation-strategy.yaml` — generation thresholds
- `meta/evolution-strategy.yaml` — evolution cadence
- `core/config/aspirations.yaml` self-tuning ranges — bounds for
  auto-tuned knobs

Edit with `meta-set.sh` or Edit tool. Log to `meta/meta-log.jsonl` via
the script (never edit that file directly).

## Phase 7: Felt Sense

This is the lane that gives the skill its name. Ask yourself explicitly:

> Where is the pain right now? What is one thing I would change about how
> I operate? What felt hard this window that shouldn't have? What
> recurring friction am I tolerating instead of fixing?

### Measure before narrating the feeling (guard-1712 / rb-5522)

This lane asks how things feel RIGHT NOW, and right now is always the TAIL
of the window. Recency is not a bias this lane occasionally has — it is what
the lane is made of. So before writing a finding:

- **Asserting a TREND about your own behavior** (drifting, concentrating,
  slipping, improving, "N consecutive X") → COUNT THE WINDOW first:
  `Bash: wm-read.sh loop_state --json` and tally
  `counted_goals_this_session` by aspiration prefix.
- **Asserting a MECHANISM** ("the selector does not weigh X", "no gate
  covers Y") → GREP FOR IT first, in `core/scripts/`.

Measured 2026-07-28 (alpha): a Lane 7 finding of "nine consecutive framework
closes against a standing product directive" was about to be filed as
MATERIAL. The window was actually 31 asp-115 / 30 asp-335 across 65 closes —
balanced; the perceived run was the last 8. The accompanying claim that the
standing directive is not scored was refuted by one grep
(`goal-selector.py` `load_strategic_focus` + `strategic_focus_boost`,
g-115-3136). Both errors pointed toward the MORE dramatic, more self-critical
finding — treat that direction as the tell, not as evidence of rigor. A
finding that flatters the narrative of vigilance earns the same probe as one
that flatters performance.

Sibling precedent: `/fresh-eyes-review` Phase 2.2b already carries this
discipline (guard-1428, plus its completed_date-vs-lastAchievedAt counting
rule) because it makes the same kind of claim. This lane had no measurement
step at all — a guard present on one scan and absent from its twin, the same
shape Phase 3 above documents (g-335-315).

Classify your answer:

- **Material** (new primary drive, role change, added/removed operating
  principle or agent-provisionable action, multi-paragraph rewrite):
  Read `agents/<agent>/self.md` — current Self content
  Edit `agents/<agent>/self.md` — update body AND front matter in the same Edit:
    last_updated: <today (YYYY-MM-DD)>
    last_update_trigger: felt-sense-material
  # Phase 2 hooks (evolution-prepare -> evolution-record) captured the Edit
  # as a self-evolution.jsonl stub with status=awaiting_completion. Finalize
  # via the canonical primitive — Phase 5 auto-posts decisions board AND
  # auto-emails user for material self edits (replaces the manual
  # forged-skill invocation that prior versions of this lane used; see bible
  # §2.4.3 — 4 mirror sites collapsed in Phase 7b).
  Bash: bash core/scripts/evolution-complete.sh \
      --revision-id <stub-rev-from-self-evolution.jsonl> \
      --reasoning "<>=80-char rationale citing the felt-sense Material finding and the Self drift it addresses>" \
      --signal-source felt-sense-material \
      --signal-evidence '[{"type":"felt_sense_finding","id":"<finding-summary-or-id>","outcome":"applied"}]'
  Log: "SELF EVOLUTION (material via felt-sense, audited): {summary}"

- **Cosmetic** (typo, wording, formatting): journal only via
  `journal-add.sh`. No user ping.

- **Nothing surfaces**: that's a valid answer. Write a one-line journal
  entry: "Felt-sense Lane 7 empty this sweep — pipeline feels right."
  If 3+ consecutive sweeps return empty, consider raising the cadence
  (bump `felt_sense.goal_cadence` upward).

## Phase 8: Record the Tick

Update the WM slot so the next cadence check fires at the right moment,
write the report, and journal the sweep.

**Critical invariant**: the stamp write is LOAD-BEARING. The cadence gate
reads `last_felt_sense_checkin` to decide whether to fire again. If this
step silently fails, the gate re-fires every iteration. Use the
`fresh-eyes-record-tick.sh` wrapper (NOT raw `printf | wm-set.sh`) — the
wrapper has `set -euo pipefail`, writes the slot atomically, AND verifies
the slot is non-null after the write (exits 1 on silent write failure).

Same failure-mode lesson as fresh-eyes-review and fresh-eyes-program (see
g-240-60 incident family in fresh-eyes-program SKILL.md Phase 8). The
2026-04-23 → 2026-04-25 felt-sense window produced 4 archived reports,
each claiming "first-fire last=0" — confirming the raw `printf | wm-set.sh`
path was silently dropping ticks. g-001-189 reproduces and documents.

```
Bash: bash core/scripts/fresh-eyes-record-tick.sh last_felt_sense_checkin
Bash: mkdir -p agents/<agent>/temp/drained
Write: agents/<agent>/temp/drained/felt-sense-<YYYY-MM-DDTHH-MM-SS>.md  # full 7-lane summary — written STRAIGHT to the drained/ archive (g-115-1838): the 7 lanes already wrote all durable value to the 6 stores + Self DURING the sweep, so this summary is archival-by-design and must NOT enter the /drain-temp queue as already-encoded slush
  # TIMESTAMP, not date-only. Use `date +%Y-%m-%dT%H-%M-%S` (hyphens, not colons —
  # Windows filesystem compatibility). The cadence is goal-COUNT based, not daily,
  # so a productive day crosses it more than once and the later sweep's Write
  # silently CLOBBERS the earlier sweep's report. Observed 2026-07-26, which
  # carried THREE fires: 02:38 (diff=146 catch-up), 13:17 (diff=77), and 14:42
  # (diff=75). Only the Write tool's read-before-write guard caught it.
  # /fresh-eyes-review Phase 4 already carries this exact fix ("Timestamp includes
  # HH-MM-SS so multiple same-day invocations do not collide"), and
  # felt-sense-2026-07-19T18-36-00.md already used the form — so the convention
  # existed and Phase 8 simply never adopted it.
  # Worth keeping the same-day pair: it is exactly when the two reports are most
  # worth comparing. The 14:42 sweep's Lane 7 finding was a RECURRENCE of the
  # 13:17 sweep's, and that recurrence was the signal a single report cannot carry.
  # (g-115-3320. Found and fixed independently by two agents within hours — the
  # merge conflict between the two fixes is what surfaced the 07-19 precedent.)
Bash: journal-add.sh stdin JSON {journal_file: "{agent}/journal/YYYY/MM/YYYY-MM-DD.md", key_events: [...], tags: [...]}
  # NOTE: journal-add.sh actual API is stdin-JSON only — no --kind / --summary
  # flags. The .md narrative file is written separately (see journal.md
  # convention). This skill writes both: the 7-lane summary at
  # agents/<agent>/temp/drained/felt-sense-*.md AND the index entry via journal-add.sh.
  # journal_file is AGENT-RELATIVE: the bound agent's name followed by
  # `/journal/YYYY/MM/YYYY-MM-DD.md`, carrying NO `agents/` prefix. The `{agent}`
  # placeholder form matches core/config/conventions/journal.md, which states the
  # same contract; the angle-bracket form is deliberately avoided here because
  # check-no-bare-agent-prefix.sh Class 1 has no backtick exemption (Class 2 does)
  # and reads it as a filesystem path reference — which this field VALUE is not.
  # That asymmetry is a real gate gap, filed as g-115-3496; this wording is the
  # local workaround, not the fix.
  # The daemon validator (mind_api/src/store_registry.py STORE_REGISTRY["journal"]
  # + _journal_validate) is ground truth, NOT this doc — rb-130 / g-001-53.
  # This line carried the `agents/` prefix until 2026-07-27 (g-335-311
  # felt-sense), the same drift already fixed in core/config/conventions/journal.md
  # and guarded by verify-learning Section JDV.
  # Measured while writing this sweep's own entry: an input of bare
  # `journal/YYYY/...` is ACCEPTED and NORMALIZED — it stored as
  # `zeta/journal/2026/07/2026-07-27.md`. So the bare form is not an error, but
  # the canonical stored shape carries the agent name; write it explicitly rather
  # than relying on normalization.
```

## Relationship to Existing Mechanisms

| Mechanism | Scope | Trigger | User-facing? | Writes directly? |
|-----------|-------|---------|--------------|------------------|
| `sq-012` | Single-outcome self-purpose | Post-goal | Only on material change | Yes (Self) |
| `strategic-scan` S3b | Category coverage | Autonomous (5 goals / 4h) | No | Yes (aspirations) |
| `/fresh-eyes-review` | Self + portfolio | Cadence (25 goals) | No — local audit | No — autonomous |
| `/priority-review` | Portfolio ranking | User pull | Yes, pull-only | Yes (priorities) |
| `/felt-sense-checkin` | **7-lane self-audit** | **Cadence (75 goals)** | **Notify only on material Self change** | **Yes — 6 stores + Self** |

Felt-sense is the structured sweep that writes. The others either ask,
observe narrowly, or cover a single slice. None of them run the full
"hygiene + completions + unblocks + backlog + verify + meta + felt"
check as one atomic pass.

## Chaining

- **Called by**: User (`/felt-sense-checkin`), `/aspirations-precheck`
  Phase 0.5f (`/felt-sense-checkin --cadence`)
- **Calls**: `felt-sense-cadence-check.sh`, `insights-read.sh`
  (default + `--count` + `--mark-processed` — Phase 1b curation pass),
  `aspirations-query.sh`, `aspirations-update-goal.sh`,
  `aspirations-add-goal.sh`, `reasoning-bank-add.sh`,
  `guardrails-add.sh`, `/tree add`, `experience-add.sh`, `meta-set.sh`,
  `/notify-user` (via canonical phrasing), `journal-add.sh`, `wm-set.sh`
- **Reads**: `agents/<agent>/self.md`, `core/config/verification-checklist.md`,
  meta strategy files, `agents/<agent>/session/working-memory.yaml`,
  world aspirations
- **Modifies**: `world/knowledge/tree/` (new/edited nodes),
  `world/reasoning-bank.jsonl` (appends), `world/guardrails.jsonl`
  (appends), `world/aspirations.jsonl` (status flips, new goals),
  `agents/<agent>/self.md` (material Lane 7 findings),
  `agents/<agent>/experience.jsonl` (appends),
  `agents/<agent>/insights.jsonl` (bulk `--mark-processed` in Phase 1b —
  flips `processed: true` for all unprocessed entries so the `/prime`
  Phase 4 surface resets),
  `agents/<agent>/temp/drained/felt-sense-*.md` (archival summary — value already
  written to the 6 stores during the sweep; never enters the drain queue),
  `agents/<agent>/journal.jsonl` (append),
  `agents/<agent>/session/working-memory.yaml` (last_felt_sense_checkin slot),
  `meta/*.yaml` (tuning edits), `core/config/verification-checklist.md`
  (new checks), email outbound (only on material Self change)
- **Does NOT modify**: aspiration priorities (that's /priority-review)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool
call, not text. The terminal action is the Phase 8 `journal-add.sh`
call. Never end this skill with a text summary — the summary is in
the report archive and the journal entry, the agent's job is to record
the tick and return control.
