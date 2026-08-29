---
description: "Read the file (or retrieve the node) this session before any Edit, Write or sed -i; summaries are not context; the hook covers few paths."
alwaysApply: true
---

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
   `core/scripts/pre-edit-context-gate.sh` is a PreToolUse[Edit|MultiEdit]
   ADVISORY hook — it never denies and never blocks. It consults the session's
   context-reads manifest and emits one of two advisories as a structured
   `permissionDecision: "allow"` payload (the only channel that reaches the
   model): "has not been Read this session" → Read it; "was Read only in part
   this session (ranged read)" → Rule 1's "count only if they cover the region
   being edited" is now yours to evaluate. Seeing either means stop and read.
   It fires ONLY for the path classes the manifest tracks (`core/config/**`,
   `.claude/skills/**`, `world/knowledge/tree/**`, `world/conventions/**`,
   `aspirations-compact.json`, `core/scripts/**` advisory-only); for
   `.claude/rules/**`, `agents/<agent>/**` and all product/external files it is
   **silent by design**, so the absence of a warning is NOT evidence you have
   current context. It was inert for 59 days (2026-05-30 → 07-28) and one more
   day on Windows, hand-testing green the whole time — a hook's production
   shape (env, platform, path form) is not your shell's; history, the
   scope-split (`is_in_scope` vs `is_in_scope_advisory` in `context-reads.py`
   is the SSOT), the constitutional-anchor exclusion and the cost measurements:
   `core/config/conventions/retrieval-triggers.md` § "The pre-edit context gate".

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
- `core/config/conventions/retrieval-triggers.md` — G14 + the gate's full
  history and scope (moved from rule 4)
