# Ground-Truth Citation (write-path gate)

Mechanism behind the one-line imperative in
`.claude/rules/verify-before-assuming.md` § "Ground-Truth Writes". The rule keeps
the imperative; everything a reader needs at the moment of *building* or
*tuning* the gate lives here.

Landed by g-357-45 as the WRITE-path half of the 2026-08-31 no-publish-from-memory
directive. The CLOSE-path half is g-357-42 (`close-review-gate.py` + the coach
fixture); the PROVENANCE ledger it reads is g-357-43 (`provenance-check.sh`,
`context-reads.py append_provenance / read_provenance`). Three separate pieces,
one directive — none of them subsumes the others, and the split matters: the
close gate can only catch a mangled artifact once it EXISTS, while this one fires
on the diff that would create it.

## What the incident was

Coach g-012-02: a goal enumerating 16 named entities closed GREEN with an
artifact carrying the right COUNT and six substituted identities — famous-name
priors displacing the real entities — attributed to a plausible publication name.
The verification criterion was count-based, so it passed. Nothing in the loop ever
compared the artifact's identities against a source.

The generalisation, and the reason this is a gate rather than a lesson: **a model
prior and a retrieved fact are indistinguishable once written down.** They differ
only in whether a retrieval happened, and that difference is observable ONLY at
write time, while the session still remembers what it fetched. An hour later the
information is gone, which is why no downstream reviewer can reconstruct it.

## The two findings

| kind | fires when | why it is not a lesser finding |
|---|---|---|
| `missing-citation` | a line carries BOTH an entity signal and an assertion signal, has no in-session source token, and is not tagged `[UNVERIFIED …]` | the coach shape |
| `decorative-citation` | the line HAS a source token, but the provenance manifest has no record of this session retrieving it | a citation nobody opened reads as verified to every downstream reader — strictly worse than none, so it is flagged at the SAME severity |

## The four source tokens

A URL, a knowledge-tree node key, a board `msg-…` id, or a `g-NNN-NN` goal id.
Anything else is prose.

**A bare publication name is deliberately NOT a source token.** This is the single
design decision the whole gate turns on: admit it and the gate passes the exact
write that motivated it, because the coach artifact's attribution was precisely a
plausible-sounding publication. Pinned by
`test_coach_shape_publication_name_only_source_is_caught`, with a positive control
beside it proving the same sentence goes clean once the source is retrievable —
without that control the assertion is equally satisfied by a gate that flags every
capitalised line.

## Why a candidate needs BOTH signals

An entity signal alone (proper-noun run, year, number+unit, currency) matches
cross-reference lists, headings and see-alsos, which are dense with proper nouns
and assert nothing. Requiring an assertion signal (copula, reporting verb,
comparison) alongside it is what keeps the gate off most knowledge-tree edits.

Better to under-flag than to spam: **a lint that fires on ordinary writes gets
switched off, and then its genuine findings are worth nothing.** Treat the
false-positive rate as the binding constraint, not the miss rate. Code fences and
YAML front matter are skipped for the same reason.

## Unreadable provenance SKIPS the decorative check

When the manifest cannot be read — or is empty — `_retrieved_predicate` returns
`None` and `analyze` skips the decorative check entirely. It does NOT treat every
citation as fetched. guard-1760: a checker must not report what it declined to
look at as a pass; the permissive default would turn an unreadable manifest into a
clean bill of health. The `missing-citation` half still fires, so an unknown
manifest degrades ONE check rather than disabling the gate.

Both no-manifest branches (absent/empty, and a read that RAISES) are pinned —
they are reached differently, and a mutation of either survived the first test
pass.

## Advisory, escalatable by env flag

`permissionDecision: "allow"` — the write proceeds and this can never wedge the
loop. `GROUND_TRUTH_CITATION_GATE=refuse` turns the same finding into a deny.

The escalation is an ENV FLAG rather than a code edit on purpose: the decision to
start refusing writes should be reversible per box, and made after the
false-positive rate has been measured in the field, not inferred at authoring
time. Before flipping it anywhere, measure — the escalation is the half of this
design with no evidence behind it yet.

## Scope

`world/knowledge/tree/**`, plus any file whose front matter carries
`ground_truth: true` (a domain's own opt-in, for briefs and reports that live
outside the tree). Everything else exits silently.

Only the ADDED text is inspected — `content` for Write, `new_string` for Edit,
every `edits[].new_string` for MultiEdit; never the file on disk. A gate that
scanned the whole file would flag inherited prose the caller did not write and
cannot fix, which is the shape that gets a gate switched off (see the
false-positive constraint above). All three tool shapes are pinned separately: a
gate wired to one of them is silent on the other two.

## Channel policy (do not "tidy" the wrapper)

The gate writes on TWO channels and needs both. **stdout** carries the structured
`hookSpecificOutput` — the only channel that reaches the model (guard-1680).
**stderr** carries the same text for the human terminal, which the structured
payload never reaches.

So `ground-truth-citation-gate.sh` deliberately does NOT redirect the python
call's stderr to `/dev/null`, unlike its sibling wrappers. Those suppress stderr
because they emit a deny on stdout and say nothing else; copying that policy
verbatim would mute half of this gate's output while leaving the source line
looking live (guard-2410 — a thin-wrapper template carries an output-channel
policy, and copying inherits it silently). The hazard suppression normally guards
— a traceback on stderr — is closed at its SOURCE instead: `main()` is wrapped so
any exception exits 0.

## Files

| path | role |
|---|---|
| `core/scripts/ground_truth_citation.py` | pure detection module — `analyze(text, retrieved=None)`, importable and testable with no hook plumbing |
| `core/scripts/ground-truth-citation-gate.py` | hook entry: stdin payload → scope → added text → `retrieved` predicate → both channels |
| `core/scripts/ground-truth-citation-gate.sh` | fail-open wrapper, registered in `.claude/settings.json` PreToolUse for Write/Edit/MultiEdit |
| `core/scripts/tests/test_ground_truth_citation_gate.py` | the three named outcomes, the coach shape, and the controls |

Registration is itself pinned by `test_the_gate_is_REGISTERED_in_settings_json`
— every other assertion in that file passes against a gate no hook ever invokes
(rb-9476: a scoped fix can be correct-looking and INERT).

## Cross-references

- `.claude/rules/verify-before-assuming.md` § "Ground-Truth Writes" — the imperative
- `core/config/conventions/negative-conclusions.md` — the sibling for NEGATIVE
  claims; this gate governs POSITIVE entity-facts, the mirror surface
- guard-1680 (which hook channel reaches the model), guard-1760 (a declined check
  is not a pass), guard-2410 (wrapper output-channel policy), rb-9476 (inert
  scoped fix), guard-4166 (a fix whose effect is silence needs a positive control)
- g-357-42 (close-path half), g-357-43 (provenance ledger), coach g-012-02
