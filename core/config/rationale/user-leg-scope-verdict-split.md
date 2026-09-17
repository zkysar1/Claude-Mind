# Rationale: the `undeclared` / `scope-unrecognized` verdict split

Referenced from `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5b.14
(reclaim lane P) and `core/scripts/audit-user-to-agent.py::_assess_user_leg`.
Why one verdict became two, and why the historical figures taken through the
old one cannot be read the way they are written.

## Why the split exists

`_assess_user_leg` returned `verdict: "undeclared"` from TWO branches:

- no `user_leg_scope` at all — the leg was never written down
- a scope declared but outside `VALID_USER_LEG_SCOPES` — written down, in words
  the grants table cannot key to

Those are different defects with **opposite remedies**. The first wants a
backfill. The second is already backfilled and wants vocabulary reconciliation.
The summary rendered both as *"cannot be re-derived until backfilled"*, so a
reader acting on it went and re-declared goals that were already declared.

guard-295 is the general form: a monitor's decision label is a CLAIM, and a
downstream consumer acts on it literally.

## The measurement that forced it (2026-09-07, g-001-595 fire 10, omni/cc-06)

Of 10 goals the audit reported as `undeclared`, **0** were genuinely undeclared
and **10** were declared non-canonically. The label was wrong for 100% of the
bucket it was applied to.

Re-measured across the whole drop lane with the split in place: **39 of 39 goals
carry a `user_leg_scope` — 100%, zero blank.** `undeclared` 0,
`scope-unrecognized` 10, `grants.unkeyed` 0.

## Why the historical figures are an upper bound, not a measurement

`.claude/rules/reclaim-routed-work.md` rule 4 quotes "20 of 28 `[agent,user]`
unscoped" (2026-07-29) and "8 of 36 — better, not fixed" (2026-08-19). Both were
taken through the conflated verdict, which counted declared-but-non-canonical
scopes as never-declared. So they bound the backfill gap from above; they do not
measure it. Do not quote either as evidence of a declaration gap without
re-measuring against the split.

## What the residual actually is

Vocabulary coverage, on both sides of the join. `VALID_USER_LEG_SCOPES` held 7
values (`architecture-decision`, `commit`, `credential-grant`, `data-provision`,
`deployment-approval`, `new-resource`, `push`). The 10 live scopes on the queue
— `permission-grant`, `standing-stand-down`, `iam-policy-write`,
`outreach-send-approval`, `third-party-console-login`, `pricing-decision`,
`submission-approval`, `strategic-direction`, `user-account-edit`,
`web-app-contracts-page-out-of-band-attestation` — matched **none** of them.

That is not agents failing to declare. It is a vocabulary that does not span the
legs this domain produces. guard-886 names why it persists: a field whose only
validator runs in a downstream audit keeps collecting unusable values, because
the writer never learns at write time.

The mirror finding on the other table is `grants no goal can key to` — a grant
row whose scope head avoids the vocabulary carries real permission the audit can
never apply. Both are rule 4 (declare invalidations in machine-findable terms)
pointed at the two tables that must converge, and the convergence is now a
vocabulary problem on both sides rather than a declaration one.

## The regression the split can cause, and its guard

Post-split, `undeclared` is the genuinely-blank population and is routinely **0**.
A consumer that branches only on `undeclared` therefore prints nothing and the
finding vanishes — strictly worse than the wrong label it replaced. The Phase
0.5b.14 body branches on BOTH counts for this reason; keep it that way.

Suppression is safe by construction and should stay so: `_ACTIONABLE_VERDICTS`
is an ALLOW-list (`grant-covered`, `grant-qualified`), so a new verdict defaults
to never-suppressed. A deny-list would have swallowed `scope-unrecognized`
silently the moment it was added — guard-1802, a predicate narrower than the
population it must cover. `test_scope_unrecognized_is_never_suppressed_by_a_prior_refusal`
pins that shape.

## Cross-references

- `guard-295` — a gate/monitor decision label is a claim a consumer acts on literally
- `guard-886` — a field validated only in a downstream audit keeps collecting unusable values
- `guard-1802` — a reclaim predicate narrower than the gate that creates its population
- `.claude/rules/reclaim-routed-work.md` rule 4 — declare invalidations in machine-findable terms
- `core/scripts/gates/user_leg_scope.py` — `VALID_USER_LEG_SCOPES` + its SYNC OBLIGATION
- `core/scripts/tests/test_audit_user_to_agent_drop_lane.py` — the two pinning tests
