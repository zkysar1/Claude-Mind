# Rationale: Code Review Protocol — Scope Beyond Framework Files

Referenced from `.claude/rules/code-review-protocol.md` step 4.

## Two measured extensions (product repos + infrastructure operations)

Step 4 was written for framework files, so work that did not LOOK like one
rode the honor system. Two lanes have been measured walking into rails that
were already encoded.

**Product/deployment repos** (2026-08-13, ZDS rb-1212): one merge missed a
guardrail naming the defect being fixed and the sibling PR that later
collided, plus another mandating the repo's pre-merge scanner. Run both
queries before designing a fix, opening a PR, or merging; for a merge the
MECHANISM query is the merge itself ("merging a PR to an auto-deploying
repo"), which surfaces the merge-readiness rails and repo scanners. Standing
merge grants untouched — no approval wait.

**Infrastructure operations** (2026-09-06, g-115-9291): invoking a service
directly, provisioning, cold-starting. The SUBJECT query ran; the
MECHANISM one ("invoking a service directly, bypassing its caller") was
skipped because this was infra, not a framework edit — walking into a
documented bypass that already carried two prior incidents.

Both share a shape: SUBJECT feels necessary, MECHANISM is what would have
fired. Honor-system: no gate counts these.

## Cross-references

- rb-1212 — the product-repo merge incident (ZDS)
- g-115-9291 — the infrastructure invocation incident
- `.claude/rules/code-review-protocol.md` — the rule this rationale supports
