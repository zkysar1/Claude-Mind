# Domain Hooks — How Core Pseudocode Reaches Into `world/`

## Purpose

The cognitive core (`core/`, base skills under `.claude/skills/`, rules under
`.claude/rules/`) must stay domain-agnostic per `.claude/rules/domain-free-examples.md`.
But many loop phases need to run domain-specific work: commit-and-push after a
code change, refresh a user-signal snapshot before goal selection, pull outcome
metrics after a state update.

This file documents the three patterns the framework uses to bridge core→world
without embedding domain details into core files, and names the four canonical
*hook slots* that Pattern B provides.

Anti-pattern to avoid: hardcoding `bash world/scripts/<script>.sh` or
`world/conventions/<file>.md` inside core or base-skill pseudocode. Core pseudocode
never names a specific world artifact directly — it names a slot (category/intent)
that the world fills.

## The Three Patterns

### Pattern A — Two-Tier File (used by `/verify-learning`)

Core owns both a *framework* file and a *template* file; world owns the lived
file that extends the template.

- Hard-read in core: `core/config/<feature>.md` (framework)
- Hard-read in core: `core/config/<feature>-domain-specific.md` (template with
  `<!-- Example -->` comments, seeded into world on init)
- Conditional-read: `world/<feature>.md` IF it exists (agent-discovered content)

Seeding: `init-world.sh` seeds (or references) the template into the world file;
the agent grows the world file over time through normal operation.

Canonical example: `.claude/skills/verify-learning/SKILL.md` Step 1 reads three
files — `verification-checklist.md`, `verification-checklist-domain-specific.md`,
and `world/verification-checklist.md` (the last one existence-gated).

When to use: domain customization is *additive checks/rules on top of a generic
foundation*, and the foundation lives in core.

### Pattern B — Existence-Gated Hook Slot (used by pre/post-execution)

Core names a *slot* by filename convention. The world fills the slot with a
convention file. Core pseudocode loads the convention via `load-conventions.sh`
and gates execution on the file existing in `$WORLD_DIR/conventions/`. Missing
file = silent no-op.

Canonical shape (mirror this exactly in new hook-slot call sites):

```
Bash: load-conventions.sh <slot-name> → IF path returned: Read it
# Procedural convention — gate on file EXISTENCE, not load status.
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/<slot-name>.md" && echo "exists"
IF exists:
    Follow each Step in the convention
ELSE:
    # No domain convention for this slot (fresh agent). Nothing to do.
```

Seeding: `/start` Phase C0.5 does **per-slot existence detection** — it checks
each canonical slot independently (`pre-execution.md`, `post-execution.md`),
not whether `world/conventions/` has any `.md` file at all. For each missing
slot the framework default template (`core/config/templates/<slot>-default.md`)
is ALWAYS installed by construction via `cp` — framework invariants
(`/verify-learning` Section DC: Step 1.5 → 1.75 → 2 ordering, fresh-eyes
wiring, `--author $MIND_AGENT` filter, step count ≤ 12) are guaranteed
without depending on the bootstrap-time LLM to reproduce the contract. The
user is then optionally offered to append domain-specific steps under a
`## Domain Additions` header at the bottom of each file. Worlds may also
grow new hook slots on-demand through replay/reflection — convention
changes are tracked in `world/conventions/convention-changes.jsonl` for audit.

The per-slot detection model replaced an earlier whole-directory short-circuit
that skipped seeding any time `world/conventions/` contained ANY `.md` file:
worlds that had `efs-session-paths.md` but no `post-execution.md` silently
inherited zero canonical-slot behavior. The template-based default means a
fresh world satisfies the structural verify-learning checks on day one,
without depending on the LLM-of-the-day to reproduce the contract.

When to use: the hook runs a *procedural sequence* (may invoke world scripts,
read world state, post to boards), and whether it runs at all is a domain
choice. Most new core→world coupling belongs here.

### Pattern C — Forged-Skill Registry (used for capability bridges)

Core describes an *intent* in natural language. A forged skill registered in
`world/forged-skills.yaml` matches the phrase via its `triggers:` list and
handles the intent. Missing trigger = intent silently unhandled (core should
have a Tier-3 fallback).

Reference: `.claude/rules/forged-skill-resolution.md`.

Canonical shape:

```
Notify the user about <event>.
(Check world/forged-skills.yaml for a skill whose triggers match
"notify the user" and invoke it with a short subject and message. If no
matching skill is registered, fall back to a `participants: [agent, user]`
goal via aspirations-add-goal.sh.)
```

Seeding: `/forge-skill` creates the skill when a capability gap surfaces; the
forged skill's SKILL.md is the unit of encapsulation.

When to use: the bridge is a *discrete capability* an agent might acquire at any
time (notification, build trigger, deployment), and the shape of the capability
varies across domains. A hook slot would be too rigid; a new base skill would
be too permanent.

## Pattern Selection

| Question | Answer routes to |
|---|---|
| Does core need a foundation that world extends? | Pattern A |
| Does core need to run a *procedural sequence* defined by the domain? | Pattern B |
| Does core need to invoke a *discrete capability* the domain may or may not have? | Pattern C |

Default for *new* core→world coupling: Pattern B. It is the cheapest to add
(one new slot name + an existence-guarded block + a world .md file), survives
fresh worlds without breakage, and evolves through the replay/reflection path
the framework already has.

## Canonical Hook Slots (Pattern B)

| Slot name | Consumer (core) | Purpose | Default template | Seeded by |
|---|---|---|---|---|
| `pre-execution` | `.claude/skills/aspirations-execute/SKILL.md` Phase 3.9 | Domain pre-checks before goal execution (e.g., pull latest, curriculum gate) | `core/config/templates/pre-execution-default.md` | `/start` Phase C0.5 — per-slot detection; always `cp` template (installed by construction), optional domain additions appended under `## Domain Additions` |
| `post-execution` | `.claude/skills/aspirations-execute/SKILL.md` Phase 4.2 | Domain post-execution steps (e.g., run tests, commit-and-push) | `core/config/templates/post-execution-default.md` | `/start` Phase C0.5 — per-slot detection; always `cp` template (installed by construction), optional domain additions appended under `## Domain Additions` |
| `signal-refresh` | `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5.0-pre | Refresh user-signal inputs before goal scoring | none (no canonical default) | On-demand — create `world/conventions/signal-refresh.md` when the domain has a signal source to scan. `/start` C0.5 prints an informational note about this slot. |
| `outcome-observation` | `.claude/skills/aspirations-state-update/SKILL.md` Step 8.12 | Pull business-layer outcome metrics (commits, CI, service health) after state update to detect process-vs-outcome divergence | none (no canonical default) | On-demand — create `world/conventions/outcome-observation.md` when the domain has measurable outcomes beyond goal-completion counts. `/start` C0.5 prints an informational note about this slot. |
| `commons-retrieval` | `core/config/execute-protocol-digest.md` Step 4a (after Step 4 retrieval) | Retrieve from a SHARED cross-world knowledge commons, so goal execution builds on what other agents/worlds already learned instead of re-deriving it | none (no canonical default) | On-demand — create `world/conventions/commons-retrieval.md` when the domain participates in a shared commons. A world that shares nothing simply has no file, and Step 4a is a silent no-op. |

Adding a new slot is allowed and expected — the framework should grow new
hook slots as new kinds of core→world coupling are discovered. Each new slot
must:

1. Be added to the table above with its consumer path and purpose
2. Use the canonical Pattern B shape at the call site (existence-gated
   `test -f "$WORLD_DIR/conventions/<slot>.md"` after `load-conventions.sh`)
3. Fail-open in fresh worlds — missing convention = silent no-op
4. Include a brief block comment at the call site pointing back to this file
5. Have at least one feedback path registered (see Evolution → Mutation
   Sources) — a slot with no mechanism for proposing edits against
   experience is a frozen contract, not a hook

### Targeting Guidance (Which Slot Does a Feedback Proposal Go To?)

Feedback paths that mint convention proposals (`replay` Step 3.5,
`reflect-on-outcome` guardrail promotion, `aspirations-evolve` CONVENTION
HEALTH audit) must decide which slot a new step belongs in. As of this
writing, `reflect-on-outcome` uses a binary `pre vs post` classifier and
`replay` uses a three-way `pre / post / skip`. Neither can target
`signal-refresh` or `outcome-observation`, and the existing ledger
(`convention-changes.jsonl`) shows 4/4 proposals routed to `pre-execution`
as a result. New call-site logic should use the four-way classifier below.

| Proposal nature | Target slot |
|---|---|
| Setup, prerequisite check, resource acquisition, or confidence-lowering step BEFORE a goal runs | `pre-execution` |
| Verification, cleanup, commit, test run, or health record AFTER a goal completes | `post-execution` |
| A new input channel to scan or refresh BEFORE goal scoring (user email, board directive count, external queue state) | `signal-refresh` |
| A new outcome metric to pull from the real world AFTER state update (repo commits, CI pass rate, service health, business KPI) | `outcome-observation` |
| A new EXTERNAL knowledge source to consult while retrieving context for a goal (shared commons, partner-org index, foreign-world store) | `commons-retrieval` |
| None of the above fit cleanly | SKIP — do not force a fit. An unroutable proposal is signal that a new slot may be needed; file an Idea goal to add one. |

**Decision order (check specific before general):** outcome-observation →
commons-retrieval → signal-refresh → post-execution → pre-execution → skip. Treat
`pre-execution` as catch-all only when the proposal is about *preparing a
single goal's execution*, not *refreshing inputs that affect all goals*.

## Directional Context (Not Hook Slots)

Two files shape behavior at every hook but are NOT hook slots themselves:
`agents/<agent>/self.md` and `world/program.md`. They supply *directional context*
— identity ("who am I") and purpose ("why does this world exist") — that
every phase of the loop reads, but they do not expose Pattern B call sites.
Keep them OUT of the canonical hook slot table.

| File | Kind | Evolves via | Read by |
|---|---|---|---|
| `agents/<agent>/self.md` | Agent identity, role, operating principles, agent-provisionable actions | sq-012 spark (after every goal); fresh-eyes-review cadence (every 25 goals); ABC-chain drift; guard-380 post-notification | Aspiration generation, goal prioritization, gap analysis, evolution |
| `world/program.md` | World's shared purpose, team model, success metrics | `/fresh-eyes-program` periodic local self-audit (every 100 goals); user directives via `/respond` | Aspiration generation, strategic scans, fresh-eyes-review observations, fresh-eyes-program briefing |

A feedback proposal that wants to change identity or shared purpose routes
to `sq-012` (self.md) or `/fresh-eyes-program` (program.md) — not to
`convention-changes.jsonl`. Identity/purpose drift is a different learning
signal than procedural-hook drift and needs a different ledger.

The `/fresh-eyes-program` ritual is the sibling of `/fresh-eyes-review` at
the world-purpose scope: it assembles a briefing from `world/program.md`
plus each agent's `self.md` plus cross-agent portfolio, writes the briefing
to `agents/<agent>/temp/`, and posts a one-line summary to the coordination
board. No email push, no user-approval gate — the user reviews via git log
and tracked signals at their own pace. Cadence parameters live in
`core/config/aspirations.yaml` → `fresh_eyes_program` (goal-count cadence
only).

## Per-Agent Overlay (Future, Not Implemented)

Today, `pre-execution.md` and `post-execution.md` are **world-shared single
conventions** read by `aspirations-execute` for every agent in the world.
There is no per-agent override mechanism. All agents follow the same
pre/post-execution steps.

If per-agent overrides are ever needed (e.g., different agents with different
test suites, deployment lanes, or pre-check requirements), the design path is:

1. **Optional file path**: `agents/<agent>/conventions/<slot>.md` (mirrors the slot
   name from the canonical hook slots table above).
2. **Resolution order**: world-level convention loaded first as base; agent
   overlay file (if present) merged or appended. Missing agent overlay = no
   change (world convention applies as-is).
3. **Implementation effort**: a small change to `aspirations-execute` Phase 3.9
   (pre-execution call site) and Phase 4.2 (post-execution call site), plus
   any other Pattern B hook consumer. After reading the world-level convention,
   check for `$AGENT_DIR/conventions/<slot>.md` and merge if present.
4. **Scope**: applies to all four canonical Pattern B slots (`pre-execution`,
   `post-execution`, `signal-refresh`, `outcome-observation`), not just the
   first two. New slots added to the canonical table inherit the same
   overlay mechanism.

This section is intentionally documentation-only — no code change — per
`.claude/rules/implementation-discipline.md` "no speculative features." The
need has not surfaced in production; when it does, this design path makes the
implementation a fast follow.

*Design path surfaced on 2026-05-19 during the `/start` prompt-defaults
investigation.*

## Convention File Format

A world convention file for a hook slot is a markdown document the LLM reads
and follows procedurally at the call site. Minimum shape:

```markdown
# <Slot Name> Convention

## Purpose
<one or two sentences — what this hook does for this domain>

## When This Fires
<which core phase/step invokes this convention and what context is available>

## Steps
1. <imperative step — may include `Bash: <script>` invocations>
2. <...>

## Fail-Open Policy
<what happens when a step fails — default: log and continue; never abort the caller>

## Downstream Consumers
<which core files read outputs this hook produces, and where those outputs live>
```

The convention may invoke world scripts (e.g., `bash world/scripts/<name>.sh`);
core pseudocode does not — core only invokes the convention.

## Evolution

Convention files under `world/conventions/` mutate over time through three
autonomous feedback paths plus direct user action. Every proposal and
application is recorded in `world/conventions/convention-changes.jsonl` so
the history of *why* a hook's procedure changed is auditable.

### Mutation Sources

| Source name | Skill | Trigger | Initial confidence |
|---|---|---|---|
| `replay-pattern-mining` | `.claude/skills/replay/SKILL.md` Step 3.5 | ≥2 corrected hypotheses share a procedural-gap condition | 0.5 (+0.15 per reinforcement) |
| `reflect-on-outcome` | `.claude/skills/reflect-on-outcome/SKILL.md` (post-resolution) | Single-hypothesis correction promotes an existing guardrail; recurrence check counts similar active guardrails | 0.6 (new); 0.85 (auto-applied on ≥2 similar guardrails) |
| `evolve-health-audit` | `.claude/skills/aspirations-evolve/SKILL.md` Step 3.5c | Active guardrail has `utilization.times_active ≥ 5` AND is procedural + universal | 0.7 |
| User directive | `/respond` | User requests convention change | Direct edit + ledger entry |
| Idea / Maintain goal | Normal goal execution | Agent edits a convention as a goal's primary action | Direct edit + ledger entry |

A slot with zero mutation-source coverage is a frozen contract. New hook
slots MUST either route to an existing source (via the Targeting Guidance
classifier above) or register a new source — otherwise they will not
evolve against experience and will drift out of sync with the world.

### Proposal Schema (`convention-changes.jsonl`)

Each line is one JSON record:

```json
{
  "date": "YYYY-MM-DD",
  "type": "add" | "promote_guardrail" | "edit" | "remove",
  "target": "<slot-name>",
  "proposed_step": {
    "title": "...",
    "condition": "IF ...",
    "action": "..."
  },
  "source": "<mutation source name from table above>",
  "source_hypothesis": "<hyp-id or null>",
  "source_guardrails": ["<guard-id>", ...],
  "reinforcement_count": <int>,
  "confidence": <0.0..1.0>,
  "status": "pending" | "applied" | "rejected" | "pending_capacity",
  "applied_date": "YYYY-MM-DD (only when status=applied)"
}
```

### Promotion Gate

`.claude/skills/aspirations-evolve/SKILL.md` Step 3.5d sweeps
`convention-changes.jsonl` on every evolve cadence and advances `pending`
proposals based on thresholds in `core/config/aspirations.yaml` →
`modifiable.convention_learning`:

| Parameter | Default | Bounds |
|---|---|---|
| `auto_apply_confidence` | 0.8 | 0.6 – 1.0 |
| `max_steps_per_convention` | 8 | 4 – 12 |
| `cooldown_goals` | 10 | 5 – 20 |

Promotion outcomes:

- `confidence ≥ auto_apply_confidence` AND target `< max_steps_per_convention`
  AND no overlap with existing step → **applied** (Edit target .md,
  append at appropriate position, retire subsumed guardrails, stamp
  `applied_date`)
- `confidence ≥ auto_apply_confidence` AND target at capacity →
  **pending_capacity** (flagged for user attention)
- Proposed step semantically overlaps an existing step → **rejected**
- Below threshold → stays **pending** until reinforced or manually actioned

Reinforcement bumps confidence by +0.15 per matching proposal from
replay re-mining (same `target` and semantically-similar `proposed_step`).
Proposals that never get reinforced stay pending indefinitely — manual
intervention (user applies, rejects, or files a Maintain goal to edit the
convention directly) closes out stale pending entries.

## Cross-references

- `.claude/rules/domain-free-examples.md` — the core-agnosticism rule this
  convention supports
- `.claude/rules/forged-skill-resolution.md` — Pattern C detail
- `core/scripts/load-conventions.sh` — batch convention loader used by Pattern B
- `core/scripts/_paths.sh` — sets `$WORLD_DIR` used in existence checks
- `world/conventions/convention-changes.jsonl` — mutation audit trail
- `.claude/skills/aspirations-execute/SKILL.md` Phase 3.9, 4.2 — canonical
  Pattern B call sites
- `.claude/skills/verify-learning/SKILL.md` Step 1 — canonical Pattern A site
