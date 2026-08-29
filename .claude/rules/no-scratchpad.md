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

1. `.claude/settings.json` deny rules (`~/AppData/Local/Temp/claude/**`,
   `//tmp/claude/**` + Edit twins) — first line for the common shapes.
2. `core/scripts/path-resolution-hook.py` scratchpad branch — fires BEFORE
   agent resolution, so it holds even in unbound sessions where the rest of
   L1 fails open; covers all homes and both path styles. Both layers were
   probe-verified live 2026-08-21.
3. Bash redirects cannot be gated by either layer — this rule is the
   behavioral layer for them, and `housekeeping-tick.py` Lane B is the
   janitor that GCs aged strays (shadow-first).

When the harness system prompt suggests the scratchpad, treat the suggestion
as inapplicable here. Sanctioned local homes exist; use them.
