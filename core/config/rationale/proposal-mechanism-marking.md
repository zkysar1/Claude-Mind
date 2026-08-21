# Rationale: Proposal Mechanism Marking (PROBED | INFERRED)

Referenced from `.claude/skills/encode-session/SKILL.md` Phase Final. Why
every forward-looking proposal must mark its MECHANISM as probed or inferred.

## Why the split exists

A proposal carries two separable claims: the ACTION ("recategorize guard-X")
and the MECHANISM that justifies it ("because a domain category
under-retrieves it"). The mechanism is a factual assertion about how the
system behaves, and it can be FALSE while the action is still right — so it
needs evidence in its own right, not the action's plausibility standing in
for it.

## The canonical incident (2026-07-26)

A proposal shipped as "guard-NNN is filed under a product-specific category …
a domain category on a domain-agnostic rule **under-retrieves it**". When the
user asked to improve it, the probe returned that guardrail at rank 12 of 40
on free-text retrieval — the stated mechanism was false. The real loss was
narrower (the token-overlap fallback fires ONLY when the category match
returns empty, so an *exact-category* read never reaches it), and the
recategorization was still correct. One bullet of unprobed causal reasoning
had been shipped in the same register as measured findings.

## Relation to communication-clarity rule 6

`communication-clarity.md` rule 6 governs verify summaries and reports —
claims about what HAPPENED. This extends the same standard to
forward-looking proposals, which rule 6 does not name: an unprobed mechanism
presented in the same voice as a measured one erodes exactly the signal the
reader needs to decide what to accept.

## Cross-references

- `.claude/skills/encode-session/SKILL.md` Phase Final — the consumer
- `.claude/rules/communication-clarity.md` rule 6 — the sibling standard
  for backward-looking claims
- `.claude/rules/verify-before-assuming.md` — causal attribution claims
  (the same discipline at the evidence layer)
