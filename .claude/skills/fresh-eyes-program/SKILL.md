---
name: fresh-eyes-program
description: "Periodic local self-audit of world/program.md — fires when the every-100-goals precheck cadence trips, or on user demand (/fresh-eyes-program). Assembles a briefing (The Program body, both agents' Self summaries, cross-agent aspiration portfolio, recent completions, drift signals), writes it to agents/{agent}/temp/ (a staging file drained to the knowledge tree), and posts a one-line summary to the coordination board. No email push, no user-approval gate — the user reviews changes via git log and tracked signals at their own pace. Sibling to /fresh-eyes-review (per-agent Self, 25 goals). Closes the 'program.md has no systematic evolution path' gap."
user-invocable: true
triggers:
  - "/fresh-eyes-program"
  - "fresh eyes program"
  - "program review"
  - "shared purpose review"
tools_used: [Bash, Read, Write, Edit, Skill]
companion_scripts: [core/scripts/fresh-eyes-cadence-check.sh, core/scripts/fresh-eyes-record-tick.sh]
conventions: [aspirations, session-state, working-memory]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-fresh-eyes-program-e01d9e"
previous_revision_id: null
---

# /fresh-eyes-program — Periodic Shared-Purpose Self-Audit

Every 100 completed goals (or on user demand), step back and produce a
shared-purpose briefing examining two questions:

1. **Is The Program still the right shared purpose for this world?**
2. **Do both agents' Selfs still serve The Program, or are they drifting?**

The ritual runs autonomously, writes the briefing to `agents/<agent>/temp/`,
and posts a one-line summary to the coordination board. No email push, no
user-approval gate. The user reviews changes via git log and tracked signals
at their own pace.

## Sibling Relationship

Sibling to `/fresh-eyes-review`. Both rituals share infrastructure
(`fresh-eyes-cadence-check.sh`, `fresh-eyes-record-tick.sh`) but target
different scopes:

| Ritual | Scope | Cadence | WM slot |
|---|---|---|---|
| `/fresh-eyes-review` | Per-agent Self + portfolio | 25 goals | `last_fresh_eyes_review` |
| `/fresh-eyes-program` | World shared purpose + team alignment | 100 goals | `last_fresh_eyes_program_review` |

## Sub-commands

```
/fresh-eyes-program                 — User-forced review, bypasses cadence gate
/fresh-eyes-program --cadence       — Check cadence; run only if gate passes
                                      (agent-invoked path from precheck)
```

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front
matter. Read only the paths returned. If output is empty, all conventions
already loaded — proceed.

## Phase 1: Cadence Gate

```
IF invoked with --cadence:
    Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_program
    IF exit 1: Output "Fresh-eyes-program: cadence not crossed — noop." → DONE (return)
    IF exit 0: proceed
ELSE (user-invoked, no --cadence flag):
    Proceed directly — user override.
```

The cadence script enforces the 100-goal threshold. User invocation
bypasses it.

## Phase 2: Briefing Assembly (read-only)

Read the inputs. Cache each result so Phase 3 can synthesize without
re-reading.

```
# 2.1 The Program — the world's shared purpose
Bash: world-cat.sh program.md
  → capture full body content
  → compute program_length_lines (roughly — stability proxy; short program = abstract)

# 2.2 Self of this agent — current identity
Read agents/<agent>/self.md
  → capture body content (after YAML front matter) and last_updated
  → compute days_since_self_updated = (today - last_updated).days

# 2.3 Self of partner agent(s) — cross-agent alignment check
Bash: team-state-read.sh --json
  → capture list of agents in agent_status
FOR EACH partner_agent in agent_status.keys() where partner_agent != <current agent>:
    Read <partner_agent>/self.md (if exists)
      → capture body content + last_updated
      → compute days_since_partner_self_updated

# 2.4 Cross-agent aspiration portfolio — the world queue
Bash: load-aspirations-compact.sh
IF path returned: Read it
Extract for each active aspiration:
  - id, title, priority, source (created_by)
  - goals: (completed / total), category
Compute:
  - category_distribution (fraction of active goals per category)
  - aspiration_source_distribution (how many created by each agent vs user)
  - completion_histogram (fraction of aspirations at <20%, 20-80%, >80% complete)

# 2.5 Recent cross-agent completions — what the team has actually done
Bash: team-state-read.sh --json (reuse cached result from 2.3)
  → capture recent_completions (last 20 entries across both agents)
  → group by completed_by agent; summarize key_finding themes

# 2.6 Drift signals — sq-012 history and program-vs-self divergence
Read agents/<agent>/session/pending-questions.yaml
  → capture entries where id starts with 'sq-012' OR tags include 'self_evolution'
    AND created within last 60 days (longer window than fresh-eyes-review since
    program cadence is 4x longer)
ALSO read <partner_agent>/session/pending-questions.yaml if accessible

# 2.6b Drift signals on the findings board (g-115-1258 — sibling of g-115-1214)
# pending-questions.yaml (2.6) is not the only drift surface. A program- or
# self-drift finding posted to world/board/findings — by either agent's
# strategic-scan, fresh-eyes-followup, or cross-agent review — is ALSO a drift
# signal 2.6 never reads. Same omission rb-1279 caught for fresh-eyes-review
# (self_evolution_signals_count=0 while a self-drift finding sat unread);
# fresh-eyes-review closed it in Phase 2.3b — this is the program-scope sibling.
# --unread-only (g-115-2490, parity with g-115-2486's fresh-eyes-review fix):
# count only UNACTIONED drift signals. A signal ACTIONED via `board.py mark-read`
# (consumed into concrete work, or retired with a resolution note) drops out so
# it stops re-counting as net-divergent residue every review within the 60d
# window. Orthogonal to the deliberate no-agent-scoping below (SCOPE note):
# --unread-only filters by the REVIEWING agent's read-state, NOT by who the
# signal is about — program-wide drift is still read program-wide, it just stops
# counting once the reviewing agent has actioned (marked-read) it.
Bash: board-read.sh --channel findings --since 60d --unread-only --json
  → filter to findings WHERE tags intersect
    {'program_drift', 'program-drift', 'self_evolution', 'self-drift'}
  → call these board_signals
  → SCOPE (diverges deliberately from guard-493 AND fresh-eyes-review 2.3b):
    do NOT add --author $MIND_AGENT and do NOT restrict to "directed at this
    agent". guard-493 mandates per-agent scoping ONLY for board-reads feeding a
    PER-AGENT gate (commit-block, blocker filter) where a partner finding would
    false-block THIS agent. Phase 5.5 here is a PROGRAM-level gate
    (self-assess-and-decide --review-type fresh-eyes-program weighs
    partner_alignment_score) — cross-agent drift findings are SIGNAL, not noise.
    Capture findings naming EITHER agent OR the shared purpose. A future
    "add --author" edit would blind the program review to partner drift.
  → window matches 2.6 (60d; program cadence is 4x the per-agent cadence)
  → surface board_signals to Phase 3 "Observations from recent team work"
    (program-side) and "Alignment observations" (self-drift side)

# 2.7 Goal-count context — how much work backs this review
Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_program --verbose
  → capture current goals-completed count, last-fire count, diff
```

## Phase 3: Synthesis

Build the briefing text (plain Markdown, no external links) with sections:

```markdown
# Fresh-eyes PROGRAM review — {today ISO date}

{One paragraph: N goals completed since last program review (or "first
program review"). How The Program has or hasn't changed. What both agents
have been working on across this window. Two or three sentences.}

## Is The Program still the right shared purpose?

Current Program (world/program.md, {program_length_lines} lines, stable since
`git log -1 --format=%ad world/program.md`):

> {world/program.md body, full text — inline, no link}

Observations from recent team work:
- {Evidence-backed bullet — e.g., "85% of active goals sit in category X;
  The Program emphasizes X and Y equally but Y has received 12% of effort."}
- {Evidence-backed bullet — e.g., "Alpha completed 47 code goals; Bravo
  completed 58 PM goals; team ratio matches The Program's Team Model."}
- {Evidence-backed bullet — e.g., "Strategic-scan flagged 'is this serving
  The Program?' 8 times this window — pattern: {theme}."}

Candidate Program refinements (if any):
{Inline diff preview of what might change in world/program.md. None if
the evidence is neutral — say so.}

## Do both agents' Selfs still serve The Program?

**<current agent>'s Self** (last updated {N} days ago):
> {agents/<agent>/self.md body, summarized to 3 sentences}

**<partner>'s Self** (last updated {M} days ago):
> {<partner>/self.md body, summarized to 3 sentences}

Alignment observations:
- {Evidence-backed bullet — e.g., "<agent>'s Self says 'primary driver: X';
  X received 60% of <agent>'s goal budget this window. Aligned."}
- {Evidence-backed bullet — e.g., "<partner>'s Self lists 'audit production
  quality' as first principle; 0 production audits fired this window."}
- {Evidence-backed bullet — e.g., "Neither Self references the team model
  explicitly; The Program defines it. Consider whether Self should cite
  its role in the Team Model."}

Candidate Self refinements (if any):
- {Per-agent proposal with rationale. None if the evidence is neutral —
  say so. Self changes route through sq-012 / guard-380, not this ritual —
  this briefing only flags candidates for the user to consider.}

## Assessment

1. Is The Program still the right shared purpose for this world, or does
   it need to refine (or narrow, or broaden)?
2. Do both agents' Selfs still serve The Program, or is one drifting
   away from shared purpose?

{Agent's own assessment based on Phase 5.5 decision: act_now / act_later /
no_change, with rationale. No user response requested.}
```

All observations must follow `.claude/rules/communication-clarity.md` rule 6:
state what the evidence shows, do not hedge. If evidence is ambiguous, say
"the evidence shows X but does not show Y."

## Phase 4: Stage Briefing to temp/

Write the briefing body to
`agents/<agent>/temp/fresh-eyes-program-{YYYY-MM-DDTHH-MM-SS}.md` as a staging
artifact — its durable findings are encoded to the knowledge tree by Phase 5.6,
after which Phase 8 Step 1.5 archives the file to `temp/drained/` (it never enters
the `/drain-temp` queue as already-encoded slush — g-115-1838; see
`core/config/conventions/temp-store.md`). Timestamp includes HH-MM-SS so multiple
same-day invocations (cadence fire + user-forced review) do not collide.

```
Bash: mkdir -p agents/<agent>/temp
Write the briefing body (from Phase 3) to agents/<agent>/temp/fresh-eyes-program-{today-isotime}.md
  (where {today-isotime} = `date +%Y-%m-%dT%H-%M-%S` — colons replaced with
   hyphens for Windows filesystem compatibility)
```

## Phase 5.5: Self-Assess Decision

Classify the review outcome via the deterministic helper and act on it.
No escalation to the user — the agent decides and proceeds autonomously.

For fresh-eyes-program, an `act_now` decision applies the `world/program.md`
edit inline and it finalizes IMMEDIATELY (status=final), then posts a
`program-change` entry to the `decisions` board for POST-HOC partner review —
the guard-380 "notify after, revert if wrong" model. Partners read the
decisions-board post during `/prime` Phase 2 and revert via git if they
disagree. NOTE: the cross-agent *pre-ack* flow documented in
world/conventions/self-program-evolution.md Phase 6 (propose → partners
ack/reject → finalize) is **DESIGNED BUT NOT IMPLEMENTED** (g-115-1619 verified
2026-06-23: `program-change-propose.py` + `program-ack-sweep.py` were never
built). Do NOT assume an `act_now` Program edit sits in `awaiting_acks` — it is
live the moment `evolution-complete.sh` finalizes it. The LLM applies the Edit;
`evolution-complete.py` finalizes ONLY — it does NOT auto-post. The LLM then
posts the `program-change` entry to the decisions board itself (verified live
2026-07-10, g-115-1904: FINALIZED output carried no board post).

Extract signals from the Phase 3 briefing synthesis (scored 0..1 unless
noted) and pass to the helper. Note `partner_alignment_score` is REQUIRED
for fresh-eyes-program — derived from comparing per-agent Self emphases
and recent goal portfolios:

```
# Build signals JSON from Phase 3 briefing content
SIGNALS_JSON='{
  "portfolio_drift_score":          {0..1 — degree cross-agent portfolio has drifted from Program emphasis since last review},
  "completion_health":              {0..1 — average completion ratio across active aspirations (both agents)},
  "self_evolution_signals_count":   {int — Program-related drift indicators across both agents = sq-012/self_evolution pending-questions hits citing purpose (2.6) + len(board_signals) from the findings board (2.6b). A program/self-drift finding on the board counts even when pending-questions is empty (rb-1279 — the count silently 0 on 2026-05-24)},
  "self_last_updated_days":         {int — days since world/program.md last_updated},
  "partner_alignment_score":        {0..1 — cross-agent agreement on Program emphasis; LOW = misalignment},
  "explicit_user_directive":        {true|false — outstanding /respond about The Program or shared purpose},
  "signal_actionable_score":        {0..1 — how clearly the signals map to a specific Program edit}
}'
Bash: echo "$SIGNALS_JSON" | bash core/scripts/self-assess-and-decide.sh --review-type fresh-eyes-program
  → capture decision, rationale, recommended_action from JSON output
```

Branch on decision:

- **`act_now`** — apply the Program edit inline via Edit on
  `world/program.md`. The Phase 2 hooks
  (`evolution-prepare.sh` → `evolution-record.sh`) capture as a
  program-evolution stub; invoke `bash
  core/scripts/evolution-complete.sh --revision-id <stub-rev> --reasoning
  "<≥80-char rationale citing fresh-eyes-program briefing signals + Phase
  3 evidence>" --signal-source fresh-eyes-program` to finalize. For
  material classification, `evolution-complete.py` finalizes the edit
  immediately and posts a `program-change` entry to the `decisions` board
  for post-hoc partner review (guard-380 model). The D2 cross-agent pre-ack
  flow (`program-change-propose.py` → `awaiting_acks` → `program-ack-sweep.py`)
  is **NOT IMPLEMENTED** (g-115-1619) — the edit is live on finalize, not
  pending ack.
- **`act_later`** — file an Idea goal:
  `bash core/scripts/aspirations-add-goal.sh asp-115` with stdin
  `{"title":"Idea: Program refinement - <one-line>",
  "priority":"MEDIUM","origin_signal":"idea:fresh-eyes-program-followup",
  "description":"<copy briefing observations + recommended_action>"}`.
- **`no_change`** — silent no-op. Phase 8 cadence stamp still fires.

ALWAYS log the decision to `agents/<agent>/journal` (one-line tagged
`fresh-eyes-program-decision`) and post to the `reasoning` board summarizing
decision + rationale. The audit trail is the guardrail's evidence path.

## Phase 5.6: Encode Non-Routed Observations

Phase 5.5 routes at most ONE finding to a durable home (a Program edit via
`act_now`, or an Idea goal via `act_later`). Every OTHER observation in the
Phase 3 briefing — program-vs-portfolio alignment ratios, team-model coverage
evidence, cross-agent drift patterns, Self-vs-Program divergence signals —
otherwise lands ONLY in the transient `temp/` staging file, which is invisible
to `/prime` and `retrieve.sh` and is drained away over time. This step encodes
the observations that have no other durable home, so the briefing's knowledge
survives after the staging file is drained. Modeled on `/felt-sense-checkin` Phase 1.

**No-double-encode**: skip the single observation Phase 5.5 already routed
(the `act_now` Program-edit target, or the `act_later` goal's
`recommended_action`). The journal/board decision entries carry the decision
label, not the observations — they are not duplicates. Per-agent Self
refinements stay out of scope here (they route through sq-012 / guard-380,
per Phase 3) — do NOT encode Self-change proposals.

For each REMAINING briefing observation, classify per
`core/config/conventions/learning-routing.md` and route to ONE store. When in
doubt, drop — the asymmetry favors dropping:

- **tree** — a compressed durable fact (program-alignment ratio, team-model
  coverage metric, cross-agent work dynamic). **Novelty gate (mandatory):**
  before adding, check whether a node already covers this observation
  (`tree-read.sh --node {candidate-key}`, or a `retrieve.sh` lookup). If one
  exists and this is only a refreshed measurement, `/tree edit` it (update
  body + `last_updated` + `last_update_trigger: fresh-eyes-program`) instead
  of adding a duplicate. Use `/tree add {parent} {key} {summary}` ONLY for a
  genuinely novel finding.
- **reasoning_bank** — a recurring drift / alignment diagnostic.
  `reasoning-bank-add.sh` with summary + ABC chain + `applies_to`
  (`framework` for multi-agent / program-alignment patterns, else `any`).
- **guardrails** — a prescriptive rule with a trigger condition.
  `guardrails-add.sh` with rule + trigger_condition.
- **drop** — already captured, too thin, or a one-cycle anomaly.

The encoding writes are self-evidencing; no separate log line is required. Do
NOT add a terminal action here — Phase 8's board-post remains the skill's
final tool call.

## Phase 8: Record the Tick

Update the WM slot so the cadence gate stops firing until 100 more goals
have completed.

**Critical invariant**: the stamp write is LOAD-BEARING. The cadence gate
reads `last_fresh_eyes_program_review` to decide whether to fire again. If
this step silently fails, the gate re-fires every iteration. Same
failure-mode lesson as fresh-eyes-review (see its Phase 8 rationale — the
g-240-60 incident applies here too).

### Step 1: Record the stamp (LOAD-BEARING — never skip)

```
Bash: bash core/scripts/fresh-eyes-record-tick.sh last_fresh_eyes_program_review
```

The positional slot-name arg routes the tick to this ritual's slot instead
of `last_fresh_eyes_review`. The wrapper reads the current completed-goals
count via `fresh-eyes-cadence-check.sh --print-current` (slot-agnostic),
writes the slot atomically, and verifies the slot is non-null after the
write (fails exit 1 on silent write failure). One script call, one failure
mode — no chaining.

### Step 1.5: Archive the briefing out of the drain queue (g-115-1838)

The briefing's durable value is fully extracted by now (Phase 5.5 routed one
finding; Phase 5.6 encoded the rest), so the staging `.md` is a pure archival
record. Move it into `temp/drained/` so it never inflates the precheck
temp-pressure metric as already-encoded slush (`/drain-temp` would only DISCARD
it). Placing this AFTER Phase 5.6 keeps the interruption case no worse than
before: if the skill dies before this step, the briefing stays in `temp/` for
the next drain, exactly as today.

```
Bash: mkdir -p agents/<agent>/temp/drained && mv agents/<agent>/temp/fresh-eyes-program-{the-Phase-4-isotime}.md agents/<agent>/temp/drained/ 2>/dev/null || true
```

Use the exact filename written in Phase 4. This is a bookkeeping move, NOT the
terminal action — Step 2's board-post remains the skill's final tool call.

### Step 2: Post to board (best-effort, must not block)

```
Bash: echo "Fresh-eyes PROGRAM review completed; briefing archived. Decision: {decision from Phase 5.5}." | bash core/scripts/board-post.sh --channel general --type status --tags fresh-eyes-program || true
```

The `|| true` ensures board-post failure (board file locked, quota
issue, etc.) does NOT propagate back through the skill and does NOT
affect the already-completed stamp write. Board-post is
cross-agent-visibility nice-to-have, not load-bearing.

The board-post is the terminal action — per Return Protocol requirements,
the skill does NOT end with text output.

## Chaining

- **Called by**: User (`/fresh-eyes-program`), `/aspirations-precheck`
  Phase 0.5e.5 (`/fresh-eyes-program --cadence`)
- **Calls**: `fresh-eyes-cadence-check.sh --config-block fresh_eyes_program`,
  `load-aspirations-compact.sh`,
  `wm-read.sh`, `wm-set.sh`, `team-state-read.sh`, `board-read.sh`, `world-cat.sh`,
  `self-assess-and-decide.sh`, `fresh-eyes-record-tick.sh
  last_fresh_eyes_program_review`, `board-post.sh`, `journal-add.sh`,
  `/tree add`, `/tree edit`, `tree-read.sh`, `reasoning-bank-add.sh`,
  `guardrails-add.sh` (Phase 5.6 encoding)
- **Reads**: `world/program.md`, `agents/<agent>/self.md`, `<partner>/self.md`
  (for each partner agent in team-state), `agents/<agent>/session/pending-questions.yaml`,
  `world/board/findings` (drift signals, Phase 2.6b),
  world aspirations compact, `agents/<agent>/session/working-memory.yaml`
- **Modifies**: `agents/<agent>/temp/fresh-eyes-program-*.md` (new staging file),
  `agents/<agent>/session/working-memory.yaml` (update last_fresh_eyes_program_review slot),
  `agents/<agent>/journal.jsonl` (append), board `general` channel (best-effort),
  `world/knowledge/tree/` (Phase 5.6 new/edited nodes),
  `world/reasoning-bank.jsonl` (Phase 5.6 appends),
  `world/guardrails.jsonl` (Phase 5.6 appends)
- **Does NOT modify**: `world/program.md` (unless Phase 5.5 returns act_now),
  `agents/<agent>/self.md`, `<partner>/self.md`, aspiration priorities,
  pending-questions. No email is sent.

## Relationship to Existing Mechanisms

| Mechanism | Scope | Trigger | User-facing? |
|-----------|-------|---------|--------------|
| `sq-012` | Per-agent single-outcome self-purpose check | Post-goal | Only for material changes |
| `aspirations-strategic-scan` S3b | Portfolio category coverage | Autonomous cadence (5 goals / 4h) | No |
| `aspirations-evolve` | Portfolio gap + dev-stage tuning | Autonomous cadence (15 goals / 12h) | No |
| `/priority-review` | Portfolio ranking | User pull | Yes, but pull-only |
| `/fresh-eyes-review` | **Per-agent Self + portfolio** | **25 goals cadence** | **No — local audit** |
| `/fresh-eyes-program` | **World shared purpose + team alignment** | **100 goals cadence** | **No — local audit** |

Fresh-eyes-program is the periodic self-audit at the world-purpose scope.
It does NOT replace any of the above. sq-012 catches per-goal purpose
drift, fresh-eyes-review catches per-agent Self drift every 25 goals,
strategic-scan watches category concentration autonomously, evolution runs
gap analysis, priority-review is the user's anytime portfolio pull.
Fresh-eyes-program is the less-frequent "step back and examine the shared
purpose" that the per-agent rituals never surface because their scope is
Self, not Program. The user reviews all changes via git log and tracked
signals.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call,
not text. The terminal action is the Phase 8 Step 2 board-post Bash call.
Never end this skill with a text summary of the briefing — the briefing
is in the archive, the agent's job is to record the tick and return control.
