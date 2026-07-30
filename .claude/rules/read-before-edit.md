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
   `context-reads.py check-file`) and, when the target has not been Read, emits
   the advisory on two channels: a stderr banner (what a human watching the
   terminal sees) and a structured `permissionDecision: "allow"` payload, which
   is the only channel that reaches the model. It never denies and never blocks
   — seeing the advisory means stop and Read the file first.

   **Two distinct advisories, matching Rule 1's conditional** (g-115-3747).
   "has not been Read this session" means no read of any kind was recorded — Read
   it. "was Read only in part this session (ranged read)" means the file WAS
   opened with offset/limit/pages, so Rule 1's "count only if they cover the
   region being edited" is now yours to evaluate: if your ranged read covered the
   region you are about to change, proceed; if not, read that region first. Until
   g-115-3747 ranged reads were discarded by the recorder outright, so they
   produced the *first* message — a false claim, fired on every large file, which
   is exactly the file whose advisory most needs to be believed. Note the gate
   deliberately does NOT go silent on a ranged read: silence would assert full
   context the manifest cannot vouch for, trading a false alarm for a false
   all-clear.

   **It did nothing at all from 2026-05-30 to 2026-07-28** (g-115-3731). Two
   independent defects, either sufficient alone: it bailed on an unset
   `MIND_AGENT`, which PreToolUse[Edit] never provides, so it exited before its
   own check on every real invocation; and it wrote only to stderr, which a
   non-blocking PreToolUse hook cannot deliver to the model (guard-1680). It
   hand-tested green the whole time, because a hand-run shell HAS `MIND_AGENT`
   set — the only environment where it failed was the only environment where it
   ran. If you are reading a version of this rule dated before that fix, it was
   describing a net that was not there. Both defects are now mutation-proofed by
   production-shape tests in `core/scripts/tests/test_pre_edit_context_gate.py`.

   **On Windows the 2026-07-28 revival did not take effect until 2026-07-29**
   (g-115-3820). Three further defects, each Windows-only and each silent, kept
   the gate 100% inert on this platform after it was declared fixed — so for one
   more day the paragraph above was still describing a net that was not there,
   just on fewer boxes. (1) The cheap path pre-filter added the SAME DAY as the
   revival matched forward-slash globs, but Claude Code sends `file_path` in
   native form, so every backslashed Windows path fell through to `*) exit 0` —
   a false REJECT on 100% of Windows edits, violating that pre-filter's own
   stated invariant, and killing the gate in BOTH the hand-test and production
   shapes. (2) `source _platform.sh` ran before agent resolution; it exports
   `MSYS_NO_PATHCONV=1`, under which `session-binding-read.sh` resolves to empty
   on Git Bash (g-304-19) — the three sibling context-reads hooks already carry
   the ordering fix and its comment, and this gate was the family member that
   missed it. (3) `tree-write-fence.sh` redirected to `/dev/stderr`, which does
   not resolve when stderr is a pipe, so `|| exit 0` ate the entire fence
   invocation under every captured-output caller.

   The through-line worth carrying: all three hand-tested green for the same
   reason the original 59-day inertia did — an interactive shell has no
   `MSYS_NO_PATHCONV`, a terminal has a real `/dev/stderr`, and a hand-typed
   path uses forward slashes. **The only environment where the gate failed
   remained the only environment where it actually ran.** A green suite on one
   OS is not evidence a hook works; this gate has now been declared fixed twice
   while inert. Treat platform as part of the production shape (guard-920), and
   record the box and OS with any claim that a hook is live.

   The gate fires ONLY for the path classes the context-reads manifest
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

   Two further exclusions, both silent and both deliberate. A cheap bash
   path pre-filter short-circuits out-of-scope paths before any subprocess
   spawn, so the gate adds no measurable cost to an edit it will not act on
   (measured cc-05: 53ms before the fix, 55ms after, on an out-of-scope path;
   ~156ms in-scope, paid only where the gate does its job). And the
   constitutional anchor (`.claude/settings.local.json`,
   `settings-structural-validator.{py,sh}`) is excluded outright — the payload's
   `allow` short-circuits the permission system, and the anchor must never
   receive one.

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
