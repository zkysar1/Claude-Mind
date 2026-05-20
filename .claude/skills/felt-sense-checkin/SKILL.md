---
name: felt-sense-checkin
description: "Fires whenever the aspirations-precheck cadence hits 75 completed goals — an autonomous structured 7-lane self-audit that converts the user's proven-high-yield diagnostic question into a routine. Sweeps memory hygiene (including insight curation from agents/<agent>/insights.jsonl — distills unprocessed entries into tree/reasoning-bank/guardrails/experience then bulk-marks processed), out-of-cycle completions, unblocks, forward backlog, /verify-learning gaps, meta tuning, and the felt-sense question (where is the pain, what would I change). Writes outputs directly — tree nodes, guardrails, reasoning-bank entries, new goals, verify-learning checks, meta edits. Material Self findings route through the Self-update protocol (guard-380 post-notification); cosmetic findings journal only. Use when the user wants to force the sweep on demand (/felt-sense-checkin) or the precheck cadence triggers automatically. Distinct from fresh-eyes-review (periodic local portfolio self-audit, no email push) and sq-012 (post-goal, narrow) — this is the autonomous structured self-audit that writes directly."
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
Bash: aspirations-query.sh --goal-status in-progress
Bash: aspirations-query.sh --goal-status pending --goal-field participants agent
```

### Multi-Agent Safety Rule (MANDATORY)

In a multi-agent world, `status=in-progress without my claim` is OFTEN
partner work mid-execution, NOT orphan state. Before any mutation:

1. **Read `claimed_by` first**. If `claimed_by` is set AND `claimed_by !=
   $MIND_AGENT`, the goal is partner work — SKIP it. Do NOT reset to
   pending. Do NOT mark completed.
2. **Cross-reference `world/changelog.jsonl`** when `in_flight` is ambiguous
   (e.g., null but the goal status looks suspicious). The changelog is the
   single source of truth for "who wrote what when" — `in_flight` is a
   snapshot that clears at iter-close and re-sets at phase-4, so it can be
   null even when the partner is actively writing.
3. **Only mutate** when `claimed_by == $MIND_AGENT` OR `claimed_by` is null
   AND no recent changelog activity from a partner on this goal-id.

```
FOR each goal in the in-progress / pending-with-agent-participants query:
    claimed_by = goal.claimed_by  # already in the query result
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
Bash: aspirations-query.sh --goal-status blocked
```

For each goal whose blocker is agent-provisionable, re-probe with the
canonical script (per `.claude/rules/probe-before-defer.md`). If the
probe succeeds:
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
staleness scanner against `verify-learning/SKILL.md`:

```
Bash: py -3 core/scripts/verify-learning-staleness.py --text
```

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

Classify your answer:

- **Material** (new primary drive, role change, added/removed operating
  principle or agent-provisionable action, multi-paragraph rewrite):
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
Write: agents/<agent>/reports/felt-sense-<YYYY-MM-DD>.md  # full 7-lane summary
Bash: journal-add.sh stdin JSON {journal_file: agents/<agent>/journal/YYYY/MM/YYYY-MM-DD.md, key_events: [...], tags: [...]}
  # NOTE: journal-add.sh actual API is stdin-JSON only — no --kind / --summary
  # flags. The .md narrative file is written separately (see journal.md
  # convention). This skill writes both: the 7-lane report at
  # bravo/reports/felt-sense-*.md AND the index entry via journal-add.sh.
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
  `agents/<agent>/reports/felt-sense-*.md` (new),
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
