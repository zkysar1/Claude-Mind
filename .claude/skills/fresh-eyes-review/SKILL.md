---
name: fresh-eyes-review
description: "Periodic local self-audit (cadence: every 25 goals). Assembles a portfolio-direction briefing (Self snapshot, aspiration portfolio, evolution signals, partner activity), writes it to agents/{agent}/temp/ (a staging file drained to the knowledge tree), and posts a one-line summary to the coordination board. No email push, no user-approval gate — the user reviews changes via git log and tracked signals at their own pace. Use whenever the user wants to force a portfolio review on demand (/fresh-eyes-review), or the precheck cadence triggers automatically (--cadence). Distinct from sq-012 (post-goal, narrow) and /priority-review (user-pull, ranking-only)."
user-invocable: true
triggers:
  - "/fresh-eyes-review"
  - "fresh eyes review"
  - "step back review"
tools_used: [Bash, Read, Write, Edit, Skill]
companion_scripts: [core/scripts/fresh-eyes-cadence-check.sh, core/scripts/team-belief-write.sh]
conventions: [aspirations, session-state, working-memory]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-fresh-eyes-review-b305df"
previous_revision_id: null
---

# /fresh-eyes-review — Periodic Portfolio-Direction Self-Audit

Every 25 completed goals (or on user demand), step back and produce a
portfolio-direction briefing. The ritual runs autonomously, writes the
briefing to `agents/<agent>/temp/` (a staging file drained to the knowledge
tree), and posts a one-line summary to the coordination board. No email push,
no user-approval gate.

The user reviews changes via git log and tracked signals at their own pace.
This follows the same pattern as Self evolution (guard-380, 2026-04-22):
the agent acts, the user reviews retroactively and reverts if they disagree.

## Sub-commands

```
/fresh-eyes-review                 — User-forced review, bypasses cadence gate
/fresh-eyes-review --cadence       — Check cadence; run only if gate passes
                                     (agent-invoked path from precheck)
```

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front
matter. Read only the paths returned. If output is empty, all conventions
already loaded — proceed.

## Phase 1: Cadence Gate

```
IF invoked with --cadence:
    Bash: core/scripts/fresh-eyes-cadence-check.sh
    IF exit 1: Output "Fresh-eyes: cadence not crossed — noop." → DONE (return)
    IF exit 0: proceed
ELSE (user-invoked, no --cadence flag):
    Proceed directly — user override.
```

The cadence script enforces the 25-goal threshold. User invocation
bypasses it.

## Phase 2: Briefing Assembly (read-only)

Read the inputs. Cache each result so Phase 3 can synthesize without
re-reading.

```
# 2.0 PRIOR SERIES — read this FIRST, before any instrument (g-335-420, 2026-07-29)
# directive-lane-compliance is the accumulated memory of every prior review: each
# agent's PM-First rule-1a series, the measurement recipe, and the Decision Rules
# that prior reviewers recorded so their successors would not re-derive them.
# Rule 1a REQUIRES a series with >=3 points — a single reading cannot distinguish
# "under the floor and correcting" from "under the floor and stuck", and those
# call for opposite responses.
Bash: bash core/scripts/tree-read.sh --node directive-lane-compliance
  → capture: this agent's prior series points, ALL Decision Rules, and the
    measurement recipe (which is load-bearing — see 2.2b SOURCE below)
  → carry these into Phase 3. Append this pass's point to the node's series
    table at Phase 5.6 rather than leaving it only in the temp/ briefing.

# WHY 2.0 EXISTS AND WHY IT IS FIRST. Both sibling rituals learned this the hard
# way and wrote it into their own Phase 2.0 (fresh-eyes-program reads
# program-alignment-health; fresh-eyes-tree reads l1-taxonomy-health) — a series
# node read only at encode time catches a re-derivation AFTER its full cost is
# already paid. fresh-eyes-review had NO series node at all until 2026-07-29, so
# its Self-mandated series lived only in the temp/ briefing that Phase 8 archives
# to temp/drained/ — re-derived from scratch every pass, or carried in volatile
# session context. Retrieval must precede synthesis
# (.claude/rules/retrieve-before-deciding.md).

# 2.1 Self — current identity
Read agents/<agent>/self.md
  → capture body content (after YAML front matter) and last_updated
  → compute days_since_self_updated = (today - last_updated).days

# 2.2 Aspiration portfolio — active work snapshot
Bash: load-aspirations-compact.sh
IF path returned: Read it
Extract for each active aspiration:
  - id, title, priority
  - goals: (completed / total), top 3 goal titles with status

# 2.2b STANDING USER DIRECTIVE — read BEFORE assessing alignment (g-115-3136)
# The Phase 3 "are we working on the right problems" verdict is an assessment
# of the work mix against what SHOULD be worked on. The agent's own gate (e.g.
# self.md's PM-First Gate) is only half that standard: a standing user
# directive in team-state OUTRANKS it, and an internal gate can pass cleanly
# while the mix violates the directive — because the gate does not know the
# directive exists. That is a vacuous pass, not sanction.
# Canonical incident (bravo, 2026-07-25): the briefing measured 109 asp-115 vs
# 16 asp-335 closes in 7d against the PM-First Gate alone and concluded
# "sanctioned, not drift". strategic_focus.primary said verbatim "Product goals
# outrank routine infra sweeps at selection time until asp-335 drains"
# (asp-335 was 194/233 — not drained) and its rationale said "spread the work
# across the fleet — some agents idle while others are overloaded", which is a
# verbatim description of what a partner belief had ALREADY reported about this
# agent. The wrong verdict was caught only by an unrelated duplication-gate
# refusal and had to be corrected mid-review. Enforced by guard-1428.
Bash: team-state-read.sh --field strategic_focus --json
  → IF non-null: capture primary, rationale, set_by, set_at, acknowledged_by
  → call this strategic_focus
  → for every aspiration id named in primary, note its live completion ratio
    from the 2.2 snapshot. An aspiration BELOW 1.0 means the directive is
    still LIVE for that lane; at 1.0 it has drained and self-retired.
  → Phase 3 MUST weigh the mix against this directive BEFORE any internal
    gate, and MUST state the comparison explicitly — including when the mix
    complies. "The internal gate passed" is NOT a verdict on its own.
  → MEASURING the mix: count one-off closes by `completed_date`, but count
    RECURRING closes by `lastAchievedAt` (companions: `achievedCount`,
    `currentStreak`). A recurring goal returns to `status: pending` on close,
    so it NEVER carries `status: completed` and a completed_date-only scan
    reports ZERO recurring closes — silently dropping the exact lane a
    "routine infra sweeps" directive targets, always in the direction that
    flatters compliance. There is no `last_completed` / `completion_count` /
    `last_run` field: probing those returns 0/N and reads as a framework
    defect (alpha 2026-07-26 nearly filed one — rb-245 class, a zero-count
    audit against a nonexistent field). Verify the field exists in one live
    record before reporting any zero.
  → SOURCE: measure against the FULL store —
    `aspirations-read.sh --source world --active` (and `--source agent`) — NOT
    the `aspirations-compact.json` that Phase 2.2 above tells you to load. The
    compact is a 9-field projection (category, discovered_by, filed_by_agent,
    id, participants, priority, status, title, work_class) carrying NONE of
    `completed_date` / `completed_by` / `recurring` / `lastAchievedAt`. Every
    field this measurement needs is absent, so the count is structurally ZERO
    for any agent, any lane, any window — while the aspiration-level completion
    ratios Phase 2.2 wants from the same file are perfectly correct, which is
    what makes the file look like the right source. (bravo 2026-07-29: returned
    0 closes in a session where the reviewer had personally closed ten goals.)
  → AND the one-live-record probe above must sample a record the predicate will
    ACTUALLY COUNT. Goal records are heterogeneous — a `field in record`
    membership test swept across the whole corpus can pass on a differently
    shaped record than the one being counted, returning a confident wrong yes
    and clearing the rb-245 check while the zero stands. Probe a record that
    matches the predicate's own filter (same source, same status, same
    recurring-ness), or the probe is a second way to be wrong. (sig-54.)
  → IF null/absent: no standing directive; the internal gate is the standard.
    Say so explicitly rather than silently omitting the check.

# 2.3 Self-evolution signals in pending-questions
# ⚠ THE OLD FILTER HERE WAS STRUCTURALLY DEAD, FLEET-WIDE, ON EVERY PASS. It read
# "id starts with 'sq-012' OR tags include 'self_evolution'". Measured 2026-07-31
# (bravo, cc-05) across all five agents' files — 99 records: `tags` is absent from
# the UNION of keys on every agent, and ZERO ids begin with `sq-`. No skill writes
# `tags:` into this store (0 matches across every SKILL.md). Neither disjunct could
# ever match, so pq_signals was 0 for every agent on every review — and that zero
# read as "no self-evolution signal" rather than "this step queries a retired
# surface." Same class as the Phase 2.4 defect fixed 2026-07-30 (three fields read
# from a store that never had them); found forty lines below it and missed then.
# ROOT CAUSE — not a schema mismatch, a RETIRED PROTOCOL. sq-012 used to gate Self
# edits through a pending-questions PRE-APPROVAL entry. `.claude/rules/self.md`
# records that gate as SUPERSEDED on 2026-04-22 ("the user explicitly traded 'ask
# first' for 'notify after, revert if wrong'"), enforced by guard-380. sq-012 signal
# moved to the board and to journal/self.md revisions. This step was never updated.
# WHY IT SURVIVED ~15 MONTHS OF PASSES: rb-1279 (2026-05-24) OBSERVED the symptom
# ("the count silently 0") and fixed it by ADDING 2.3b's board channel as a second
# source. That was the right adaptation — the signal genuinely had moved — but it
# made the total non-zero, which removed all pressure to ask why the FIRST source
# was still zero. A compensating second source masks a broken first one indefinitely.
# guard-1922 names the general shape: a condition whose signal is not durably
# readable retires itself silently, always as a pass. guard-1419 names the reading
# duty: a zero with two candidate explanations that imply OPPOSITE actions must be
# disambiguated before it is believed.
Read agents/<agent>/session/pending-questions.yaml
  → SCHEMA (measured, all five agents): every record carries `id, question, status,
    created`; most carry `type` (43 distinct free-text values, ~35% null) and
    `default_action`. `category` is null on 94 of 99. There is NO `tags` key and no
    id convention marking self-evolution. Re-probe before trusting any of this.
  → This store is the `self.md` Decision Authority mechanism-1 surface: decisions
    ALREADY EXECUTED, logged as "I decided X because Y — override if you disagree."
    So a self-evolution signal here is a DECISION ABOUT THIS AGENT'S OWN purpose,
    role, lane, or scope — a judgment on the text, not a key match.
  → capture such entries created within the last 30 days (any status)
  → call these pq_signals
  → EXPECT ZERO and say so explicitly. Since the 2026-04-22 supersession the
    primary sq-012 surfaces are 2.3b (board) and 2.6b (partner beliefs); an empty
    pq_signals is now the NORMAL reading, not a missing signal. What would make it
    non-empty is a logged decision that narrows or redirects this agent's purpose.

# 2.3b Self-evolution signals on the findings board (g-115-1214)
# pending-questions.yaml is not the only self-evolution surface. A self-drift
# or self_evolution finding posted to world/board/findings — by a partner
# agent, or by this agent's own strategic-scan / fresh-eyes-followup — is
# ALSO a self-evolution signal that Phase 2.3 above never read. Incident
# (2026-05-24): a no_change verdict landed with self_evolution_signals_count=0
# while alpha self-drift finding msg-20260523-091626-alpha-1586 sat unread on
# the findings board (later actioned by hand as g-115-1213). See rb-1279.
# --unread-only (g-115-2486): count only UNACTIONED signals. A signal ACTIONED
# via `board.py mark-read` (consumed into concrete work, or explicitly retired
# with a resolution note) drops out of the count so it stops re-counting as
# net-divergent residue every review within the 30d window (the stale-signal
# treadmill g-115-2486 fixed: echo ARC-frontier 06-27 + retired-charlie sq-012
# 07-04 re-counted every fresh-eyes review until marked read). Aligned with the
# g-115-1214 intent — an unread finding IS exactly an unactioned one, so
# genuinely-pending signals are still caught (line below already documented "the
# unread finding(s)"); this only aligns the board-read call with that intent.
Bash: board-read.sh --channel findings --since 30d --unread-only --json
  → filter to findings WHERE ('self_evolution' in tags OR 'self-drift' in tags)
    AND directed at this agent. **EVALUATE THE TESTS IN THIS ORDER — an
    explicit agent ROUTING TAG outranks a loose prose mention** (the same
    precedence `aspirations-select` Phase 2.07 states for directives). Taking
    (a)'s prose disjunct before (b)'s exclusion is what inflates the count:
      (a0) tags carry ANY agent name → that tag DECIDES. MIND_AGENT among them
           → directed. Another agent's name and not MIND_AGENT → it is THAT
           agent's own signal → EXCLUDE, and do not consult the text at all.
           **Match BOTH tag forms — the bare name (`alpha`) AND the qualified
           `agent:<name>` form.** Neither is documented as canonical in
           `board.md` / `coordination.md`; board tags are free-form and agents
           demonstrably write both. Measured 2026-07-31 (echo, 26 self_evolution
           /self-drift findings in 30d): 25 bare, 1 `agent:alpha`. A bare-name
           membership test silently drops the qualified form out of (a0) — the
           post then falls through to (a1)/(b), which is exactly the loose prose
           branch guard-1877 was written to keep it out of. In the measured case
           (b)'s author-check happened to exclude it anyway, so the count was
           unaffected; do not read that as the hole being harmless — it means
           the failure is invisible when it fires. Normalize the prefix before
           the membership test.
      (a1) no agent tag, and the finding's SUBJECT is this agent (a claim ABOUT
           it — not merely a row in a cross-agent comparison table, and not an
           @-broadcast mention) → directed.
      (b) the finding carries no agent tag (applies to all) AND is genuinely
          agent-agnostic — i.e. about the framework / all agents, NOT the
          author's own self. EXCLUDE a PARTNER-authored self-signal:
          author != MIND_AGENT AND the text names the AUTHOR'S OWN
          goal/purpose (e.g. "<author> - recorded for cross-signal review",
          or an sq-012 tentative on the author's OWN goal). A partner's own
          untagged sq-012-about-themselves is about the AUTHOR, not this
          reviewer — it is not a self-evolution signal for THIS agent.
          (An untagged self-signal authored by THIS agent — author ==
          MIND_AGENT, e.g. this agent's own strategic-scan / fresh-eyes
          followup — still counts, per line 91.)
          **AND THE AUTHOR-NAMES-ITSELF TEST IS NOT SUFFICIENT — add a
          post-SHAPE test.** The rule above asks whether the TEXT names the
          author's own goal/purpose, but a partner's routine cadence post
          carries its authorship in the `author` FIELD and never restates it
          in prose, so it slips (b) while being purely about that partner.
          Measured 2026-07-31 (echo N=18, 28 self_evolution/self-drift findings
          in 30d): the filter kept 5 as directed-at-echo — 4 echo-authored
          (correct, per the paragraph above) and 1 foxtrot post opening
          "Fresh-eyes 7192→7220 … Series point 4 appended", a self-signal about
          FOXTROT. Honest partner-authored count: ZERO. So ALSO exclude when
          `author != MIND_AGENT` AND the text opens with this ritual's own
          post shape (`Fresh-eyes <n>-><n>`, `sq-012 TENTATIVE`, or a
          `N=<k>` series-point line) — those are the AUTHOR's cadence record,
          regardless of whose names appear in their comparison tables. Third
          near-miss in this one step (guard-1877 family, after the (a0)
          tag-form hole fixed at N=15): each one inflates
          self_evolution_signals_count in the direction that forces act_later
          forever.
  # SECOND REGRESSION, and the reason the ORDER above is now explicit
  # (guard-1877, alpha 2026-07-29; independently replicated by bravo
  # 2026-07-30, one day later, on a different agent). This ritual's own board
  # posts now @-broadcast to every agent and carry cross-agent comparison
  # tables, so the "text names this agent" disjunct — written as a rare
  # fallback — matches essentially EVERY peer's routine cadence post. Measured
  # alpha: loose test kept 10 of 18; honest count ZERO. Measured bravo: loose
  # test kept 15 of 29; honest count ONE (27 of 29 were tagged to another
  # agent). Left unordered, this inflates self_evolution_signals_count enough
  # to force act_later on every review forever, and it GROWS — every new
  # comparison table adds another false match. guard-1877 already carried the
  # rule; the pseudocode still invited the error, which is why alpha's
  # measurement did not prevent bravo's near-miss the next day. A guardrail
  # cannot outvote the instrument it guards.
  # FIRST REGRESSION (g-115-2922, zeta review 2026-07-22): before this exclusion,
  # two echo self-signals (echo-3542, echo-3840) — echo's own untagged sq-012s
  # — were counted toward zeta's review via the "applies to all" disjunct,
  # inflating self_evolution_signals_count 5->7 and net-divergent 1.0->3.0,
  # flipping zeta's self-assess from no_change to a FALSE act_later (caught by
  # a manual authorship check, corrected by hand). The author!=MIND_AGENT +
  # names-own-purpose test is the fix; a genuinely agent-agnostic untagged
  # finding is unaffected. Verify-learning guard: a partner-authored untagged
  # sq-012 MUST NOT count toward another agent's Phase 2.3b board_signals.
  → call these board_signals
  → surface board_signals to Phase 3 "Recent self-evolution signals" bullets
    so the briefing names the unread finding(s), not just pending-questions

# 2.4 Evolution engine output — dev stage, gap analysis, novelty pressure
# The dev STAGE does NOT live in evolution-log.jsonl. Probed 2026-07-30 (bravo,
# cc-05): the live record schema is exactly {date, event, details}, and NO entry
# in the last 40 carries any *stage* key. This step used to say "capture
# current_stage, gap_analysis summary, interestingness_state" from that file —
# three fields the store has never had, so all three returned null for every
# agent on every pass, and that null reads as "the evolution engine has no
# signal" rather than "this step read the wrong file." rb-245 class: verify the
# field exists in one live record before reporting its absence.
Bash: bash core/scripts/curriculum-evaluate.sh
  → capture current_stage (+ stage_name / all_passed / terminal_stage / next_stage).
    Branch on SHAPE per the cadence-battery contract: terminal_stage:true with
    all_passed:true and gates:[] is the CORRECT end state, not a pending promotion.
Bash: tail -n 5 <META_DIR>/evolution-log.jsonl
  → parse recent entries for `event` + `details` (the fields that exist).
    `event` values seen live: strategic_scan. Gap-analysis / interestingness
    narrative, when present, is prose inside `details` — read it, do not key on it.

# 2.5 Strategic-scan portfolio health — category concentration, uncovered Self priorities
Bash: wm-read.sh portfolio_health_signal
  → capture any recent signal (category_concentration, uncovered_priorities)

# 2.6 Partner activity — the other half of the team
Bash: team-state-read.sh --json
  → capture partner.last_active, partner.current_focus, partner.live_phase, partner.session_goals_completed
  → capture recent_completions (last 5)
  → ALSO capture agent_status.<partner>.beliefs for every partner (used by 2.6b)

# 2.6b CONSUMER — partner beliefs ABOUT this agent (g-306-28, Theory-of-Mind; BRD Gap 9; OpenToM 2402.06044)
# Each agent is the SOLE writer of agent_status.<self>.beliefs, so a partner's
# sublist holds what THAT partner believes — including beliefs directed at THIS
# agent. A fresh-eyes self-audit treats those external perspectives as
# confidence- AND staleness-weighted HYPOTHESES about this agent's identity /
# drift — NEVER as ground truth, and never substituting a partner's self.md or
# aspirations for an observed belief. Canonical signal: bravo's "cross-domain
# stretch" belief about alpha, which the 2026-06-18 briefing otherwise saw only
# from alpha's OWN prior review — the team's external read was invisible until
# this step. Reuse the team-state --json already read in 2.6 (no extra daemon call).
FROM the agent_status read in 2.6:
  belief_signals = []
  FOR EACH partner != MIND_AGENT:
    FOR EACH b in agent_status.<partner>.beliefs (list may be absent/empty/null — handle gracefully):
      IF b.about == MIND_AGENT:
        staleness_days = (today - date(b.last_observed)).days
        weight = b.confidence * (1.0 if staleness_days <= 14 else 0.5)   # fresh + confident = stronger
        belief_signals.append({holder: <partner>, claim: b.belief,
                               confidence: b.confidence, staleness_days, weight})
  → surface belief_signals to Phase 3 "Recent self-evolution signals" as
    "<partner> believes (conf {confidence}, {staleness_days}d old): {claim}"
  → these are WEIGHTED hypotheses, NOT verdicts: a low-confidence or stale
    belief is a soft nudge. Do NOT auto-edit Self from them; they raise the
    Phase 5.5 self_evolution_signals_count so a fresh, high-confidence, or
    clustered external signal can tip the self-assess toward act_now/act_later.

# 2.6c WRITER — record ONE belief about the primary partner observed (g-306-28, Theory-of-Mind)
# The 25-goal fresh-eyes cadence IS the "real decision point, not every tick":
# this fires once per review. Pick the SINGLE most salient partner observation
# from the 2.6 activity read (e.g. a partner working notably outside its nominal
# lane, an unusually high/low completion count, a stalled live_phase) and record
# a calibrated single-observation belief (confidence ~0.5). team-belief-write.sh
# SUPERSEDES the prior belief about that partner (one-per-partner, hard cap 10 —
# no unbounded growth) and is lock-safe via the daemon. Skip SILENTLY if no
# partner observation rises above noise this window — a belief must be grounded
# in observed activity, NEVER fabricated to satisfy the step (communication-clarity
# rule 6).
IF a salient partner observation exists in the 2.6 activity read:
  Bash: team-belief-write.sh --about <partner> \
        --belief "<one-line observed claim, grounded in current_focus / completions / live_phase>" \
        --confidence 0.5 \
        [--domain "<partner.current_focus from the 2.6 read>"]
  → confirm stdout "Updated agent_status.<self>.beliefs"
  # --domain (g-306-29) is the OPTIONAL structured focus-domain this belief
  # asserts the partner is working in — pass the partner's observed
  # current_focus value VERBATIM when the belief is genuinely about WHICH
  # DOMAIN the partner is working (the common case). It makes the belief
  # contradiction-checkable: aspirations-precheck Phase 0-pre.0a later compares
  # the partner's FRESH current_focus against this recorded domain and, on a
  # sustained mismatch, forces a belief revision. OMIT --domain when the
  # observation is not domain-shaped (e.g. an unusually high/low completion
  # count, a stalled live_phase) — those beliefs stay free-form and the
  # contradiction detector conservatively skips them.

# 2.7 Goal-count context — how much work backs this review
Bash: fresh-eyes-cadence-check.sh --verbose
  → capture current goals-completed count, last-fire count, diff
```

## Phase 3: Synthesis

Build the briefing text (plain Markdown, no external links) with sections:

```markdown
# Fresh-eyes review — {today ISO date}

{One paragraph: N goals completed since last review (or "first review").
Where the portfolio has moved. Which aspirations finished. What the
agent's been working on most. Two sentences, not five.}

## Self snapshot (informational)

Self evolves autonomously via sq-012 (post-notification per guard-380). Shown
here so you can revert via direct edit or `/respond` if you disagree — no
answer needed. The autonomous evolution path will not be gated on your
response to this section.

Current Self (last updated {N} days ago):

> {self.md body, full text — inline, no link}

Recent self-evolution signals (FYI):
- {Evidence-backed bullet — e.g., "sq-012 flagged 'core purpose may
  be narrowing' in pq-NNN (2026-04-NN)."}
- {Evidence-backed bullet — dev-stage / gap analysis signal, if any}
- {Partner-belief bullet from Phase 2.6b belief_signals, if any — e.g.,
  "bravo believes (conf 0.5, 4d old): alpha may be on a cross-domain stretch."
  State it as a weighted external hypothesis, not a verdict.}

## Are we working on the right problems?

Active aspirations ({N} total):
| ID | Title | Priority | Progress |
|----|-------|----------|----------|
| asp-NNN | {title} | HIGH/MED/LOW | N/M goals |

Observations:
- {Category concentration finding — e.g., "70% of recent goals in
  infrastructure; 0 in primary-domain despite Self emphasis."}
- {Completion-health finding — e.g., "3 aspirations above 80%
  completion; 2 below 20% for >5 sessions."}
- {Partner signal — e.g., "<partner-agent> created 4 review goals this
  window; <this-agent> executed 3."}

Candidate portfolio rebalances (if any):
- {Priority shift proposal with rationale}

## Portfolio assessment

Are we working on the right problems — is the portfolio still aligned with
the Self?

{Agent's own assessment based on Phase 5.5 decision: act_now / act_later /
no_change, with rationale. No user response requested.}
```

All observations must follow `.claude/rules/communication-clarity.md` rule 6:
state what the evidence shows, do not hedge. If evidence is ambiguous, say
"the evidence shows X but does not show Y."

## Phase 4: Stage Briefing to temp/

Write the briefing body to `agents/<agent>/temp/fresh-eyes-{YYYY-MM-DDTHH-MM-SS}.md`
as a staging artifact — its durable findings are encoded to the knowledge tree
by Phase 5.6, after which Phase 8 Step 1.5 archives the file to `temp/drained/`.
The briefing therefore never enters the `/drain-temp` queue as already-encoded
slush (g-115-1838; the drain would only DISCARD it anyway, so the
staging→drain→DISCARD round-trip inflates the precheck temp-pressure metric for
nothing — see `core/config/conventions/temp-store.md`). Timestamp includes
HH-MM-SS so multiple same-day invocations (cadence fire + user-forced review) do
not collide.

```
Bash: mkdir -p agents/<agent>/temp
Write the briefing body (from Phase 3) to agents/<agent>/temp/fresh-eyes-{today-isotime}.md
  (where {today-isotime} = `date +%Y-%m-%dT%H-%M-%S` — colons replaced with
   hyphens for Windows filesystem compatibility)
```

## Phase 5.5: Self-Assess Decision

Classify the review outcome via the deterministic helper and act on it.
No escalation to the user — the agent decides and proceeds autonomously.

Extract signals from the Phase 3 briefing synthesis (scored 0..1 unless
noted) and pass to the helper:

```
# Build signals JSON from Phase 3 briefing content
SIGNALS_JSON='{
  "portfolio_drift_score":          {0..1 — degree the portfolio has drifted from Self emphasis since last review},
  "completion_health":              {0..1 — average completion ratio across active aspirations},
  "self_evolution_signals_count":   {int — count of recent self-evolution indicators in last 30d = len(pq_signals from Phase 2.3) + len(board_signals from Phase 2.3b, g-115-1214) + len(belief_signals from Phase 2.6b, g-306-28). A partner's belief ABOUT this agent (e.g. bravo's "cross-domain stretch" read of alpha) is an external self-evolution signal even when pending-questions.yaml AND the findings board are both empty — it was invisible before g-306-28},
  "confirming_signal_fraction":     {0..1 — fraction of the counted self_evolution signals that CONFIRM the current lane, so the act_later gate fires on NET-DIVERGENT signal not gross volume (this PULLS the g-115-1680 lever self-assess-and-decide.sh already has but that Phase 5.5 was leaving at the 0.0 default). Compute = confirming_beliefs / self_evolution_signals_count, where a belief_signal (Phase 2.6b) is CONFIRMING if STALE (staleness_days > 14 — an aged partner read is not current drift-pressure) OR AFFIRMING (its claim matches this agent's current Self focus + active-aspiration lane); DIVERGENT only when FRESH AND its claim suggests drift/contradiction from current Self. pq_signals + board_signals are genuine change-indicators (never confirming). Emit 0.0 only when self_evolution_signals_count == 0. WHY (g-115-1742, alpha 2026-07-03): an affirming partner-belief — a correct external read of a stable, in-lane Self — is STABILITY evidence, not change-pressure; counting it toward act_later was a false-positive treadmill (evo=5 = 3 fresh-affirming + 2 stale, self 2d fresh, portfolio in-lane → wrongly returned act_later, re-filing a follow-up Idea every review that partners correctly read this agent)},
  "self_last_updated_days":         {int — days_since_self_updated from Phase 2.1},
  "explicit_user_directive":        {true|false — outstanding /respond about purpose or portfolio},
  "signal_actionable_score":        {0..1 — how clearly the signals map to a specific Self edit}
}'
Bash: echo "$SIGNALS_JSON" | bash core/scripts/self-assess-and-decide.sh --review-type fresh-eyes-review
  → capture decision, rationale, recommended_action from JSON output
```

Branch on decision:

- **`act_now`** — apply the Self edit inline via the existing autonomous
  edit path (Edit tool on `agents/<agent>/self.md`). The Phase 2 hooks
  (`evolution-prepare.sh` → `evolution-record.sh`) capture the change as
  a stub; invoke `bash core/scripts/evolution-complete.sh --revision-id
  <stub-rev> --reasoning "<≥80-char rationale citing fresh-eyes briefing
  signals + Phase 3 evidence>" --signal-source fresh-eyes-review` to
  finalize. Material classification triggers `guard-380` post-notification
  via journal only (no email).
- **`act_later`** — file an Idea goal under `asp-115` (the recurring
  infrastructure aspiration that catches cross-system follow-ups):
  `bash core/scripts/aspirations-add-goal.sh asp-115` with stdin
  `{"title":"Idea: <one-line summary>","priority":"MEDIUM",
  "category":"self-evolution",
  "origin_signal":"idea:fresh-eyes-followup",
  "description":"<copy briefing observations + recommended_action>"}`.
- **`no_change`** — silent no-op. Phase 8 cadence stamp still fires so
  counter resets to next window.

ALWAYS log the decision to `agents/<agent>/journal` (one-line tagged
`fresh-eyes-decision`) and append a single board post to the `reasoning`
channel summarizing decision + rationale. The audit trail is the
guardrail's evidence path.

## Phase 5.6: Encode Non-Routed Observations

Phase 5.5 routes at most ONE finding to a durable home (a Self edit via
`act_now`, or an Idea goal via `act_later`). Every OTHER observation in the
Phase 3 briefing — category-distribution evidence, completion-health
patterns, partner-activity signals, self-evolution-signal aggregations,
candidate-rebalance rationale — otherwise lands ONLY in the transient `temp/`
staging file, which is invisible to `/prime` and `retrieve.sh` and is drained
away over time. This step encodes the observations that have no other durable
home, so the briefing's knowledge survives after the staging file is drained.
Modeled on
`/felt-sense-checkin` Phase 1.

**No-double-encode**: skip the single observation Phase 5.5 already routed
(the `act_now` Self-edit target, or the `act_later` goal's
`recommended_action`). The journal/board decision entries carry the decision
label, not the observations — they are not duplicates.

For each REMAINING briefing observation, classify per
`core/config/conventions/learning-routing.md` and route to ONE store. When in
doubt, drop — the asymmetry favors dropping (over-encoding inflates retrieval
cost forever; under-encoding loses one signal once):

- **tree** — a compressed durable fact (category-distribution ratio,
  completion-health pattern, cross-agent work dynamic). **Novelty gate
  (mandatory — fresh-eyes fires every 25 goals; un-gated `/tree add` floods
  the tree):** before adding, check whether a node already covers this
  observation (`tree-read.sh --node {candidate-key}`, or a `retrieve.sh`
  lookup). If one exists and this is only a refreshed measurement, `/tree
  edit` it (update body + `last_updated` + `last_update_trigger:
  fresh-eyes-review`) instead of adding a duplicate. Use `/tree add {parent}
  {key} {summary}` ONLY for a genuinely novel finding.
- **reasoning_bank** — a recurring diagnostic / ABC pattern.
  `reasoning-bank-add.sh` with summary + ABC chain + `applies_to`
  (`framework` for multi-agent / portfolio patterns, else `any`).
- **guardrails** — a prescriptive rule with a trigger condition.
  `guardrails-add.sh` with rule + trigger_condition.
- **drop** — already captured, too thin, or a one-cycle anomaly.

The encoding writes are self-evidencing (the new/edited tree, reasoning-bank,
and guardrail records); no separate log line is required. Do NOT add a
terminal action here — Phase 8's board-post remains the skill's final tool
call.

## Phase 8: Record the Tick

Update the WM slot so the cadence gate stops firing until 25 more goals
have completed.

**Critical invariant**: the stamp write is LOAD-BEARING. The cadence gate
reads `last_fresh_eyes_review` to decide whether to fire again. If this
step silently fails, the gate re-fires every iteration (see g-240-60 —
fresh-eyes-2026-04-20 was resolved but left the slot null, so the next
iteration's precheck said "fire!" and would have re-fired 45 min later
when there was still nothing new to review). Do NOT chain the stamp
write into the board-post with `&&` — a failing board-post MUST NOT
eat the stamp write.

### Step 1: Record the stamp (LOAD-BEARING — never skip)

```
Bash: bash core/scripts/fresh-eyes-record-tick.sh
```

This wrapper reads the current completed-goals count via
`fresh-eyes-cadence-check.sh --print-current`, writes the
`last_fresh_eyes_review` WM slot atomically, and verifies the slot is
non-null after the write (fails exit 1 on silent write failure). One
script call, one failure mode — no chaining.

### Step 1.5: Archive the briefing out of the drain queue (g-115-1838)

The briefing's durable value is fully extracted by now — Phase 5.5 routed one
finding (a Self edit via `act_now` or an Idea goal via `act_later`) and Phase 5.6
encoded every other observation — so the staging `.md` is a pure archival record.
Move it into `temp/drained/` so it never inflates the precheck temp-pressure
metric as already-encoded slush (`/drain-temp` would only DISCARD it). Placing
this AFTER Phase 5.6 keeps the interruption case no worse than before: if the
skill dies before this step, the briefing simply stays in `temp/` for the next
drain, exactly as today.

```
Bash: mkdir -p agents/<agent>/temp/drained && mv agents/<agent>/temp/fresh-eyes-{the-Phase-4-isotime}.md agents/<agent>/temp/drained/ 2>/dev/null || true
```

Use the exact filename written in Phase 4. This is a bookkeeping move, NOT the
terminal action — Step 2's board-post remains the skill's final tool call.

### Step 2: Post to board (best-effort, must not block)

```
Bash: echo "Fresh-eyes review completed; briefing archived. Decision: {decision from Phase 5.5}." | bash core/scripts/board-post.sh --channel general --type status --tags fresh-eyes-review || true
```

The `|| true` ensures board-post failure (board file locked, quota
issue, etc.) does NOT propagate back through the skill and does NOT
affect the already-completed stamp write. Board-post is
cross-agent-visibility nice-to-have, not load-bearing.

The board-post is the terminal action — per Return Protocol requirements,
the skill does NOT end with text output.

## Chaining

- **Called by**: User (`/fresh-eyes-review`), `/aspirations-precheck`
  Phase 0.5e (`/fresh-eyes-review --cadence`)
- **Calls**: `fresh-eyes-cadence-check.sh`, `load-aspirations-compact.sh`,
  `wm-read.sh`, `wm-set.sh`, `team-state-read.sh`, `team-belief-write.sh`
  (Phase 2.6c writer), `self-assess-and-decide.sh`, `journal-add.sh`,
  `board-post.sh`, `/tree add`, `/tree edit`, `tree-read.sh`,
  `reasoning-bank-add.sh`, `guardrails-add.sh` (Phase 5.6 encoding)
- **Reads**: `agents/<agent>/self.md`, `agents/<agent>/session/pending-questions.yaml`,
  `<meta>/evolution-log.jsonl`, world aspirations compact,
  `agents/<agent>/session/working-memory.yaml`,
  `world/team-state.yaml` `agent_status.<partner>.beliefs` (Phase 2.6b consumer)
- **Modifies**: `agents/<agent>/temp/drained/fresh-eyes-*.md` (briefing, archived
  there at Phase 8 Step 1.5 after value extraction — never enters the drain queue),
  `agents/<agent>/session/working-memory.yaml` (update last_fresh_eyes_review slot),
  `agents/<agent>/journal.jsonl` (append), board `general` channel (best-effort),
  `world/team-state.yaml` `agent_status.<self>.beliefs` (Phase 2.6c writer, supersede-or-cap),
  `world/knowledge/tree/` (Phase 5.6 new/edited nodes),
  `world/reasoning-bank.jsonl` (Phase 5.6 appends),
  `world/guardrails.jsonl` (Phase 5.6 appends)
- **Does NOT modify**: `agents/<agent>/self.md` (unless Phase 5.5 returns act_now),
  aspiration priorities, pending-questions. No email is sent.

## Relationship to Existing Mechanisms

| Mechanism | Scope | Trigger | User-facing? |
|-----------|-------|---------|--------------|
| `sq-012` | Single-outcome self-purpose check | Post-goal | Only for significant changes |
| `aspirations-strategic-scan` S3b | Portfolio category coverage | Autonomous cadence (5 goals / 4h) | No |
| `aspirations-evolve` | Portfolio gap + dev-stage tuning | Autonomous cadence (15 goals / 12h) | No |
| `/priority-review` | Portfolio ranking | User pull | Yes, but pull-only |
| `/fresh-eyes-review` | **Portfolio direction** | **Goal-cadence (25 goals)** | **No — local audit** |

Fresh-eyes is the periodic portfolio-direction self-audit. It does NOT
replace any of the above — sq-012 keeps catching narrow per-goal purpose
drift (and updates Self autonomously per guard-380), strategic-scan keeps
watching category concentration autonomously, evolution keeps running gap
analysis, priority-review stays the user's anytime portfolio pull.
Fresh-eyes produces a local briefing artifact and board post — the user
reviews via git log and tracked signals at their own pace.

## Restricted Operations

This skill writes one live datastore: **partner-belief entries in
`world/team-state.yaml`**, exclusively via the `team-belief-write.sh`
companion (Phase 5.6 / Phase 8). The write boundary:

- **Canonical writer is the daemon.** `team-belief-write.sh` composes the
  read + set daemon wrappers around the supersede/cap compute in
  `_team_belief.py`; the skill MUST NOT edit `world/team-state.yaml`
  directly (Write/Edit/echo), which would bypass the shared team-state lock
  and the supersede-not-grow hygiene.
- **Each agent is the sole writer of its OWN belief sublist**
  (`agent_status.<self>.beliefs`). This skill never writes another agent's
  sublist — cross-agent signalling goes through `world/board/` per
  `core/config/conventions/coordination.md`.
- **Supersede, do not grow.** A new belief about a partner replaces the
  prior one for that partner (bounded list); the hygiene lives in
  `_team_belief.py`, not in this skill's pseudocode.

No other datastore is mutated — the briefing artifact and board post are the
skill's only other outputs, each through its own canonical path.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call,
not text. The terminal action is the Phase 8 board-post Bash call.
Never end this skill with a text summary of the briefing — the briefing
is in the archive, the agent's job is to record the tick and return control.
