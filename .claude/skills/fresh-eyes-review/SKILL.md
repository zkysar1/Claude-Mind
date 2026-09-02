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
# ⚠ `tree-read.sh --node` RETURNS METADATA ONLY — summary, child_count,
# retrieval_count, poignancy. NO BODY, at any depth. Running it and reading its
# output IS NOT reading the series: this step is satisfiable exactly as written
# while leaving you with no prior series at all (`guard-3312`, which has caught
# FOUR consecutive passes — including two that followed the instruction). A
# guardrail cannot outvote the instrument it guards (guard-1984), so the paths
# are named HERE.
#
# Read the FILES, in this order. `world/` is external and bare Bash path args are
# NOT hook-rewritten (path-resolution.md), so $WORLD_PATH must be resolved INSIDE
# EACH INVOCATION — shell state does NOT persist between Bash calls, and a
# `source _paths.sh` on its own line leaves $WORLD_PATH EMPTY in the next one,
# delivering nothing. It can also read rc=0 when piped (guard-1150) — judge by
# the OUTPUT, never the exit status.
  # (a) YOUR OWN SERIES ROW — two levels down, and the ONLY place the readings live.
  #     ⚠ NO FLEET-WIDE "TOP" OR "TAIL": the shards diverged and get restructured
  #     without announcement, so DERIVE N AS THE MAX over the three shapes the probe
  #     below reads. Exclude forward-reference headings ("Handoff to N=k") CASE-
  #     INSENSITIVELY (`-vi`) — they name an entry that does not exist yet
  #     (guard-2653, guard-1922, guard-3487). Dated per-shard readings:
  #     `core/config/fresh-eyes-shard-readings.md` — APPEND THERE, never here (this
  #     file is over its injection ceiling; g-115-6690).
  #     ⚠ THE PROBE'S THREE BRANCHES ARE LOAD-BEARING AND MUST STAY BYTE-IDENTICAL.
  #     Do NOT "simplify" them into a single union regex, do NOT drop the `-vi`, and do
  #     NOT take the max WITHIN a row (branch 3 takes the FIRST `N=` per row). Each of
  #     those returns a wrong-but-WELL-FORMED N, which reads as plausible rather than as
  #     an error. Every one is measured, with the numbers and the shards it broke, in
  #     `core/config/rationale/fresh-eyes-series-index-probe.md` — read it BEFORE
  #     touching the probe.
  #     ⚠ READ THE AUTHORITATIVE STORE COPY, NOT $WORLD_PATH — AND RE-RUN THIS PROBE
  #     IMMEDIATELY BEFORE THE PHASE-8 WRITE (g-115-8055). Two independent defects, and
  #     fixing only the first leaves the collision intact: SOURCE ($WORLD_PATH is a
  #     read-through cache — guard-157) and SHELF LIFE, the load-bearing half (the PUT
  #     fence proves no LOST UPDATE and says NOTHING about whether the allocated VALUE
  #     is unique, so two boxes can mint the SAME N while every drift and integrity
  #     probe reports [match] — guard-5322, guard-1876). SO: allocate at WRITE time.
  #     Re-run this as the last step before writing the section heading and use THAT
  #     max+1; if the value moved, a peer allocated in the gap — take the new max.
  #     ⚠ FAIL LOUD, NEVER FALL BACK TO THE MIRROR. A failed authoritative read means N
  #     is unallocatable this pass; silently re-reading $WORLD_PATH restores the SOURCE
  #     defect at precisely the moment it is most likely to bite.
  Bash: source core/scripts/_paths.sh && P="world/knowledge/tree/system/directive-lane-compliance/directive-lane-series-$MIND_AGENT.md"; S="$(mktemp)"; bash core/scripts/backend-cat.sh cat "$P" > "$S" 2>/dev/null || { echo "FATAL: authoritative read of $P failed — N is UNALLOCATABLE this pass. Do NOT fall back to \$WORLD_PATH (g-115-8055)."; rm -f "$S"; exit 1; }; test -s "$S" || { echo "FATAL: authoritative read returned 0 bytes — refusing to allocate N from an empty file."; rm -f "$S"; exit 1; }; { grep -E '^#{1,4} ' "$S" | grep -viE 'handoff to N=' | grep -oE 'N=[0-9]+'; grep -oE '^\| \*\*N=[0-9]+' "$S"; grep -viE 'handoff to N=' "$S" | grep -E '^\|' | sed -nE 's/^[^N]*(N=[0-9]+).*/\1/p'; } | grep -oE '[0-9]+' | sort -n | tail -1; rm -f "$S"
  #     ⚠ POSITIVE-CONTROL WHATEVER THIS PROBE RETURNS (guard-2421) against a shard
  #     whose N you have confirmed FROM THE ROWS — and NOT against the shard-index
  #     table, a hand-maintained prose cell with no writer and no check that produced a
  #     live off-by-three. A wrong index is embarrassing; the real cost is the wrong
  #     PRIOR POINT it carries into Decision Rule 11 — a wrong drift score and therefore
  #     a wrong verdict.
  #     Then read the section around that heading/row.
  # (b) The parent's Decision Rules + measurement recipe (load-bearing — see 2.2b
  #     SOURCE below). The parent is over the Read cap, so grep its headers and
  #     sed the ranges you need; do NOT Read it whole.
  Bash: source core/scripts/_paths.sh && grep -n '^#\{2,4\} ' "$WORLD_PATH/knowledge/tree/system/directive-lane-compliance.md"
  # (c) OPTIONAL index only: tree-read.sh --node directive-lane-compliance. Useful
  #     for child_count / confidence. Never a substitute for (a).
  → **N COMES FROM (a)'s MAX SECTION HEADING, NEVER FROM THE CADENCE GOAL COUNT.**
    (Said "TOP HEADING" until 2026-08-12 — correct only for a newest-first shard;
    see the divergence measurement in (a) above.) The
    cadence diff (`current - last`, typically ~25-30) is NOT the series index and
    the two are never close. Both N=57 and N=65 drafted entire briefings numbered
    with the diff (32 and 27, against true 57 and 65).
  → **RE-DERIVE THE PRIOR POINT'S Phase 5.5 INPUTS from (a) before scoring your
    own** (Decision Rule 11). This is the half that bites hardest: at N=65 the
    prior fire had scored drift 0.45 → `act_later`, and this pass — four hours
    later on the same box, after EVERY measured window had fallen — first scored
    drift 0.35 → `no_change`. A wrong INDEX is embarrassing; a wrong VERDICT is
    the actual cost, and only the prior point's numbers expose it.
  → carry these into Phase 3. Append this pass's point to the series table in
    (a) at Phase 5.6 rather than leaving it only in the temp/ briefing.

# WHY 2.0 EXISTS AND WHY IT IS FIRST: a series node read only at encode time
# catches a re-derivation AFTER its full cost is already paid. Retrieval must
# precede synthesis (.claude/rules/retrieve-before-deciding.md). Rationale:
# core/config/rationale/fresh-eyes-series-index-probe.md

# 2.1 Self — current identity
Read agents/<agent>/self.md
  → capture body content (after YAML front matter) and last_updated
  → compute days_since_self_updated = (today - last_updated).days

# 2.2 Aspiration portfolio — active work snapshot
Bash: load-aspirations-compact.sh
IF path returned: Read it
Extract for each active aspiration:
  - id, title, priority
  - goals: (completed / total) — READ `progress.completed_goals` /
    `progress.total_goals`, the aspiration-level field already in this file.
    **Do NOT count `status == "completed"` in the record's `goals` array.** That
    array holds ONLY NON-TERMINAL goals — measured across all 22 active
    aspirations, the `completed` bucket is absent ENTIRELY from its status
    histogram. So the count is a structural ZERO for every aspiration, every
    lane, every fire, and the `completion_health` derived from it is **0.0000**
    — maximally unhealthy, the direction that forces act_now/act_later. Positive
    control, same file, same run: asp-335 reads **0/122** from the goals array
    and **989/1106** from `progress`. Same defect class as 2.2b/2.3/2.4 below (a
    step reading a field its store does not carry), and it fails as a plausible
    SIGNAL rather than as an error. `guard-3410` carried this rule and did not
    prevent it — a guardrail cannot outvote the instrument it guards
    (`guard-1984`), which is why the correction is written HERE. `progress` is
    also cheaper than the second store its `action_hint` routes you to, and
    reconciles exactly on `completed_goals`.
  - top 3 goal titles with status

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
    compact omits `completed_date` and `completed_by` entirely, so the count is
    structurally ZERO for any agent, any lane, any window — while the
    aspiration-level completion
    ratios Phase 2.2 wants from the same file are perfectly correct, which is
    what makes the file look like the right source. (bravo 2026-07-29: returned
    0 closes in a session where the reviewer had personally closed ten goals.)
  → THE REASON IS STRONGER THAN A MISSING FIELD, and the earlier wording here
    got it wrong in a way that invites the wrong fix (corrected g-115-3622,
    alpha, cc-07, 2026-08-11 — measured on a live 544-goal compact). Two
    corrections. FIRST: the compact DOES carry `recurring` (83 goals),
    `lastAchievedAt` (76) and `achievedCount` (76) — this block used to list all
    four fields as absent, which is false for three of them, and it is precisely
    WHY the recurring half of the measurement above works while the one-off half
    does not. SECOND, and the load-bearing part: the compact holds **ZERO
    completed goals at all**. A completed one-off goal is not a record missing a
    date field — it is ABSENT from the projection entirely, because the compact
    carries non-terminal goals. Recurring goals stay countable only because a
    recurring goal returns to `pending` and never leaves.
    So do NOT "fix" this by adding `completed_date` to the compact: no field
    addition can count records the projection does not contain. Reading the FULL
    store is the only remedy, which is what this SOURCE line already prescribes.
  → AND the one-live-record probe above must sample a record the predicate will
    ACTUALLY COUNT. Goal records are heterogeneous — a `field in record`
    membership test swept across the whole corpus can pass on a differently
    shaped record than the one being counted, returning a confident wrong yes
    and clearing the rb-245 check while the zero stands. Probe a record that
    matches the predicate's own filter (same source, same status, same
    recurring-ness), or the probe is a second way to be wrong. (sig-54.)
  → RULE 16 SUBSTITUTION POPULATION (g-115-4865): when the parent's Decision
    Rule 16 (guard-2424) substitution measurement runs — scoring what REPLACED
    a drained or fallen lane against Self's PRIMARY mandate — compute it
    through `world/scripts/directive-lane-share.py` and report BOTH splits it
    prints: the aspiration-id split AND the work_class split (the "by
    work_class" table). **The BARE call prints both — `--lane` and
    `--work-class` do NOT select a split.** They are value-taking
    CONFIGURATION flags (`--lane <comma-ids>`, `--work-class <kind>`, default
    `product`); a bare `--lane` exits 2 with "expected one argument". This
    line named them in backticks beside each split until 2026-08-15, which
    reads as the invocation for that split and cost N=53 a turn — the same
    correct-comment-beside-a-copyable-wrong-line shape `/felt-sense-checkin`
    Phase 8 fixed in its own `journal-add.sh` call line on 2026-08-11. An
    aspiration id is a FILING LOCATION, not a work kind: measured at alpha
    N=33 (48h window, n=38 one-off batch-filtered), the on-mandate share read
    7.9% by aspiration id and 36.8% by work_class — a 28.9pp gap, one-signed
    toward INDICTING the agent, because server/backend product work filed
    under asp-115 is invisible as product work to the aspiration-id split
    (checked by title, not inferred: 9 of the 11 asp-115 product-classified
    closes were exactly the Self-mandate lane). STATE EXPLICITLY which
    population the verdict is scored over; a verdict that names no population
    inherits the condemning default, and the series shards are scored by
    aspiration id up to alpha N=71 (see the shard head note).
  → IF null/absent: no standing directive; the internal gate is the standard.
    Say so explicitly rather than silently omitting the check.

# 2.3 Self-evolution signals in pending-questions
# ⚠ THE OLD FILTER HERE WAS STRUCTURALLY DEAD, FLEET-WIDE, ON EVERY PASS — it read
# "id starts with 'sq-012' OR tags include 'self_evolution'", and measured across all
# five agents (99 records) NEITHER disjunct can ever match: no `tags` key exists in the
# store and zero ids begin with `sq-`. The zero read as "no signal" rather than "this
# step queries a retired surface". ROOT CAUSE is a RETIRED PROTOCOL, not a schema
# mismatch: sq-012's pending-questions PRE-APPROVAL gate was SUPERSEDED 2026-04-22
# (.claude/rules/self.md, guard-380 — "ask first" traded for "notify after, revert if
# wrong"); the signal moved to the board and to journal/self.md revisions, and this step
# was never updated.
# WHY IT SURVIVED ~15 MONTHS: rb-1279 fixed the SYMPTOM by adding 2.3b's board
# channel as a second source, which made the total non-zero and removed all pressure
# to ask why the FIRST source was still zero — a compensating second source masks a
# broken first one indefinitely. Shape: guard-1922 (a signal that is not durably
# readable retires itself silently, always as a pass); reading duty: guard-1419.
Read agents/<agent>/session/pending-questions.yaml
  → SCHEMA (measured, all five agents): every record carries `id, question, status,
    created`; most carry `type` (43 distinct free-text values, ~35% null) and
    `default_action`. `category` is null on 94 of 99. There is NO `tags` key and no
    id convention marking self-evolution. Re-probe before trusting any of this.
  → This store is the `self.md` Decision Authority mechanism-1 surface: decisions
    ALREADY EXECUTED, logged as "I decided X because Y — override if you disagree."
    So a self-evolution signal here is a DECISION ABOUT THIS AGENT'S OWN purpose,
    role, lane, or scope — a judgment on the text, not a key match.
  → ⚠ BUT "a judgment on the text" IS NOT "a judgment on the SUBJECT MATTER".
    Classify on the record's `type` FIELD and the ACTION IT PROPOSES, BEFORE any
    subject-matter judgment: `type: scope-decision`, or text proposing an edit to
    `agents/<agent>/self.md`, IS a pq_signal — it does not stop being an identity
    signal because a product repo/goal/PR occasioned it, since a scope question only
    ever arises WHILE DOING WORK. Reading it the other way produced 23 consecutive
    false P=0 readings (measurement + direction-of-defect: guard-5433).
  → capture such entries created within the last 30 days, EXCLUDING any whose
    `status` is already terminal (`resolved`/`answered`/`superseded`) — a CONSUMED
    signal is not change-pressure. This is 2.3b's `--unread-only` (g-115-2486) on
    this surface; without it an actioned decision re-counts toward P every fire for
    its full 30d window. Measured 2026-09-01 (echo, cc-03, N=121):
    `pq-echo-ayoai-public-web-app-scope`, consumed by N=119 (self.md rev 0027), was
    still the ONLY pq_signal — P=1 not 0, and that single stale count DECIDED the
    verdict (evo=5/conf=0.60 → net 2.0000 `act_later`; at P=0, evo=4/conf=0.75 →
    net 1.0 `no_change`). Predicted at N=120, confirmed live at N=121.
    ⚠ FILTER ON CONSUMPTION, NEVER ON SUBJECT MATTER — guard-5433's 23 consecutive
    false P=0 readings are the OPPOSITE error (rejecting identity signals because a
    product occasioned them). A terminal-status test is orthogonal to that: it drops
    what was already ACTED ON, whatever the signal is about.
  → call these pq_signals
  → EXPECT ZERO and say so explicitly. Since the 2026-04-22 supersession the
    primary sq-012 surfaces are 2.3b (board) and 2.6b (partner beliefs); an empty
    pq_signals is now the NORMAL reading, not a missing signal. What would make it
    non-empty is a logged decision that narrows or redirects this agent's purpose.
    ⚠ "NORMAL" is a PRIOR, not a finding: run the type-field test above and name the
    records you rejected before recording a zero.

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
  → RECORD SHAPE (measured 2026-08-01, bravo N=20, cc-05). Output is JSONL — one
    object per LINE, not a JSON array; `json.load` on the whole stream raises
    "Extra data". Keys are exactly:
    `author, channel, id, reply_to, session_id, tags, text, timestamp, type`.
    **The body field is `text`.** Every rule below says "whose text opens with"
    and none names the key, so an implementation reading `content` / `body` /
    `message` gets `""` for every record — `(a-pre)`, the most consequential
    filter in this step, then matches NOTHING and drops silently out of the
    pipeline. Measured same corpus, same run, only the key differing: **0 receipts
    dropped on the wrong key, 28 on `text`.** No error and no empty result to
    notice; the count just comes back inflated by 28, which forces a false
    `act_later` forever.
  → filter to findings WHERE ('self_evolution' in tags OR 'self-drift' in tags)
    AND directed at this agent. **EVALUATE THE TESTS IN THIS ORDER — an
    explicit agent ROUTING TAG outranks a loose prose mention** (the same
    precedence `aspirations-select` Phase 2.07 states for directives). Taking
    (a)'s prose disjunct before (b)'s exclusion is what inflates the count:
      (a-pre) **CADENCE-RECEIPT EXCLUSION — runs FIRST, before any tag or author
           test, and applies REGARDLESS of author.** Drop any finding whose text
           opens with this ritual's own post shape: `Fresh-eyes <n>-><n>`,
           `Fresh-eyes N=<k>`, `sq-012 TENTATIVE`, or a bare `N=<k>` series-point
           line. Phase 8 Step 2 REQUIRES every fire to post a status tagged
           `self_evolution` + the author's own name, so these are mandated
           receipts, not signals.
           **MATCH THESE CASE-INSENSITIVELY, AND MATCH THE `-review`/`-code`/
           `-tree`/`-program` SUFFIXED FORMS TOO — the literals above are written
           in one casing and the fleet writes at least thirteen.**
           **THE SUFFIX IS OPTIONAL — ANCHOR ON THE OPENING TOKEN, NOT ON A
           SUFFIXED FORM.** Copyable predicate, IGNORECASE, matched against the
           START of `text`:
           `^\s*(?:⚠\s*)?(?:fresh[- ]eyes\b|sq-012\s+tentative\b|n=\d+\b|correction\b[^\n]{0,80}?(?:fresh[- ]eyes|n=\d+))`.
           **The `correction…` alternative and the leading `⚠` are LOAD-BEARING —
           a CORRECTION to a ritual post is a second receipt for the same fire.**
           The shapes above all describe a ritual post's OPENING token, so a
           correction that opens "CORRECTION to my own Fresh-eyes N=57 post …"
           matches none of them and survives as a signal — the ritual then reads
           its own erratum as external change-pressure, one extra count per
           corrected fire. This is the echo-N=19 finding measured verbatim ("one
           sq-012 self-signal counted twice because its own 10-minute correction
           is a separate post"), which was recorded there and never encoded in
           the predicate. Measured 2026-08-16 (bravo N=58, `hostname` cc-05,
           `uname -r` 6.8.0-137-generic) over all 97 self_evolution/self-drift
           findings unread in 30d: survivors 19 → 17, dropping exactly two, BOTH
           ritual corrections — `msg-20260816-165514-bravo-5085` (mine) and
           `msg-20260729-170301-foxtrot-5015` (foxtrot's N=7 correction, 18 days
           earlier), so this is fleet-wide and long-standing, not one agent's
           quirk. Negative control, same run: a substantive non-ritual correction
           ("CORRECTION: the env-server retry budget is 3, not 5") still
           SURVIVES, which is what keeps this a receipt filter rather than a
           correction filter — the `[^\n]{0,80}?` leash is what requires the
           opening clause to NAME the ritual. Widening measured over the full
           live population before adoption, never against the motivating example
           (guard-2499).
           The sentence directly above says "match the SUFFIXED forms too", which
           invites building the suffix INTO the pattern; do not. Measured
           2026-08-12 (echo, N=54, cc-03 / Linux 6.8.0-137-generic) on the same
           corpus, same run, only the pattern differing: a regex requiring the
           suffix (`fresh[- ]eyes[- ](review|code|tree|program)`) dropped **16 of
           82**, where the anchored form dropped **70 of 82 (85.4%)** — survivors
           66 vs 12, again in the false-`act_later` direction. The bare
           `Fresh-eyes <n>-><n>` and `Fresh-eyes N=<k>` shapes named at the top of
           this block carry no suffix at all, so a suffix-requiring pattern misses
           the majority of the receipts the block exists to catch. Measured
           2026-08-01 (alpha, N=21, cc-04, over every self_evolution/self-drift
           finding in the 30d window): opening tokens split **287 lowercase vs 40
           capital** — `fresh-eyes-code` ×267, `FRESH-EYES-CODE` ×40, `Fresh-eyes`
           ×27, `FRESH-EYES` ×13, `fresh-eyes` ×12, `fresh-eyes-review` ×4,
           `sq-012 tentative` ×4 (lowercase, against the `sq-012 TENTATIVE`
           literal above), plus `Fresh-eyes-TREE`, `fresh-eyes-tree`,
           `FRESH-EYES-PROGRAM`, `Fresh-eyes-program`. A reader who implements the
           shape list LITERALLY — which is what a pseudocode literal invites —
           catches ~12% of the receipts it was written to catch.
           This was measured BY this defect firing: alpha's own N=14 receipt
           (`fresh-eyes N=14 (alpha, cc-04): COMPLIES at 42.7%...`, lowercase f)
           survived a literal (a-pre) implementation, reached (a0), matched its own
           `alpha` tag, and landed in `board_signals` as a self-evolution signal —
           the exact echo-N=19 convergence above, reproduced one day later by the
           fix meant to prevent it. The case gap lands PRECISELY on own-authored
           receipts, because (a-pre) is the only test that can reach them (see the
           next paragraph), so a case miss here is never harmless.
           Note the direction, which is why this cannot wait for a tidier fix:
           every escaped receipt INFLATES `self_evolution_signals_count`, and
           Phase 5.5 reads that count as change-pressure — so the failure always
           pushes toward a false `act_later`, never toward a missed one.
           **Why this shape test sits ABOVE (a0):** (a0) short-circuits on the
           agent tag, and the tag on a receipt is the AUTHOR'S OWN name — so for
           exactly the agent that posted them, the test that would catch them was
           unreachable, and the ritual reads its own output as input, converging
           on a permanent act_later. Measured echo N=19: filter kept 4, all 4
           echo's own ritual output; honest partner-authored count 0. Line 91's
           intent is preserved — a genuine own-authored FOLLOWUP still counts.
           Owned by `g-115-4087`. Rationale:
           core/config/rationale/fresh-eyes-board-signal-attribution.md
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
           **THIRD FORM: @env-QUALIFIED (`<name>@<env-id>`)** — g-115-4188.
           Same defect, one step further out: a qualified tag matches NEITHER
           the bare test NOR the `agent:` prefix test, so it falls through to
           (a1)/(b) exactly as the `agent:` form did. Normalize by splitting on
           the FIRST `@` (every registry env-id contains a hyphen, so a
           hyphen-joined form cannot be split back unambiguously), then decide
           on the agent part — and on the env part, which carries real meaning
           HERE: `<name>@<this deployment's ENVIRONMENT_ID>` is that agent,
           while `<name>@<some other env-id>` is a PEER DEPLOYMENT's same-named
           agent and is neither MIND_AGENT nor a local partner. Do NOT compare
           only the text before the `@` (guard-2860 — never relax an ownership
           test to a pattern); that reads a peer's agent as the local one.
           Measured 2026-08-06 over 9110 board records: 353 bare routing tags
           vs 7 qualified, and all 7 named a peer deployment — so this form is
           rare-but-live today, and the convention actively recommends it.
           Canonical implementation: `peer_surface.routing_tag_targets_agent`.
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
  # ⚠ THE ORDER IS LOAD-BEARING. Unordered, the prose-mention disjunct matched
  # essentially EVERY peer cadence post (alpha: kept 10 of 18, honest count 0;
  # bravo, next day, different agent: 15 of 29, honest 1), and a partner's
  # untagged sq-012 flipped zeta's verdict to a FALSE act_later. Both inflate
  # self_evolution_signals_count toward a permanent act_later, and both GROW
  # with every new comparison table. guard-1877 (order), g-115-2922 (authorship).
  # Rationale (WHY the order is explicit + both regression traces):
  #   core/config/rationale/fresh-eyes-board-signal-attribution.md
  → call these board_signals
  → surface board_signals to Phase 3 "Recent self-evolution signals" bullets
    so the briefing names the unread finding(s), not just pending-questions

# 2.4 Evolution engine output — dev stage, gap analysis, novelty pressure
# The dev STAGE is NOT in evolution-log.jsonl (schema: {date, event, details};
# probed 2026-07-30, bravo cc-05). This step once captured current_stage/
# gap_analysis/interestingness_state here — fields it never had — so it read
# null every pass, and null read as "no signal" not "wrong file" (rb-245: verify
# a field exists in one live record before reporting its absence). The same
# ambiguity survives a CORRECT read, so classify it (guard-4759).
Bash: bash core/scripts/curriculum-evaluate.sh
  → capture current_stage (+ stage_name / all_passed / terminal_stage / next_stage).
    Branch on SHAPE per the cadence-battery contract: terminal_stage:true with
    all_passed:true and gates:[] is the CORRECT end state, not a pending
    promotion — and stage_name/next_stage are emitted only on the non-terminal
    branch, so their absence there is correct too (g-115-2513).
Bash: tail -n 5 <META_DIR>/evolution-log.jsonl
  → parse recent entries for `event` + `details` (the fields that exist).
    `event` values seen live: strategic_scan, gap_analysis. Gap-analysis and
    interestingness are event VALUES carrying prose in `details`, never keys
    (measured 2026-08-31: gap_analysis as a KEY is 0 of 7090). Read the prose.
  → carry ONE disposition to Phase 3 — absence alone cannot distinguish
    "measured, nothing there" from "never measured", and the two have opposite
    responses (ignore vs. repair the read):
    dev_stage_signal = "measured:<summary>" | "measured:none" (a real negative)
                     | "unmeasured:<why>"   (zero signal, and a repair lead)

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
  → **READ EACH BELIEF TO FULL LENGTH BEFORE CLASSIFYING IT.** A partner belief
    states the OBSERVATION first and its QUALIFYING INTERPRETATION last, so a
    fixed-width display slice reliably shows a drift claim and cuts the clause
    retracting it. This is `guard-1421`'s rule ("a truncated entry is UNREAD — do
    not tune the slice") on a third store, with `guard-2043`'s mechanism (what you
    cut is what matters, because qualifiers accumulate at the END).
    Measured 2026-08-06 (alpha N=44, `hostname` cc-04, `uname -r`
    6.8.0-136-generic) over all four beliefs held about this agent — lengths
    281/337/413/467 chars, **every qualifying clause past char 245** (offsets 247,
    265, 305, 325), so a `[:130]` survey slice showed NONE of them. Read whole,
    bravo's belief opens "alpha has moved off the asp-335 product lane" and closes
    "the directive lane being drained fleet-wide **rather than deprioritised by any
    one agent**" — an EXHAUSTION reading (Decision Rule 14), the opposite of the
    drift its head states.
  → **The stake is arithmetic, not presentation.** This classification becomes
    `confirming_signal_fraction` below, so a truncated read is laundered into a
    number the Phase 5.5 helper cannot audit. Measured here on one unchanged
    signal set: 0.5 → `act_later`; 0.75 and 1.0 → `no_change`. Note the DIRECTION
    — a belief's head states what CHANGED (that is why the partner wrote it), so
    truncation is biased toward reading change-pressure the author explicitly
    disclaimed: always toward a false `act_later`, the same direction as the Phase
    2.3b receipt leak above. When a belief carries no explicit disclaimer, say the
    classification is a judgment and report the fraction BOTH ways.
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
- {Phase 2.4 `dev_stage_signal`, rendered in full and NEVER omitted — an absent
  bullet cannot distinguish "no signal this window" from "never measured".}
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
  "completion_health":              {0..1 — mean completion ratio across active aspirations, EXCLUDING single-goal `asp-xw-` cross-world imports (guard-2829, guard-2804). Each such import is one goal wearing an aspiration's clothes: a hard 0.0 weighted like an 897-goal aspiration, so it dilutes portfolio health rather than measuring it. Measured swing 0.5025 raw -> 0.7537 filtered; two consecutive passes still shipped the RAW figure. Rationale: core/config/rationale/fresh-eyes-signals-json-fields.md},
  "self_evolution_signals_count":   {int — count of recent self-evolution indicators in last 30d = len(pq_signals from Phase 2.3) + len(board_signals from Phase 2.3b, g-115-1214) + len(belief_signals from Phase 2.6b, g-306-28). A partner's belief ABOUT this agent is an external self-evolution signal even when pending-questions.yaml AND the findings board are both empty},
  "confirming_signal_fraction":     {0..1 — = confirming_beliefs / self_evolution_signals_count. A belief_signal (Phase 2.6b) is CONFIRMING if STALE (staleness_days > 14) OR AFFIRMING (its claim matches this agent's current Self focus + active-aspiration lane); DIVERGENT only when FRESH AND suggesting drift/contradiction. pq_signals + board_signals are genuine change-indicators, NEVER confirming. Emit 0.0 only when self_evolution_signals_count == 0. An affirming partner-belief is STABILITY evidence, not change-pressure — counting it toward act_later was a measured false-positive treadmill (g-115-1742). Rationale: core/config/rationale/fresh-eyes-signals-json-fields.md},
  "self_last_updated_days":         {int — days_since_self_updated from Phase 2.1},
  "explicit_user_directive":        {true|false — outstanding /respond about purpose or portfolio},
  "signal_actionable_score":        {0..1 — how clearly the signals map to a specific Self edit}
}'
Bash: echo "$SIGNALS_JSON" | bash core/scripts/self-assess-and-decide.sh --review-type fresh-eyes-review
  → capture decision, rationale, recommended_action from JSON output
```

**NEUTRALIZE ALL THREE SUFFICIENT AXES BEFORE SWEEPING ANY ONE OF THEM**
(guard-3295). `drift >= 0.40`, `net_divergent >= 2.0`, and
`signal_actionable_score >= 0.40` EACH fire `act_later` ALONE, so a sweep that
leaves the others at firing values returns a constant and measures the HELD axes,
not the swept one — three prior fires (N=44/45/47) each booked exactly that as
"robustness". Neutralize to `drift = 0.05`, `confirming = 1.00`,
`actionable <= 0.35`, sweep, and report the flip point rather than the constancy.

Boundaries: `confirming` fires where **`N·(1−confirming) >= 2.0`** — an inequality
in `N`, not a fixed fraction (0.50/N=4, 0.60/N=5, 0.7143/N=7). The two `>= 0.40`
cutoffs above are plain bounds, NEVER intervals — `(0.35, 0.40]` drifted twice.

**COMPUTE `P = len(pq_signals) + len(board_signals)` BEFORE YOU READ THE BELIEFS,
AND SAY WHAT IT WAS** (guard-3390). `pq_signals + board_signals` are spec'd
never-confirming, so `net_divergent >= P` for EVERY possible classification of
EVERY belief: `P >= 3` forces `act_later` before a belief is read, and `P <= 1`
is the only regime where Phase 2.6b decides anything. The step is usually inert,
not usually decisive — but keep reading beliefs to full length when you will ACT
on their content (guard-1421/2043 still bind); just do not report the
classification as having determined a verdict it could not reach.

⚠ **NEVER QUOTE A PRINTED `net` AS THE MARGIN — IT IS ROUNDED, AT ANY `N`.**
`net=2.0` prints across true_net ∈ **[1.95, 2.05]**, spanning BOTH verdicts, and
`confirming` is rounded too — so two runs with OPPOSITE decisions emit
byte-identical `net=2.0 @50%conf`. Recompute `N·(1−confirming)` yourself before
believing any `net`, including one this helper just printed. At `P == 2` (`net`
exactly 2.0) the typed decimal precision of `confirming` IS the verdict: pass
full float precision, or declare the boundary explicitly.

# Rationale (WHY three sufficient axes, the P>=2 derivation, the rounding band,
# and the per-box replications): core/config/rationale/fresh-eyes-self-assess-axes.md

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

**CITE ONLY A RECEIPT YOU CAPTURED (g-115-4405).** If an observation encoded here
needs a board pointer, post FIRST and cite the id `board-post.sh` printed on
stdout — never one recalled from context. Note this skill's own Phase 8 post is
its LAST tool call, so it cannot be the source of an id cited here; if you have
no captured receipt, cite the goal id or the tree node instead. Both resolve.

**VERIFY A BOARD ID ACROSS ALL CHANNELS — this is the actual lesson, and it cost
a goal to learn.** g-115-4405 was filed claiming this step invented
`msg-20260801-042738-alpha-611`, "verified 3 ways" as existing on no channel. It
exists: `reasoning.jsonl`, authored by alpha at the exact claimed timestamp. All
three verifications had searched `findings` and `coordination` only, against a
board that carries EIGHT channels. The suffix cited as proof of fabrication
("-611, off-pattern against findings -55xx") is the reasoning-channel counter —
the strongest stated evidence for the accusation was confirmation of
authenticity. A channel-incomplete probe does not return "unknown"; it returns a
confident, specific, wrong "this never existed."

So before concluding any `msg-…` is fabricated, sweep with
`py -3 core/scripts/board-citation-check.py` (add `--exit-on-hits` for gate use).
It resolves citations in live surfaces against ALL channels — which is the whole
point of it — and classifies non-resolving ones as `dangling` or as a schema
`example`. A citation to a PEER deployment's post will not resolve here and is
EXPECTED (`core/config/conventions/cross-deployment-channel.md`). Dangling
citations are real (30 measured 2026-08-10); this one was not.

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
