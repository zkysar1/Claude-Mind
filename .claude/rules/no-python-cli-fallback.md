---
paths:
  - "core/scripts/**"
  - "mind_api/**"
---

<!--
  Path-scoped (g-115-6469). The rule's own "When This Applies" section names
  exactly two surfaces — authoring/editing a wrapper in core/scripts/*.sh, and
  implementing a daemon endpoint — so the trigger set is the governed set.

  Enforcement does not depend on this text being in context:
  core/scripts/check-no-python-cli-fallback.sh is a pre-commit gate that REFUSES
  the regression, and a 24h recurring audit greps the wrappers. See
  core/config/conventions/rules-loading.md.
-->

# No Python CLI Fallback

## Principle

As of 2026-05-14, 35 wrappers in `core/scripts/` were migrated to daemon-only
operation. The Python CLI fallback (`_fallback_exec`, `python3 -m core.scripts.*`,
`python3 core/scripts/<name>.py <subcommand>`) no longer exists in those wrappers.
Re-introducing any of these patterns is a regression that the Layer B pre-commit
gate (`core/scripts/check-no-python-cli-fallback.sh`) will refuse.

## When This Applies

- Authoring or editing any wrapper in `core/scripts/*.sh`
- Implementing a new daemon-aware tool or endpoint
- Reviewing wrapper code for correctness

## Rules

1. **Wrappers MUST exit 1 with `_no_daemon_error()` on rc=3 from `rt_call`.** No
   fallback to Python CLI. If the daemon is unreachable, the wrapper fails loudly
   with a diagnostic message on stderr.
2. **Auto-spawn happens once per call via `_runtime.sh`.** Failure to spawn is loud
   (stderr + exit 1). There is no silent degradation path.
3. **New endpoints are daemon-only from day 1.** Never plumb a Python CLI subcommand
   alongside a daemon endpoint. The CLI path does not exist to fall back to.
4. **Migration audits are continuous.** The Layer D recurring goal (24h interval,
   asp-115) greps wrappers for the regression patterns. Hits produce an Investigate
   goal.
5. **When the daemon misbehaves, fix the daemon or revert the cutover.** Do NOT add
   a CLI fallback as a "temporary fix." Temporary fallbacks become permanent drift.
6. **The Layer B pre-commit gate refuses commits introducing the regression.**
   `--no-verify` is not the right answer; fix the code instead.

## How To Recover If the Daemon Is Broken

- The cutover landed as a 4-commit sequence (chronological):
  `20a09b1` (drift defense docs) → `d3e5e6e` (daemon proactive spawn) →
  `25d6520` (Python CLI deletion + T2.1) → `f281ed8` (wrapper cutover).
- `git revert f281ed8 25d6520 d3e5e6e 20a09b1` — atomic rollback of the
  entire sequence (reverse chronological order so the working tree restores
  cleanly). Plus `e7960a9` (python-invocation doc) and any tree-node /
  self.md edits if you want to undo Stage 4 follow-up too.
- `git show 25d6520~1:<path>` — read the deleted CLI code at any point
  (the last commit before A4's deletion is `25d6520`'s parent).
- The detailed recovery story lives in the knowledge-tree node
  `world/knowledge/tree/system/daemon-only-architecture.md`.
- `git log -p --follow <deleted-path>` — full evolution of any removed file.
- BRD: `<deployment-local docs path>/2026-05-14-preparing-to-remove-python-cli.md`
  (kept off-repo by the original author; ask the deployment maintainer if
  the migration rationale is needed for audit purposes)
  contains the full rationale, audit results, and migration plan.

## Anti-patterns

- Adding `_fallback_exec` back into a wrapper "just in case the daemon is down."
- Restoring `cmd_*` functions in Python scripts that had their CLI entry points removed.
- Adding new flags or subcommands via `python3 core/scripts/<name>.py` instead of
  creating a daemon endpoint.
- Using `--no-verify` to bypass the pre-commit gate instead of removing the
  regression pattern.

## Cross-references

- `world/knowledge/tree/system/daemon-only-architecture.md` — Layer A.2 tree node
- `core/scripts/check-no-python-cli-fallback.sh` — Layer B pre-commit gate
- `core/BOUNDARY.md` — daemon/CLI boundary documentation
- BRD: `<deployment-local docs path>/2026-05-14-preparing-to-remove-python-cli.md`
  (kept off-repo by the original author; ask the deployment maintainer if
  the migration rationale is needed for audit purposes)
