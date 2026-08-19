# Constitutional Rings Convention

Three-ring governance model for multi-agent coordination, mapping the framework's
existing permission structure to a formal constitutional hierarchy. Inspired by
arXiv 2603.28990 (Dochkina): "The closer to 'why,' the more human control;
the closer to 'how,' the more system autonomy."

## Ring 0 — The Constitutional Anchor (tamper-proof; no autonomous path)

The single surface the agent CANNOT modify by ANY autonomous path — not
post-notification, not propose-via-pending-questions, not at all. Enforced
structurally (not honor-system) by a self-referential permission deny.

| File | Why anchored |
|------|--------------|
| `.claude/settings.local.json` | Holds the deny rules that make every other tier's permission model real. A self-referential `deny: Edit/Write/MultiEdit(**/.claude/settings.local.json)` *inside this file* makes it immutable to every session — deny beats allow across all tiers, hot-reloaded (linchpin-verified 2026-05-16). This in-repo deny is the entire mechanism. An out-of-repo mirror in `~/.claude/settings.json` would be optional extra hardening, but nothing in the repo provisions it — the persistent tripwire advisory A1 is that expected state, not drift. **A1's predicate is a missing DENY, not a missing FILE**: `anchor-tripwire.py:96` tests the deny list for `settings.local.json`, so file-absent and file-present-carrying-no-deny emit the IDENTICAL advisory and A1 cannot distinguish them. This row read "measured absent on every box" until 2026-08-11. The file is PRESENT on **all six boxes ever measured** — cc-05 67 bytes (`theme`, `skipDangerousModePermissionPrompt`), cc-03 93 bytes (those plus `effortLevel`), cc-04 93 bytes, cc-08 48 bytes, cc-07 48 bytes, cc-02 67 bytes (same two keys as cc-05, 2026-08-16, g-115-831) — client-managed keys only, no `permissions` block and zero deny rules on any, with A1 firing regardless. The byte counts CHURN, and a change in one is not movement on the anchor: cc-08 measured 81 bytes at 2026-08-14T05:00 and 48 bytes at 11:56 the same day (a client-managed `model` key removed), while A1's predicate never reads those keys. CLAUDE.md carried the identical sentence: one claim copied twice, not two measurements agreeing (guard-2283). The module docstring (`anchor-tripwire.py:21-23`) was correct throughout — "carries the mirror" / "a missing mirror", never "the file is absent" — so both prose copies had drifted from the code they describe.

**How a false claim outlived a correct measurement of the same fact (guard-2978).** The author who wrote "absent on every box" into both docs had *already measured the file present at 93 bytes* and recorded that in the same pass, in `pq-anchor-mirror-disposition`'s `context` field. Truth and falsehood were written concurrently into different stores, and the store nobody greps kept the truth. So this was never an inference error from A1 — the measurement was correct and simply did not reach the prose. Treat a disagreement between two records from the same author as a prompt to measure, never to prefer the later or the more prominent one. The disposition below is unaffected; only do not infer file-absence from A1. Do NOT create one (g-115-3676): that file is client-managed and rewritten wholesale, so a hand-added deny is silently erased, and it falls outside the deny patterns above, so an agent-written mirror is agent-erasable — it would read as a second independent copy while providing none of the protection. |
| `core/scripts/settings-structural-validator.{py,sh}` | The fail-closed PreToolUse hook that protects `settings.json`'s deny[]/hooks. rb-931: a meta-safeguard whose substrate is in scope for self-edit is no safeguard. Anchored so the agent cannot neuter it. |

**Modification authority**: NONE autonomously. Changes require a deliberate
user-authorized maintenance path. This friction is the entire point — the
anchor is what lets Rings 1–3 be *more* permissive safely, because the floor
cannot be removed from inside the loop. Origin: g-115-732 loosened the
deny-list so the loop self-evolves the framework, creating a bootstrap
paradox (rb-931); the anchor resolves it.

## Ring 1 — Core Mission (user owns intent; agent edits post-notification)

These define WHY the system exists. Only the user may modify them.

| File | Purpose |
|------|---------|
| `world/program.md` | The Program — shared purpose statement |
| `agents/<agent>/self.md` | Agent identity, core capabilities, operating principles |
| `core/config/conventions/` | Framework conventions (the rules of the game) |
| `core/config/modes/` | Mode behavioral rules |
| `.claude/rules/` | Agent behavioral rules |

**Modification authority**: The user OWNS the *intent* of Ring 1 (the "why").
The agent MAY edit these files directly — the deny-list was loosened
(2026-05-14, g-115-732) so the loop self-evolves the framework; git-tracking
+ loop-commit are the safety net. Material changes are **post-notification,
revert-if-wrong** (per `guard-380`, the self.md evolution model), NOT
pre-approval via pending-questions — that earlier gate is superseded. The
hard floor the agent genuinely cannot cross is **Ring 0**, not Ring 1.
(This line previously read "User only … MUST NOT write directly"; that was
stale relative to g-115-732 + guard-380 and was the same behavioral-layer
split-brain that caused the g-115-792 user-gated-goal spam.)

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
| `agents/<agent>/developmental-stage.yaml` | Agent's mutable state (epsilon, competence) |
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
