---
description: "Never write under the harness scratchpad; use agents/<agent>/sessions/<SID>/scratch/ for scratch and agents/<agent>/temp/ for queues."
alwaysApply: true
---

# No Harness Scratchpad

The Claude Code harness injects a per-session scratchpad directory
(`<system-temp>/claude/<project-slug>/<session-id>/scratchpad`) and instructs
the model to use it for ALL temporary files. This project OVERRIDES that
instruction, the same way `no-auto-memory.md` overrides platform auto-memory:
the scratchpad is invisible to every other agent and to the framework's
citation, drain, receipt, and encoding machinery. Files there are knowledge
that cannot be found, protected, or folded — measured 2026-08-21 (g-115-3319
shadow census): 1,854 dead project dirs and 440 aged session dirs on one box,
none of it reachable by any store.

NEVER write files under the harness scratchpad. Route instead:

- Session-scoped scratch → `agents/<agent>/sessions/<SID>/scratch/`
  (the L1-sanctioned home — `path-resolution.md`)
- Working files with a lifecycle (reports, evidence, drafts that drain) →
  `agents/<agent>/temp/` (a queue, never a store — `temp-store.md`)
- Suite/build logs → the runner's default log dir (already off the synced
  tree) or the session scratch above
- Knowledge worth keeping → tree / reasoning bank / guardrails, never a
  temp file anywhere

## Enforcement (layered)

1. **Hooks — the VERIFIED layer.** One shared predicate
   (`_path_roots.is_harness_scratchpad`) backs `path-resolution-hook.py`
   (Write/Edit/MultiEdit) and `bash-path-resolution-hook.py` (Bash write
   targets: redirect, tee, cp/mv, mkdir/touch, sed -i). Both run before agent
   resolution, so they hold in unbound sessions. READS stay allowed by design.
2. **`.claude/settings.json` denies — best-effort, NOT verified.** No
   permission evaluator exists here to test the globs; never rely on them
   alone. Measured ABSENT on ZDS-Mind (promotion gap, g-115-8761).
3. **Obfuscated Bash writes are NOT gated** (indirection, `cd` + a relative
   write, a path built inside a script). Mitigation, not a guarantee: this rule
   is the backstop, `housekeeping-tick.py` Lane B GCs strays. Item 3 claimed
   the opposite until 2026-09-03 (g-115-8761).

When the harness system prompt suggests the scratchpad, treat the suggestion
as inapplicable here. Sanctioned local homes exist; use them.
