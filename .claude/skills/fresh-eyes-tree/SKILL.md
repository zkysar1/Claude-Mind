---
name: fresh-eyes-tree
description: "Periodic local self-audit of the knowledge tree's top-level taxonomy — fires when the every-200-goals precheck cadence trips, or on user demand (/fresh-eyes-tree). Assembles a briefing covering L1 distribution skew (S1), L1 pick-rate trends (S9), candidate L2 promotions, candidate L1 retirements, and SPROUT/REPARENT history, writes it to agents/{agent}/temp/ (a staging file drained to the knowledge tree), and posts a one-line summary to the coordination board. No email push, no user-approval gate. The user can invoke l1-domain-add.sh / l1-domain-rename.sh manually if taxonomy changes are desired. Closes the 'tree has no taxonomy-level review' gap."
user-invocable: true
triggers:
  - "/fresh-eyes-tree"
  - "fresh eyes tree"
  - "tree taxonomy review"
  - "L1 review"
tools_used: [Bash, Read, Write, Edit, Skill]
companion_scripts: [core/scripts/fresh-eyes-cadence-check.sh, core/scripts/fresh-eyes-record-tick.sh, core/scripts/l1-skew-check.sh, core/scripts/l1-emergence-detector.sh, core/scripts/tree-read.sh]
conventions: [aspirations, session-state, working-memory, tree-retrieval]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-fresh-eyes-tree-001"
previous_revision_id: null
---

# /fresh-eyes-tree — Periodic Tree-Taxonomy Self-Audit

Every 200 completed goals (or on user demand), step back and produce a
taxonomy assessment:

**Are the L1s still the right top-level cuts for the knowledge tree?**

The ritual runs autonomously, writes the briefing to `agents/<agent>/temp/`,
and posts a one-line summary to the coordination board. No email push, no
user-approval gate. The user reviews via git log and tracked signals.

If taxonomy changes are desired, the user can invoke the S8 apply scripts
(`l1-domain-rename.sh`, `l1-domain-add.sh`) manually. These scripts remain
standalone user-runnable tools. The periodic ritual does NOT auto-invoke or
gate on them.

## Sibling Relationship

Third sibling in the fresh-eyes ritual family. All three share infrastructure
(`fresh-eyes-cadence-check.sh`, `fresh-eyes-record-tick.sh`) but target
different scopes:

| Ritual | Scope | Cadence | WM slot |
|---|---|---|---|
| `/fresh-eyes-review` | Per-agent Self + portfolio | 25 goals | `last_fresh_eyes_review` |
| `/fresh-eyes-program` | World shared purpose + team alignment | 100 goals | `last_fresh_eyes_program_review` |
| `/fresh-eyes-tree` | **Knowledge-tree top-level taxonomy** | **200 goals** | `last_fresh_eyes_tree_review` |

Lower frequency (200 vs 100 vs 25) because L1 changes are high-blast-radius
and the evidence needs accumulation time. Skew + pick-rate signal is most
useful when measured across a few hundred goals.

## Sub-commands

```
/fresh-eyes-tree                 — User-forced review, bypasses cadence gate
/fresh-eyes-tree --cadence       — Check cadence; run only if gate passes
                                   (agent-invoked path from precheck)
```

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front
matter. Read only the paths returned. If output is empty, all conventions
already loaded — proceed.

## Phase 1: Cadence Gate

```
IF invoked with --cadence:
    # Run this ALONE, in its own Bash call. Do NOT batch it with the 1.1 claim
    # below (see "Never batch the gate with the claim").
    Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_tree
    # Read the OUTPUT TEXT, not just the exit code: the gate prints its verdict
    # ("noop (…)" vs a fire line) on stdout, and a trailing pipe (| tail, | head)
    # replaces the gate's rc with the pipe's (guard-1150). A "noop" line means
    # DONE regardless of what rc you observed.
    IF noop / exit 1: Output "Fresh-eyes-tree: cadence not crossed — noop." → DONE (return)
                      Do NOT run 1.1. A non-running agent must never hold the claim.
    IF exit 0: proceed
ELSE (user-invoked, no --cadence flag):
    Proceed directly — user override.

# 1.1 CLAIM the shared ritual (g-115-3218, 2026-07-28) — the FIRST action once
# committed to running, on BOTH paths above (a user-invoked pass is just as
# much a duplicate risk to a sibling as a cadence-invoked one).
# PRECONDITION: the gate above did NOT noop. This line is conditional, not
# unconditional — reaching it means this agent is about to run the ritual.
Bash: bash core/scripts/fresh-eyes-record-tick.sh last_fresh_eyes_tree_review --claim
```

**Never batch the gate with the claim.** They must be separate Bash calls, in
this order, with the gate's verdict read before the claim runs. Batching inverts
the mechanism: the claim writes unconditionally, so an agent the gate just
SUPPRESSED still stamps `__inflight_claim` — overwriting the claim of the sibling
who is actually mid-flight. Nothing clears that field on the real runner's Phase-8
stamp, so the stomped claim survives to its 30m TTL carrying the wrong
`claimed_by` and a later `claimed_at`, silently extending the suppression window
on every noop. The corruption is invisible: suppression still *works* (a third
agent sees *a* claim), so the only symptom is a wrong name in shared state.

Observed 2026-07-28T10:18 on the sibling `/fresh-eyes-program` (zeta): the gate
printed `noop (… bravo claimed this shared ritual 4.6m ago and is still
mid-flight …)` and the batched claim overwrote bravo's live claim in the same
call. Both team-aware rituals carry the identical shape, so both were corrected.

Same shape as `guard-1061` (run `aspirations-claim.sh` ALONE and verify success
before post-claim setup) and `guard-1007` (read a gate's stdout verdict, do not
trust rc) — a verdict-bearing call and the action it governs never share a Bash
invocation.

**Why the claim, and why it must be FIRST.** This is a SHARED-resource ritual:
one time series, reviewed on behalf of the whole fleet. The cadence gate reads
the shared stamp at ritual START, but that stamp is only written at ritual END
(Phase 8) — so two agents entering this window together both read the same
pre-fire value, both pass, and both run the entire review. Measured: 4 duplicate
rituals across ~34 fires made AFTER the team gate landed (2026-06-15, 06-19, and
BOTH slots on 07-19; gaps 2m48s–9m52s). The claim is visible to a sibling's gate
immediately, so it noops instead of re-deriving the same briefing. Run it as
early as possible — the residual race is exactly the span between the gate
passing and this line. Best-effort: a WARN degrades duplicate suppression but
never blocks the ritual, the call is a no-op for non-team-aware slots, and the
claim expires after 30m so a ritual that dies here cannot lock the fleet out.

## Phase 2: Briefing Assembly (read-only)

Read the inputs. Cache each result so Phase 3 can synthesize without re-reading.

```
# 2.0 PRIOR SERIES — read this FIRST, before any instrument (g-335-315, 2026-07-27)
# The l1-taxonomy-health node is the accumulated memory of every prior review:
# the verdict streak, the Decision Rules, and — critically — the RETRACTIONS
# that prior reviewers recorded so their successors would not re-derive a
# finding already investigated and closed.
#
# ⚠ THIS STEP WAS UNREACHABLE AS WRITTEN — TWO INDEPENDENT DEFECTS, EITHER
# SUFFICIENT ALONE (measured 2026-08-11, alpha, cc-04, Linux 6.8.0-136-generic).
# (1) `tree-read.sh --node` RETURNS METADATA ONLY — key/file/summary/depth/
# confidence — at every depth. It NEVER returns the body, so "capture ALL
# Decision Rules" and "every paragraph containing RETRACTED" were satisfiable
# in full while delivering none of it. The step did not fail; it returned a
# rich-looking summary, which is why it survived. (guard-3312 — which named
# this class for the sibling /fresh-eyes-review and explicitly exonerated the
# other rituals as leaf nodes. That exoneration is FALSE, and this block is
# the correction.)
# (2) EVEN WITH A BODY, THE PARENT IS THE WRONG FILE FOR HALF THE CAPTURE.
# The per-review assessment entries moved OUT to a depth-4 child on 2026-08-01
# (the parent's own summary says so verbatim: "Per-review assessment entries
# live in the -assessments child"). The parent kept the Decision Rules. So the
# two halves of this capture now live in two different files, and this step
# named only one of them for ten months' worth of passes.
# (a) THE ASSESSMENT ENTRIES — the depth-4 child, read DIRECTLY.
#     ⚠ ORDERING IS OLDEST-FIRST: entries are APPENDED, so the newest is at the
#     BOTTOM. This is the OPPOSITE of /fresh-eyes-review's directive-lane-series-
#     <agent>.md, which is newest-FIRST. Do not carry a read direction across the
#     two rituals — `head` here returns 2026-07-31 entries and they look current.
#     ONE self-contained call: shell state does NOT persist between Bash calls,
#     so a $VAR assigned in a previous Bash: line is empty here. Re-source, and
#     compute the start line inside the same invocation.
Bash: source core/scripts/_paths.sh && \
  F="$WORLD_PATH/knowledge/tree/system/tree-taxonomy-review-mechanism/l1-taxonomy-health/l1-taxonomy-health-assessments.md" && \
  L=$(grep -n '^### ' "$F" | tail -3 | head -1 | cut -d: -f1) && \
  sed -n "${L},\$p" "$F"

# (b) THE DECISION RULES + RETRACTIONS — the parent node file, read DIRECTLY.
#     105 lines / ~45KB and it reads WHOLE (its own summary tracks this at 77.5%
#     of the Read cap). If a future split pushes it over, grep the headings first
#     rather than truncating blind. The `world/` prefix IS resolved for Read
#     (path-resolution.md) — unlike a bare `world/` arg to Bash, which is not.
Read world/knowledge/tree/system/tree-taxonomy-review-mechanism/l1-taxonomy-health.md

# (c) OPTIONAL — the tree-read call this step used to make is still useful as a
#     one-line index (it names the child files and the current verdict streak in
#     its summary), but it is NOT the prior-series read and must not stand in for
#     (a) or (b).
Bash: bash core/scripts/tree-read.sh --node l1-taxonomy-health

  → capture from (a): the last 2-3 assessment entries IN FULL
  → capture from (b): ALL Decision Rules, and every
    paragraph containing "RETRACTED" / "recorded so the next reviewer" /
    "does not re-run" / "FALSIFIED"
  → **THE COUNTER COMES FROM (a)'s LAST HEADING, NEVER FROM THE CADENCE GOAL
    COUNT.** The headings are `### YYYY-MM-DD -- world-count NNNN`; that
    world-count is what the next entry's diff is measured against.
  → carry these into Phase 3. Before writing ANY finding, check it against
    them: a signal the series has already investigated and closed is NOT a
    new finding, however fresh the instrument reading looks.
  → also note who fired last and at what counter. If a sibling agent ran
    within ~50 goals, THIS pass is a duplicate-cadence CONFIRMING pass —
    record it as a confirming measurement, not an independent assessment.
    WHY a duplicate can still reach you (corrected 2026-07-28, g-115-3218):
    NOT because "per-agent WM slots do not see each other's fires" — that
    note predates g-115-1388 and is STALE. The gate IS team-aware:
    fresh-eyes-cadence-check.py `team_stamp_value()` reads
    world/team-state.yaml `shared_cadences.<slot>` (written by
    fresh-eyes-record-tick.sh on every fire) and noops while the team's last
    fire is within cadence, so a sibling fire you can SEE is already
    suppressed. What survives is narrower and purely temporal: the gate READS
    that stamp when the ritual STARTS and the stamp is WRITTEN when it ENDS,
    with no write in between — so agents who enter that multi-minute window
    together all read the same pre-fire value and all pass. A duplicate here
    means you overlapped, not that the mechanism is missing.

# 2.0b LOST-WINDOW JOIN (Decision Rule 18, g-115-4196 lineage; added 2026-08-07).
# MANDATORY, and it is a JOIN — neither store answers it alone. The cadence
# STAMP gates the next fire; the assessment ENTRY carries the value. They are
# written in separate steps (Phase 6 Step 1 vs Phase 5.5), so a pass can advance
# the clock without producing the artifact, and BOTH readings stay
# self-consistent: read -assessments alone and you compute your diff from the
# last ENTRY; read the stamp alone and you compute from the last FIRE. The gap
# exists only in the comparison.
#
# Measured 2026-08-07 (echo, world-count 8488): the stamp read
# {2026-08-06T07:39:02, world_goals_count_at_last_fire: 8285, fired_by: foxtrot}
# while the newest entry anywhere in the series was 2026-08-04 / 8082 — a
# 203-world-goal window with NO series record. Confirmed real, not an instrument
# failure, by a tree-wide search for a misfiled entry (none) plus a positive
# control that returned the known 08-04 entry.
#
# The command is named here rather than left as a requirement: Phase 2.0's own
# tree-read cannot answer this, and a step the mandated tool cannot satisfy is
# silently disobeyed by every compliant reader (guard-2466).
Bash: bash core/scripts/team-state-read.sh --field shared_cadences.last_fresh_eyes_tree_review --json
  → stamp_count = world_goals_count_at_last_fire ; stamp_by = fired_by
  → entry_count = the world-count in the NEWEST `### ` heading of
    l1-taxonomy-health-assessments (headings carry it since 2026-08-01;
    pre-08-01 headings say "counter NNNN" and are agent-relative — Rule 13 —
    so treat those as unusable for this join rather than comparing them)
  → IF (stamp_count - entry_count) >= cadence (200): a prior pass FIRED and left
    no entry. Say so in THIS pass's entry, naming stamp_by, both numbers, and the
    window size. Do NOT infer WHY, and do NOT read a partner's empty temp/ as
    evidence — it is a per-agent read-through cache that is not materialized on
    your box (guard-980, check-team-state-before-silent.md). Report the artifact
    gap; never a partner's conduct.
  → ELSE: the series is continuous; note the diff and continue.

# WHY 2.0 EXISTS AND WHY IT IS FIRST. This node used to be read only at Phase
# 5.5, where the encoding novelty-gate forces it — i.e. AFTER the briefing was
# already synthesized. That ordering guarantees each reviewer re-derives from
# scratch and lets a recorded retraction catch the re-run only at encode time,
# once a false section is already written. Measured: the 2026-07-26 pass
# investigated the "tree_growth_log is a dead writer" signal, established that
# the log's contract is L1-ops-only (so zero entries since inception is
# CORRECT), and wrote "recorded so the next reviewer does not re-run it." The
# 2026-07-27 pass re-ran it anyway and wrote a full "the input is dead"
# section, catching it only at the Phase 5.5 novelty gate. The note was neither
# missing nor unclear — it was merely read too late. Same class as this node's
# own Decision Rule 8 (a lesson sitting in two stores that did not reach the
# moment of use). Retrieval must precede synthesis, not follow it —
# .claude/rules/retrieve-before-deciding.md.
#
# ⚠ THE 07-26 RETRACTION WAS ITSELF WRONG, AND IS RETRACTED (g-115-3210,
# 2026-07-29). Its two load-bearing claims are both false on inspection: the
# log held EIGHT entries, not zero, and every one is `op: DECOMPOSE` — which
# is not an L1 op, so "the contract is L1-ops-only" cannot explain the rows
# that are actually in the file. What was true is narrower: the only SCRIPT
# writers were l1-domain-add.py / l1-domain-rename.py (L1_ADD / L1_RENAME),
# while DECOMPOSE was an honor-system instruction repeated at 9 sites in
# tree/SKILL.md and enforced at none. So the 07-27 reviewer's "the input is
# dead" was the CLOSER read, and the novelty gate suppressed it in favour of
# a wrong retraction.
#
# Keep the ordering lesson above — it is sound and independent. But note what
# it cost here: a retraction is read as settled, so it is exactly the kind of
# note that stops the next reader from checking. This one survived two passes
# unexamined because it arrived pre-labelled as the answer. When a recorded
# retraction is the reason you are NOT investigating, spend the one command
# that would falsify it (here: print the log and look at its `op` values).
#
# STATUS NOW: the batch/reparent/remove-child write paths append via
# core/scripts/_growth_log.py (SSOT, called from BOTH tree.py and the daemon's
# tree_write.py). Rows written from 2026-07-29 onward carry a `reason` naming
# the writer. The 2026-04-04 → 2026-07-29 window is deliberately NOT
# backfilled: it is a real gap and later reviews should see it as one.

# 2.1 L1 distribution stats (S1) — the structural mass + retrieval + utility breakdown
Bash: bash core/scripts/l1-skew-check.sh --markdown
  → capture markdown table (already nicely formatted)
Bash: py -3 core/scripts/tree.py read --stats --by-l1
  → capture JSON for programmatic use (mean_confidence, capability_mass, etc.)

# 2.2 L1 pick-rate history (S9) — what got picked recently
Bash: py -3 -c "
import json, sys, os
from pathlib import Path
sys.path.insert(0, 'core/scripts')
from _paths import META_DIR
log = Path(META_DIR) / 'l1-pick-log.jsonl'
if not log.exists():
    print(json.dumps({'entries': 0, 'pick_rate': {}, 'decision_types': {}}))
    sys.exit(0)
lines = log.read_text(encoding='utf-8').splitlines()
recent = [json.loads(l) for l in lines[-500:] if l.strip()]
from collections import Counter
pick_rate = Counter(e['l1'] for e in recent)
decision_types = Counter(e['decision_type'] for e in recent)
sources = Counter(e.get('source') or 'unknown' for e in recent)
print(json.dumps({
    'entries': len(recent),
    'pick_rate': dict(pick_rate.most_common()),
    'decision_types': dict(decision_types),
    'sources': dict(sources),
}))
"
  → parse the JSON for the briefing

# 2.2b MANDATORY git cross-check — S9 CANNOT SEE RESTRUCTURE OPS (Decision Rule 12,
# 2026-07-31). The pick-log above is add-child-shaped: measured at 183 rows it held
# add-child 99 / batch-add-child 83 / test 1 and ZERO decompose/reparent/prune-typed
# rows in its entire history. So its `tree-maintain-decompose` source count can only
# ever sit still, and reading that stillness as "the restructure lane is idle" is a
# confident wrong answer the instrument will support forever.
#
# This is not hypothetical. That count has been frozen at 32 since 2026-05-28 and
# the series filed it as an "observability note" on 07-01, 07-11 and 07-26 without
# checking git. The 2026-07-31 pass went further and wrote a full "the tree has not
# restructured in 64 days, it accretes but cannot rebalance" finding — and filed a
# goal on it — while git showed a vinheim-web-stack decompose (07-28), a /tree
# maintain decompose+distill sweep (07-30), a directive-lane-series distill at 219%
# of cap (07-30), a nine-over-cap-node census (07-30), and the discovery+repair that
# /tree maintain DISTILL was FULLY INERT (07-30). It was caught by a git probe run
# for an unrelated reason, so treat the catch as luck and this step as the method.
#
# RUN THIS BEFORE writing ANY sentence about restructure/decompose/distill activity.
Bash: git log --all --since="14 days ago" --date=short --format="%h %ad %s" \
        | grep -iE "decompose|distill|reparent|prune|split.*node|tree maintain" | head -15
  → If this returns rows, the lane is ACTIVE regardless of what S9 shows. Report
    BOTH, and attribute activity to git, never to the pick-log's silence.
  → If it returns nothing, you still may not conclude "idle" from S9 alone — widen
    the window before making the claim (--since="60 days ago").

# 2.3 Tree growth log — L1-OPS-ONLY history (L1_ADD / L1_RENAME).
# READ THIS BEFORE CONCLUDING THE LOG IS BROKEN. Its only non-test writers are
# l1-domain-rename.py, l1-domain-add.py, and coordination_merge.py. Nothing
# emits REPARENT or PRUNE, and DECOMPOSE is NOT in scope — so a log frozen at
# its 8 founding 2026-04-04 entries is the CORRECT reading, not a dead writer:
# zero L1 ops have ever occurred, therefore zero entries should exist.
# Decompose activity is visible in the l1-pick-log (source:
# tree-maintain-decompose), which is a different instrument, not a replacement.
# This description previously read "RENAME/ADD/REPARENT/PRUNE history", which
# is what made the log look broken to fresh readers — it has now been
# investigated and retracted twice (2026-07-26, 2026-07-27).
# DO NOT interpolate ${WORLD_DIR} into the python -c body — guard-165 forbids
# bash-var injection into py source. Resolve WORLD_DIR via _paths import, the
# same way Phase 2.2 resolves META_DIR.
Bash: py -3 -c "
import sys, yaml
sys.path.insert(0, 'core/scripts')
from _paths import WORLD_DIR
with open(str(WORLD_DIR / 'knowledge' / 'tree' / '_tree.yaml'), 'r', encoding='utf-8') as f:
    tree = yaml.safe_load(f)
log = (tree.get('tree_growth_log') or [])[-30:]
print(yaml.dump(log, sort_keys=False, allow_unicode=True))
"
  → capture last 30 structural ops for the "what's been happening" section

# 2.4 Emergence candidates — S4 new-L1, S6 cross-domain leak, S7 reparent
# signals. The detector reads meta/l1-pick-log.jsonl (S9) and the live
# tree, runs three orthogonal analysis passes, and emits structured
# candidates with strength scores.
Bash: bash core/scripts/l1-emergence-detector.sh --markdown
  → capture the rendered table (paste into Phase 3 briefing verbatim)
Bash: bash core/scripts/l1-emergence-detector.sh
  → also capture the JSON form for programmatic use

JSON shape (consume programmatically when synthesizing the briefing):
  - s4_new_l1_candidates: LIST of {parent_key, l1, cluster_size,
    distinctive_tokens, child_keys, signal_strength}. Empty list when
    no parent met the cluster bar.
  - s6_cross_domain_leaks: LIST of {target_node, current_l1,
    suggested_l1, current_overlap, suggested_overlap, summary}. Empty
    list when no recent SPROUT scored higher against a non-assigned L1.
  - s7_reparent_signals: DICT (NOT a list) — {status, findings,
    total_picks, threshold}. status ∈
      no_data / no_tree   — detector inputs unavailable
      data_sparse         — <10 picks logged; signal not yet meaningful
      balanced            — all L1s within imbalance threshold
      imbalanced          — findings list is non-empty
    findings: LIST of {l1, signal: hot|stagnating|stable-reference,
    pick_share, mass_share, imbalance, interpretation}. Low-write findings
    (stagnating / stable-reference) additionally carry read_side
    (measured|unavailable), retrieval_per_node, median_retrieval_per_node;
    `hot` findings carry none of those — they are judged on write share
    alone, so do not read a missing retrieval_per_node as zero.

    `status: imbalanced` describes the NUMERIC condition, NOT a problem —
    read `signal` for the health judgment (g-115-3214):
      hot              — growing faster than mass; review the boundary
      stagnating       — few picks AND consulted below the median rate;
                         a genuine retirement/merge candidate
      stable-reference — few picks but consulted at/above the median rate.
                         HEALTHY. Done growing is not dying — do NOT
                         propose retirement or merge on the imbalance
                         number alone (guard-731).
    `read_side: "unavailable"` is a PER-L1 claim, NOT a global one: THIS
    L1 has no retrieval data. Read the interpretation string for the
    scope — it names either "no L1 has retrieval data, so the whole
    read-side instrument is blind" or "this L1 has no retrieval data,
    though peers do". The remedies differ: the first means nothing is
    measurable (fix retrieval logging before judging ANY L1); the second
    is a measured peer-relative fact and points TOWARD retirement, not
    away from it. Either way the `stagnating` verdict is write-side-only
    and is not on its own a retirement recommendation (guard-1974 —
    absence of evidence never renders as the healthy verdict). The
    healthy branch is gated on this L1's OWN density, never on the
    median comparison alone: the median collapses to 0.0 as soon as
    >=50% of L1s are unconsulted, and `0.0 >= 0.0` would otherwise hand
    every zero-retrieval L1 `stable-reference` off a zero basis
    (guard-2393 — a cross-item statistic is not evidence about the
    item). Before g-115-3214 the low-write branch was guarded by
    `imbalance > 0`, so a zero-pick L1 was dropped from findings
    entirely and NO zero-pick L1 could ever surface, dead or alive.

S4/S6 return [] when no candidates met the bar; S7 carries an explicit
status to distinguish "wait for more data" from "all clear" — they look
identical in `findings`=[] otherwise. Code that iterates s7 must read
s7["findings"], NOT s7 itself.

# 2.5 Goal-count context
Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_tree --verbose
  → capture current goals-completed count, last-fire count, diff
```

## Phase 3: Synthesis

Build the briefing text (plain Markdown). Each observation must follow
`.claude/rules/communication-clarity.md` rule 6 — assert what evidence shows,
do not hedge.

```markdown
# Fresh-eyes TREE review — {today ISO date}

{One paragraph: N goals completed since last tree review (or "first tree
review since taxonomy review mechanism shipped on 2026-05-14"). What has
happened in the tree across this window — total nodes, growth log
operations, drift signals.}

## Are the L1s still the right top-level cuts?

Current L1 set (from core/config/tree.yaml l1_domains):

{For each L1: "- **{key}** — {summary}"}

### Distribution evidence (S1)

{Paste the markdown table from `l1-skew-check.sh --markdown`}

{Observation paragraph: which L1 is over-mass, which is under-mass.
Reference specific ratios. "Intelligence has 69× the structural mass of
performance; retrieval volume ratio is 124×. Performance has zero EXPLOIT
or MASTER nodes — it is structurally an early-stage L1 12 months after
framework inception."}

### Pick-rate evidence (S9)

Recent {entries} L1 picks (last ~500 tree writes):

| L1 | Picks | % of total |
|---|---:|---:|
{One row per L1 from pick_rate, sorted desc}

Decision-type distribution:
{One row per decision_type from decision_types}

Source attribution (where the picks come from):
{One row per source from sources, top 5}

{Observation paragraph: do picks distribute the same way nodes do? If
yes, the skew is self-reinforcing. If the picks lean toward one L1
disproportionate to its mass, that L1 is growing FASTER than others —
emergent signal worth noting.}

### Structural-op history (last 30)

{Paste tree_growth_log tail. Highlight any L1_ADD or L1_RENAME entries
since last review.}

### Candidate moves

{For each candidate, one bullet with: action (RENAME or ADD), reason
(evidence-backed), proposed key + summary, blast radius. None if the
evidence is balanced — say so.}

Out of scope for this review (filed for future): RETIRE and RESHAPE
operations. Surface candidates here as observations only; do not propose
them as one-shot moves — they require a separate workflow.

## Assessment

**Are the four L1s — execution, intelligence, performance, system —
still the right top-level cuts for this knowledge tree?**

{Agent's assessment based on the evidence assembled above. State the
conclusion clearly: "no change warranted" or "candidate change: {X}
with rationale: {Y}" — but do not auto-apply taxonomy changes.}

If the user decides a change is warranted after reviewing this archive,
they invoke the S8 apply scripts directly:
- `bash core/scripts/l1-domain-rename.sh --approved-by <user-supplied-id>`
- `bash core/scripts/l1-domain-add.sh --approved-by <user-supplied-id>`
```

## Phase 4: Stage Briefing to temp/

```
Bash: mkdir -p agents/<agent>/temp
Write the briefing body to agents/<agent>/temp/fresh-eyes-tree-{today-isotime}.md
  (where {today-isotime} = `date +%Y-%m-%dT%H-%M-%S` — colons replaced with
   hyphens for Windows filesystem compatibility)
```

## Phase 5: Apply-Script Note

The S8 apply scripts (`l1-domain-rename.sh`, `l1-domain-add.sh`) remain
standalone user-runnable tools with their own `--approved-by` validation. If
the user decides a taxonomy change is warranted after reviewing the archived
briefing, they invoke the apply scripts directly. The periodic ritual does
not gate on them.

## Phase 5.5: Encode Durable Findings

The Phase 3 briefing's taxonomy-health observations otherwise land ONLY in
the transient `temp/` staging file, which is invisible to `/prime` and
`retrieve.sh` and is drained away over time. This step encodes the substantive
findings into the durable stores so they survive after the staging file is
drained. Modeled on `/felt-sense-checkin` Phase 1.

**No-double-encode**: this skill has NO `act_now` (Self/Program edit) or
`act_later` (goal) routing — every finding here is net-new durable storage,
so there is nothing to exclude. (If a future revision adds a self-assess /
triage step, this step must then skip any finding already routed to Self or a
filed goal.)

Encode the substantive findings (S1 distribution skew, S9 pick-rate trends,
S4/S6/S7 emergence candidates, structural-op observations, and the overall
taxonomy assessment) per `core/config/conventions/learning-routing.md`. When
in doubt, drop:

- **tree** — a compressed durable fact about tree structure (skew ratio,
  pick-rate distribution, emergence candidate, taxonomy assessment). Target
  the tree-taxonomy-health area under the `system` L1. **Novelty gate
  (mandatory — preserves a time series instead of flooding):** before adding,
  check whether a node already covers this (`tree-read.sh --node
  {candidate-key}`). If one exists and this is a refreshed measurement,
  `/tree edit` it (update body + `last_updated` + `last_update_trigger:
  fresh-eyes-tree`) rather than adding a duplicate. Use `/tree add {parent}
  {key} {summary}` ONLY for a genuinely novel finding (e.g. a new emergence
  candidate).

  **Post-split routing (2026-08-01, g-115-4461) — write to the right one of
  three.** `l1-taxonomy-health` was split when it reached 155% of the Read
  cap; it is now an INTERIOR node and appending a pass entry to it re-creates
  the exact condition the split resolved. Route by content:

  | what you are writing | node |
  |---|---|
  | this pass's assessment entry (`### YYYY-MM-DD — ...`) | `l1-taxonomy-health-assessments` |
  | a measured value backing it | `l1-taxonomy-health-verified-values` |
  | a NEW operating rule derived across passes | `l1-taxonomy-health` (parent, `## Decision Rules`) |

  The parent is what Phase 2.0 reads FIRST, so it must stay small enough to
  Read whole — that is the entire point of keeping only the rules there.
- **reasoning_bank** — a recurring cross-review pattern (e.g. "skew is
  self-reinforcing across 3+ reviews"). `reasoning-bank-add.sh` with summary
  + ABC chain + `applies_to: framework`.
- **guardrails** — a prescriptive rule with a trigger condition (e.g. "when a
  single L1 exceeds N% structural mass for 2+ reviews, surface a decompose
  candidate"). `guardrails-add.sh` with rule + trigger_condition.
- **drop** — already captured, too thin, or a one-cycle anomaly.

### Cap enforcement (STANDING — runs EVERY pass, after the encode above)

This series is append-only by construction: one entry per review, entries are
getting longer, and nothing removes anything. So it re-crosses any cap it is
split under. A one-off split buys ~3 passes and then the same goal is filed a
fourth time — measured twice (`vinheim-web-stack` re-grew 26.9KB→80KB after
its split, g-115-3861; `l1-taxonomy-health` reached 155% and grew a further
12% *while the remedy goal sat queued*, g-115-4461). **This step is what makes
the split durable. Do not skip it because the node "looks fine" — it looked
fine one pass before it was 155%.**

```
# 1. MEASURE the assessments child with the CANONICAL estimator. Never chars/4,
#    never a byte count: bytes are not tokens, and this node's own history has
#    two agents mis-reading a byte figure as a token figure on the same day
#    (guard-1478 / rb-2077). CHARS_PER_TOKEN lives in core/scripts/tree.py — read
#    it, do not hardcode 2.3 here, so this step tracks the constant if it moves.
Bash: py -3 -c "
import sys, pathlib; sys.path.insert(0, 'core/scripts')
from tree import CHARS_PER_TOKEN
p = pathlib.Path(<assessments-child-file>)
c = len(p.read_text(encoding='utf-8')); t = c / CHARS_PER_TOKEN
print('chars=%d tokens=%d pct_of_cap=%.0f%%' % (c, t, 100*t/25000))
"

# 2. IF the child is over 70% of the ~25k cap: archive its OLDEST entries
#    (whole `### ` blocks, oldest first) until it is back under 50%. 70/50 is a
#    hysteresis band, not one threshold — a single trigger point would re-fire
#    every pass and thrash. Headroom is sized for two large entries: the largest
#    observed single entry is ~129 lines / ~9k chars.
#    Archive target: a dated sibling under the same parent, e.g.
#    `l1-taxonomy-health-assessments-archive-{YYYY-H1|H2}`.
#    Use `/tree split-overcap` if the child is ALREADY too big to Read whole
#    (never hand-split an over-cap node); a plain `/tree edit` move is fine
#    while it still reads whole. Either way the parent's `## Decision Rules`
#    and the newest 2-3 entries stay put.

# 3. VERIFY by a full Read that returns the file's FINAL line — the truncation
#    test, not a size estimate (the estimate chooses WHEN to act; the Read is
#    what proves it worked).
```

Report the measurement in the Phase 6 board post either way — including when
no archive was needed. A pass that silently skips this step is
indistinguishable from a pass where the node was healthy, which is how the
last over-cap went unnoticed for 15 passes (`guard-1760`: report what you
declined to act on, not only what you acted on).

Taxonomy CHANGES remain user-driven via the S8 apply scripts (Phase 5) — this
step encodes OBSERVATIONS only, never mutates `core/config/tree.yaml
l1_domains`. The encoding writes are self-evidencing; no separate log line is
required. Do NOT add a terminal action here — Phase 6's board-post remains
the skill's final tool call.

## Phase 6: Record the Tick

```
# Step 1: Record stamp (LOAD-BEARING — same g-240-60 lesson as the sibling rituals)
Bash: bash core/scripts/fresh-eyes-record-tick.sh last_fresh_eyes_tree_review

# Step 1.5: Archive the briefing out of the drain queue (g-115-1838). The
# encode-non-routed-observations step already wrote the briefing's durable
# findings to the durable stores, so the staging .md is a pure archival record.
# Move it to temp/drained/ so it never inflates the precheck temp-pressure metric
# as already-encoded slush (/drain-temp would only DISCARD it). Placing this
# AFTER the encode step keeps the interruption case no worse than today.
Bash: mkdir -p agents/<agent>/temp/drained && mv agents/<agent>/temp/fresh-eyes-tree-{the-Phase-4-isotime}.md agents/<agent>/temp/drained/ 2>/dev/null || true

# Step 2: Board post (best-effort)
Bash: echo "Fresh-eyes TREE review completed; briefing archived to agents/<agent>/temp/drained/fresh-eyes-tree-{today-isotime}.md." | bash core/scripts/board-post.sh --channel general --type status --tags fresh-eyes-tree || true
```

The board-post is the terminal action — per Return Protocol requirements,
the skill does NOT end with text output.

## Chaining

- **Called by**: User (`/fresh-eyes-tree`), `/aspirations-precheck` Phase 0.5e.7
  (`/fresh-eyes-tree --cadence`)
- **Calls**: `fresh-eyes-cadence-check.sh --config-block fresh_eyes_tree`,
  `l1-skew-check.sh --markdown`,
  `tree-read.sh --stats --by-l1`, `fresh-eyes-record-tick.sh
  last_fresh_eyes_tree_review`, `board-post.sh`,
  `/tree add`, `/tree edit`, `tree-read.sh --node`, `reasoning-bank-add.sh`,
  `guardrails-add.sh` (Phase 5.5 encoding)
- **Reads**: `meta/l1-pick-log.jsonl`, `world/knowledge/tree/_tree.yaml`
  (tree_growth_log), `core/config/tree.yaml` (l1_domains)
- **Modifies**: `agents/<agent>/temp/fresh-eyes-tree-*.md` (new staging file),
  `agents/<agent>/session/working-memory.yaml` (update last_fresh_eyes_tree_review),
  board `general` channel (best-effort),
  `world/knowledge/tree/` (Phase 5.5 observation nodes under existing L1s),
  `world/reasoning-bank.jsonl` (Phase 5.5 appends),
  `world/guardrails.jsonl` (Phase 5.5 appends)
- **Does NOT modify**: `core/config/tree.yaml l1_domains` — the L1 cut
  itself. Taxonomy changes (rename/add an L1) remain user-driven via
  `l1-domain-rename.sh` / `l1-domain-add.sh` invoked manually. No email is
  sent. (Phase 5.5 DOES add observation nodes under existing L1s and may
  append reasoning-bank / guardrail entries — see Modifies.)

## Relationship to Existing Mechanisms

| Mechanism | Scope | Trigger | User-facing? |
|-----------|-------|---------|--------------|
| `/tree maintain` | Per-node structural ops (decompose, distill, etc.) | Autonomous + manual | No |
| `/reflect` Step 7 Tree Health Lint | Per-node staleness + cross-ref + width | Reflection cadence | No |
| `l1-skew-check.py` (S1) | Per-L1 distribution measurement | Aspirations-precheck Phase 0.5g (50 goals) | Board post on skew |
| `/fresh-eyes-tree` | **L1 taxonomy itself — is the cut right?** | **200 goals cadence + user demand** | **No — local audit** |
| `l1-domain-rename.sh` / `l1-domain-add.sh` (S8) | Apply approved L1 changes | User invocation | Direct invocation |

Fresh-eyes-tree is the periodic self-audit at the L1-taxonomy scope. It
does NOT replace any of the above. `/tree maintain` keeps node-level
structure healthy; `/reflect` Step 7 lints individual node health; S1
surfaces quantitative skew evidence; S8 applies approved changes (invoked
by the user manually). This skill is the deliberate, low-frequency "step
back and examine the top-level cuts" that none of the others surface. The
user reviews via git log and tracked signals.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call,
not text. The terminal action is the Phase 6 Step 2 board-post Bash call.
Never end this skill with a text summary of the briefing — the briefing
is in the archive.
