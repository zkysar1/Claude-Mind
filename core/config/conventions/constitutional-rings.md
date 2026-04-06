# Constitutional Rings Convention

Three-ring governance model for multi-agent coordination, mapping the framework's
existing permission structure to a formal constitutional hierarchy. Inspired by
arXiv 2603.28990 (Dochkina): "The closer to 'why,' the more human control;
the closer to 'how,' the more system autonomy."

## Ring 1 — Immutable Core Mission (human-only modification)

These define WHY the system exists. Only the user may modify them.

| File | Purpose |
|------|---------|
| `world/program.md` | The Program — shared purpose statement |
| `<agent>/self.md` | Agent identity, core capabilities, operating principles |
| `core/config/conventions/` | Framework conventions (the rules of the game) |
| `core/config/modes/` | Mode behavioral rules |
| `.claude/rules/` | Agent behavioral rules |

**Modification authority**: User only (via direct edit or `/respond` directive).
Agent may propose changes via pending-questions queue but MUST NOT write directly.

## Ring 2 — Standards and Metrics (human + system modification)

These define WHAT the system measures and enforces. System proposes; humans approve
or the evolution engine modifies with audit trail.

| File | Purpose |
|------|---------|
| `core/config/aspirations.yaml` | Aspiration caps, scopes, scoring weights |
| `core/config/evolution-triggers.yaml` | When evolution fires |
| `core/config/developmental-stage.yaml` | Stage definitions and progression |
| `core/config/curriculum.yaml` | Curriculum stage definitions |
| `world/guardrails.jsonl` | Safety rules and behavioral constraints |
| `world/reasoning-bank.jsonl` | Learned reasoning patterns |
| `world/conventions/*.md` | Domain-specific conventions |

**Modification authority**: User + agent evolution engine. Changes logged to
`decisions` board channel for user review. Evolution engine may modify scoring
weights and thresholds; guardrail additions require user confirmation.

## Ring 3 — Protocols and Parameters (fully autonomous)

These define HOW the system operates. Agents tune freely with rollback via
strategy archive.

| File | Purpose |
|------|---------|
| `meta/goal-selection-strategy.yaml` | Goal scoring weight preferences |
| `meta/aspiration-generation-strategy.yaml` | Generation heuristics |
| `meta/reflection-strategy.yaml` | Reflection mode preferences |
| `meta/evolution-strategy.yaml` | Evolution parameters |
| `meta/encoding-strategy.yaml` | Knowledge encoding preferences |
| `<agent>/developmental-stage.yaml` | Agent's mutable state (epsilon, competence) |
| Board communication patterns | Channel usage, posting frequency |
| Batching thresholds, context budgets | Operational parameters |

**Modification authority**: Fully autonomous via evolution engine (`/aspirations-evolve`).
Changes logged to `meta/evolution-log.jsonl`. Strategy archive (`meta/strategy-archive.yaml`)
enables rollback if metrics regress.

## Governance Principle

Ring 1 errors cascade system-wide — human gatekeeping prevents catastrophic drift.
Ring 2 changes are medium-risk — audit trail + user review catches regressions.
Ring 3 changes are low-risk — A/B comparison via strategy archive enables safe experimentation.

When evaluating a proposed change, first classify it by ring. If uncertain, treat it
as the higher (more restrictive) ring.
