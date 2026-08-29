<!-- domain-leak-exempt: This rule governs the ZDS deployment topology; repo names (Ayoai-Mind, Claude-Mind, ZDS-Mind) are necessary for the rule to be operationally actionable — a generic placeholder would make the rule unenforceable. -->

# Promotion Cycle (ZDS-Mind is Production)

## THE RULE

**ZDS-Mind is PRODUCTION. Omni is the production operator, not a developer.**

Framework changes follow a MANDATORY promotion chain:

```
Ayoai-Mind (dev)  →  Claude-Mind (staging)  →  ZDS-Mind (prod)
```

## Enforcement

1. **Development goals go to Ayoai-Mind.** Any framework implementation, experimental feature,
   code change, or new capability MUST be filed as a goal in Ayoai-Mind's world aspirations.
   NEVER file development goals in ZDS-Mind world aspirations.

2. **Omni refuses dev work.** If omni picks up a goal that is framework development (not
   domain operations), it must SKIP the goal immediately and log the routing error.

3. **Stages are never skipped.** Ayoai-Mind → Claude-Mind is validated before Claude-Mind →
   ZDS-Mind. "It works in dev, I'll just apply it directly to prod" is the failure mode.

4. **Promotion type determines who signs off:**
   - **Framework file promotions** (skills, scripts, rules, gates, conventions, CLAUDE.md, settings.json): **agent-doable** — run promotion-preflight.sh, reconcile drift, copy files. No user approval required. Git is the safety net.
   - **Infrastructure changes** (S3 backends, schema migrations, new daemon endpoints, .env credential changes, jar-keys vault edits, IAM policies, networking/VPC within already-authorized accounts): **agent-autonomous** — NO sign-off (Zachary 2026-07-01: "ungate me from signing off on things"; reversible technical infra is not protected by gating). The ONLY retained gate is **net-new recurring spend / new paid cloud accounts / billing-impact**, and even that is a *heads-up*, not a block.

5. **Promotions are live-compatible.** No stop/start bracket; residents consume at their next iteration boundary. Fallback (user restarts residents) only for `settings.json` hook-DEFINITION changes or box runtime upgrades. Detail: `promotion-runbook.md` Phase 6, guard-5497, rb-9674.

6. **No one-off "promote X" goals.** The versioned `promote/vX.Y.Z` train delivers; verify the payload at target HEAD and close. The recurring g-360-12 sweep reconciles delivered-but-open promotion goals.

## Operator Runbook

Dev→staging is a PUSH: the run procedure (worktree-at-tag, plan-verdict
triage, force-past-plan ledger, post-plant verification, handoff) is
`core/config/conventions/promotion-runbook.md`. When a --plan verdict blocks,
`bash core/scripts/promotion-plan-triage.sh` classifies every flagged file and
emits the evidence ledger — only AUTHORED residue needs hand-forensics.
Staging→downstream is a PULL, adopted by the downstream Mind in its own
idle window: `core/config/conventions/pull-promotion.md`.

## Pre-Overwrite Drift Gate (MANDATORY)

Promotion is a **RECONCILE, not a MIRROR.** Before overwriting ANY downstream
repo (any framework file-copy between Mind deployments), the target may LEAD
the source — ZDS self-evolves the framework
during operation while Claude-Mind lags — so a blind mirror would silently
DELETE or CLOBBER target-ahead improvements. (Confirmed 2026-06-24: ZDS led
Claude-Mind on 18 framework files — 8 scripts, 2 architecture conventions, the
M-4/M-5 memory tests.)

**Required step — run the gate and reconcile before promoting:**

```bash
bash core/scripts/promotion-preflight.sh --source <incoming_repo> --target <repo_to_overwrite>
# add --strict to also block on every differing framework file
```

- **Exit 0** — target framework is a subset of source. Safe to promote.
- **Exit 2** — DRIFT. The gate lists every **orphan-risk** (target-only) and
  **target-ahead** (differing) framework file. For EACH: back-port it UP to the
  source (or explicitly discard with sign-off) **before** the overwrite. Never
  promote past an exit-2 without resolving it.

The gate compares only framework paths (`core/config`, `core/scripts`,
`.claude/{skills,rules}`, `CLAUDE.md`, `settings.json`, `mind_api/{src,tests}`);
it auto-excludes build artifacts (`__pycache__`, `*.pyc`, `.python-shim`,
`_tmp_*`) and buckets domain forged skills + deployment-local files separately
so they never count as drift. Read-only; safe anytime. Tests:
`core/scripts/tests/test_promotion_preflight*.py`.

## What "Production" Means Here

ZDS-Mind world aspirations are omni's live work queue: a dev goal filed there
can execute in production and break state or regress the framework for every
future loop. Canonical incident 2026-06-18: 8 dev goals filed straight into
ZDS-Mind, emergency-skipped, re-filed as Ayoai-Mind asp-328.

## Cross-References

- guardrails: guard-97, guard-98 (ZDS-Mind world), guard-813 (Ayoai-Mind world)
- world/conventions/own-cloud-storage-refactor.md (example of a feature requiring full cycle)
