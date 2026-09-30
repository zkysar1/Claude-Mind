---
description: "Cite or tag every entity-fact added to a knowledge node: an in-session source token, or [UNVERIFIED]. A name-only citation is not one."
paths:
  - "world/knowledge/tree/**"
  - "**/knowledge/tree/**"
---

# Ground-Truth Citation

## Principle

A model prior and a retrieved fact are indistinguishable once written down; the
difference is observable **only at write time**, while the session still
remembers what it fetched. That is why this rule fires here, not at close.

Sibling of `verify-before-assuming.md` § "Positive File-State Claims": that one
governs claims about a FILE, this one claims about the WORLD — positive (rules
1-5) and, for the framework's own machinery, negative (rule 6).

## Rules

1. **Cite or tag.** Every entity-bearing assertion you ADD to a knowledge node
   carries an in-session source token or the tag `[UNVERIFIED -- model prior]`.
   Entity-bearing: names a proper noun, year, quantity or currency amount AND
   asserts something about it.

2. **Four things count as a source token**: a URL, a knowledge-tree node key, a
   board `msg-` id, or a `g-NNN-NN` goal id. Nothing else.

3. **A publication name is not a source.** "According to the Quarterly
   Industrial Review" attributes without citing — the shape of the g-012-02
   incident (convention § "What the incident was").

4. **A citation you did not fetch this session is DECORATIVE**, and worse than
   none: it reads as verified downstream. Cite what you opened; a URL carried
   from memory is tagged `[UNVERIFIED]`.

5. **Tagging is not a defeat.** `[UNVERIFIED -- model prior]` keeps a useful
   recollection writable while telling the next reader what it is. The failure
   this rule prevents is the unmarked prior, not the prior.

6. **A negative claim about the framework is not exempt.** A "the Mind has no
   X" row written while encoding an external source is a capability-absence
   verdict on this framework's machinery, and no source token can back an
   absence. Run `core/config/conventions/exhaustive-search-before-negation.md`
   (2+ independent surfaces) BEFORE the write and put the trail in the row
   (`RELATIVES: <paths>` or `searched A, B — absent`). Twin: guard-7544.
   2026-09-29: 2 of 2 such rows in one encode batch were wrong.

## Anti-patterns

- Writing a confident figure from recollection because it "sounds right"
- Attributing to a publication, institution or report name instead of a locator
- Pasting a URL you have not opened in this session
- Adding `[UNVERIFIED]` to a claim you DID retrieve — the tag means "not
  verified", not "hedged", and over-use makes it unreadable

## Enforcement

`core/scripts/ground-truth-citation-gate.sh` — ADVISORY PreToolUse hook on
Write/Edit/MultiEdit (`.claude/settings.json`); inspects only ADDED text, never
blocks; `GROUND_TRUTH_CITATION_GATE=refuse` escalates to deny. A backstop —
these rules are the guarantee.

## Cross-references

- `core/config/conventions/ground-truth-citation.md` — mechanism, the incident,
  gate policy, file map (`load-conventions.sh ground-truth-citation`)
- `.claude/rules/verify-before-assuming.md` — the sibling positive-claim classes
- g-357-42 (the close-time half), g-357-43 (the provenance ledger this reads)
