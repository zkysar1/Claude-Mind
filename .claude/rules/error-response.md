# Error Response Imperative

After ANY infrastructure interaction (success or failure): check error alerts.
Never trust superficial success. Does NOT apply to local/tooling errors.

Blocker-centric model: try fix inline first. Unfixable problems become blockers via
CREATE_BLOCKER (blocker + unblocking goal, atomic). Three primitives: Unblock (HIGH),
Investigate (MEDIUM), Idea (MEDIUM).

Guardrail-driven enforcement: Phase 4.1 post-execution, Phase 0.5a pre-selection.

**Detail:** `core/config/conventions/infrastructure.md` for full protocol, rules, and scripts.
