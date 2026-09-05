---
description: "Cite or tag every entity-fact added to a knowledge node: an in-session source token, or [UNVERIFIED]. A name-only citation is not one."
paths:
  - "world/knowledge/tree/**"
  - "**/knowledge/tree/**"
---

# Ground-Truth Citation

## Principle

A model prior and a retrieved fact are indistinguishable once written down.
They differ only in whether a retrieval actually happened — and that difference
is observable **only at write time**, while the session still remembers what it
fetched. An hour later the information is gone, which is why no downstream
reviewer can reconstruct it and why this rule fires here rather than at close.

Sibling of `verify-before-assuming.md` § "Positive File-State Claims": that one
governs positive claims about a FILE, this one positive claims about the WORLD.

## Rules

1. **Cite or tag.** Every entity-bearing assertion you ADD to a knowledge node
   carries either an in-session source token or the explicit tag
   `[UNVERIFIED -- model prior]`. Entity-bearing means it names a proper noun,
   year, quantity, or currency amount AND asserts something about it.

2. **Four things count as a source token**: a URL, a knowledge-tree node key, a
   board `msg-` id, or a `g-NNN-NN` goal id. Nothing else.

3. **A publication name is not a source.** "According to the Quarterly
   Industrial Review" attributes without citing — it is the exact shape of the
   coach g-012-02 incident, where plausible-sounding attribution carried six
   substituted identities into a GREEN close.

4. **A citation you did not fetch this session is DECORATIVE**, and worse than
   none: it reads as verified to every downstream reader. Cite what you actually
   opened. If you are carrying a URL from memory, tag it `[UNVERIFIED]` instead.

5. **Tagging is not a defeat.** `[UNVERIFIED -- model prior]` is a first-class
   outcome — it keeps a useful recollection writable while telling the next
   reader exactly what it is. Reach for it whenever retrieval is not worth the
   cost; the failure this rule prevents is the unmarked prior, not the prior.

## Anti-patterns

- Writing a confident figure from recollection because it "sounds right"
- Attributing to a publication, institution or report name instead of a locator
- Pasting a URL you have not opened in this session
- Adding `[UNVERIFIED]` to a claim you DID retrieve (the tag is not a hedge —
  it means "not verified", and over-using it makes it unreadable)

## Enforcement

`core/scripts/ground-truth-citation-gate.sh` — an ADVISORY PreToolUse hook on
Write/Edit/MultiEdit, registered in `.claude/settings.json`. It inspects only
the text being ADDED, and never blocks (`permissionDecision: "allow"`);
`GROUND_TRUTH_CITATION_GATE=refuse` escalates it to a deny once a box has
measured its false-positive rate. Like every advisory gate, it is a backstop:
these rules are the guarantee, and the gate is silent outside its scope.

## Cross-references

- `core/config/conventions/ground-truth-citation.md` — mechanism: the two
  findings, why both an entity AND an assertion signal are required, the
  unreadable-manifest skip (guard-1760), channel policy, file map
  (`load-conventions.sh ground-truth-citation`)
- `.claude/rules/verify-before-assuming.md` — the sibling positive-claim classes
- g-357-42 (the close-time half), g-357-43 (the provenance ledger this reads)
