<!-- domain-leak-exempt: deployment names are functional (promotion chain between Mind instances) -->
# Rationale: Pre-Overwrite Drift Gate

Referenced from `.claude/rules/promotion-cycle.md` § Pre-Overwrite Drift Gate.
This file holds the narrative, the measured drift, and the gate's scope/exclusion
rules, so the always-loaded rule can carry the imperatives alone.

## Why the gate exists

Before overwriting ANY downstream repo (any framework file-copy between Mind
deployments), the target may LEAD the source — ZDS self-evolves the framework
during operation while Claude-Mind lags — so a blind mirror would silently
DELETE or CLOBBER target-ahead improvements. Confirmed 2026-06-24: ZDS led
Claude-Mind on 18 framework files — 8 scripts, 2 architecture conventions, the
M-4/M-5 memory tests.

## Invocation

```bash
bash core/scripts/promotion-preflight.sh --source <incoming_repo> --target <repo_to_overwrite>
# add --strict to also block on every differing framework file
```

- **Exit 0** — target framework is a subset of source. Safe to promote.
- **Exit 2** — DRIFT. The gate lists every **orphan-risk** (target-only) and
  **target-ahead** (differing) framework file. For EACH: back-port it UP to the
  source (or explicitly discard with sign-off) **before** the overwrite. Never
  promote past an exit-2 without resolving it.

## Gate scope and exclusions

The gate compares only framework paths (`core/config`, `core/scripts`,
`.claude/{skills,rules}`, `CLAUDE.md`, `settings.json`, `mind_api/{src,tests}`);
it auto-excludes build artifacts (`__pycache__`, `*.pyc`, `.python-shim`,
`_tmp_*`) and buckets domain forged skills + deployment-local files separately
so they never count as drift. Read-only; safe anytime. Tests:
`core/scripts/tests/test_promotion_preflight*.py`.

## Cross-references

- `.claude/rules/promotion-cycle.md` — the imperatives this file explains
- `core/config/conventions/promotion-runbook.md` — the full push procedure
- `core/config/conventions/pull-promotion.md` — downstream pull procedure
