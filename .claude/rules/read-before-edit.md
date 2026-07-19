# Read Before Edit

## Principle

Before any Edit, MultiEdit, or Write on an existing file, confirm you have Read
that specific file in the current session (or retrieved the relevant knowledge
category this turn). Editing from memory of a prior session, from a summary, or
from training-data priors produces stale-context errors that are invisible at
write time and expensive to diagnose later.

This is the BEFORE complement to two existing rules:
- `verify-before-assuming.md` gates CLAIMS (post-hoc: "did I verify what I asserted?")
- `pre-completion-review.md` gates COMPLETION (post-hoc: "did I re-read what I wrote?")

This rule gates the EDIT itself (pre-hoc: "do I have current context for what I
am about to change?").

## When To Apply

Every time, in every mode (reader/assistant/autonomous). The rule is mode-agnostic
because the failure mode is mode-agnostic: stale context causes bad edits regardless
of whether the agent is user-directed or self-directed.

Applies to:
- **Edit / MultiEdit** on any existing file
- **Write** that overwrites an existing file (new-file writes are exempt)
- **State-changing Bash** on an existing artifact (e.g., `sed -i`, `python3 -c`
  that modifies a file in place)

## Rules

1. **Read the file first**: Before invoking Edit, MultiEdit, or overwriting Write
   on a file, verify that you have Read that specific file in the current session.
   If you have not, Read it now. Partial reads (offset/limit) count only if they
   cover the region being edited.
2. **Retrieval counts for knowledge files**: For knowledge tree nodes, convention
   files, and reasoning bank entries, a `retrieve.sh` call that returned the
   relevant category in the current turn satisfies the context requirement. You
   do not need to separately Read the individual `.md` file if retrieval already
   surfaced its content.
3. **Summaries are not context**: Post-autocompact summaries describe what a prior
   session INTENDED. They are not a substitute for reading what is on disk right
   now. Treat every carried-forward claim about file contents as a hypothesis
   requiring fresh evidence — per `verify-before-assuming.md` "Summaries are claim
   snapshots, not filesystem snapshots."
4. **The automated safety net is PARTIAL — Rules 1-3 are the real guarantee**:
   `core/scripts/pre-edit-context-gate.sh` is wired as a PreToolUse[Edit|MultiEdit]
   advisory hook. It consults the session's context-reads manifest (via
   `context-reads.py check-file`) and prints a stderr advisory when the target has
   not been Read. The warning is advisory (never blocks) — seeing it means stop
   and Read the file first.

   But the gate fires ONLY for the path classes the context-reads manifest
   advisory-tracks: `core/config/**`, `.claude/skills/**`,
   `world/knowledge/tree/**`, `world/conventions/**`, `aspirations-compact.json`,
   and — since g-115-2210 — `core/scripts/**` (framework code, the surface where
   loop self-evolution lands). For everything else — `.claude/rules/**`,
   `agents/<agent>/**` (including `self.md`), and all product-code / external
   files — the gate stays **silent by design**: a read of those is never
   recorded in the manifest, so a "has not been Read" warning there would be a
   guaranteed false positive that desensitizes you to the banner. The absence of
   a warning is therefore NOT evidence you have current context. Rules 1-3 are
   honor-system for the out-of-scope majority — the gate backstops only the
   trackable subset.

   Scope-split caveat (g-115-2210): `core/scripts/**` is *advisory-only*. Its
   reads are recorded and the edit advisory fires there, but the separate
   PreToolUse[Read] re-read dedup gate (which BLOCKS a redundant whole-file
   re-read) keeps the NARROWER pre-2210 scope — so a mandated whole-file
   re-verify of a script after a linter/user touch (verify-before-assuming.md)
   is never refused as "already in context." The `is_in_scope` (narrow, dedup)
   vs `is_in_scope_advisory` (wide, recorder+advisory) split in
   `context-reads.py` is the single source of truth for this.

## Anti-patterns

- Editing a SKILL.md from memory of "what I wrote last session" without re-reading
- Applying a fix to a script based on a stale-context line number that has drifted
- Writing to a config file based on a summary's claim about its current contents
- Ignoring the `[pre-edit-context-gate] ADVISORY` warning and proceeding anyway
- Using Write to overwrite a file whose current contents you have not verified

## Cross-references

- `.claude/rules/verify-before-assuming.md` — gates claims; this rule gates edits
- `.claude/rules/pre-completion-review.md` — re-read AFTER editing; this rule reads BEFORE
- `core/scripts/pre-edit-context-gate.sh` — automated advisory hook (PARTIAL
  backstop — covers the trackable subset only; see Rule 4)
- `core/scripts/context-reads-record.sh` — PostToolUse[Read] hook that records reads
- `core/scripts/context-reads.py` — context-reads tracker engine
