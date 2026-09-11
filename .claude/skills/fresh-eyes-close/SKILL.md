---
name: fresh-eyes-close
description: "Use when a tier-2 goal needs its blocking close review — the INDEPENDENT adversarial review that produces the verdict artifact core/scripts/close-review-gate.py reads before allowing a close. Fires when close-review-gate.py REFUSES a close for want of an APPROVE verdict, when a peer sends a review request for a tier-2 goal, or when the user invokes /fresh-eyes-close directly. Runs four mandatory checks — requirements traceability, source fidelity (mechanised: every enumerated entity diffed verbatim), criteria adequacy, and an adversarial wrong-artifact probe — and writes APPROVE, APPROVE_WITH_NOTES or REJECT to world/audit-reports/close-reviews/<goal-id>.json. The reviewer MUST NOT be the closing agent. Distinct from /fresh-eyes-code (bug hunt on a named artifact set) and /fresh-eyes-review (portfolio + self meta-review): this one is close-time, goal-scoped, and BLOCKING."
user-invocable: true
triggers:
  - "/fresh-eyes-close"
  - "fresh eyes close"
  - "close review"
  - "close-review verdict"
  - "independent close review"
tools_used: [Bash, Read, Grep]
companion_scripts:
  - core/scripts/close-review-verdict.py
  - core/scripts/close-review-gate.py
  - core/scripts/q4-provenance-sample.sh
  - core/scripts/provenance-check.sh
  - core/scripts/liveness-check.sh
conventions: [coordination, board, reasoning-guardrails, goal-schemas, aspirations]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking: []
---

# /fresh-eyes-close — Independent Blocking Close Review

Produces the verdict artifact the close-review gate reads. The gate
(`close-review-gate.py`, g-357-40) refuses a tier-2 close without an APPROVE
verdict; this skill is what writes one — or, more often and more usefully,
what writes the REJECT that sends the work back.

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## The one rule that makes this skill worth having

**The reviewer MUST NOT be the closing agent.** The gate enforces it
(`independence_defect` refuses an APPROVING verdict whose `reviewer` is the
closer, and refuses one that names no reviewer at all; a REJECT on your own
close is allowed — finding fault in your own work needs no independence, and
refusing it would suppress the record rather than the conflict), and the verdict path is
goal-keyed and world-scoped precisely so an independent party can write it.

Independence is the GATE's definition, not this skill's: the producer IMPORTS
`independence_defect` rather than reimplementing it, so a self-review is refused
at production time (`test_self_review_is_REFUSED_at_production_time`). Choose the
reviewer before assembling anything.

Independence order, per `coordination.md` Review Gate:

1. **A live peer agent** via the review-request lane — check with
   `bash core/scripts/liveness-check.sh --agent <peer> --json` and conclude a
   peer is unavailable only on `dormant` (never on `unknown`).
2. **A fresh-context subagent** when no peer is live. Always available, and the
   fallback solo deployments depend on.

A fresh-context reviewer is not a weaker substitute for the whole defect class
this gate exists for: the founding incident (coach g-012-02) was refutable from
the goal description alone. A peer additionally catches shared-prior blind
spots, which is why it is preferred where one exists.

## What the reviewer receives — and what it must NOT

Give the reviewer ONLY:

- the goal record: title, description, verification criteria, and every
  referenced source id **FETCHED TO ITS TEXT**
- the artifact paths
- the adversarial mandate below

FETCHED, not merely resolved (g-357-44). Naming a source is not reading one: a
citation this session never opened is DECORATIVE, and it is the shape that makes
a fabricated claim look sourced. An unresolved id is a hole in check 1 — resolve
it or record it as unreviewable. Burn the tokens; the user directive of
2026-08-31 asks for exactly that trade. Confirm each cited source was actually
retrieved rather than recalled:

```bash
bash core/scripts/provenance-check.sh --session-id "$MIND_SID" <url-or-node-key>
```

Exit 1 means no tool-fetch record this session — go fetch it, do not reason from
the citation. Keep `--session-id`: the manifest is per-Body, so omitting it reads
an agent-wide file a worker Body may not even have.

Never hand over the executing session's context, reasoning, or narrative. The
review is worth exactly what its independence is worth, and a reviewer primed
with the author's account of why the work is correct is no longer independent. If
you cannot run this review without the executing context in view, escalate to a
fresh-context reviewer per the independence order above rather than proceeding.

## The four mandatory checks

### 1. Requirements traceability — judgment, plus a MECHANISED citations-MATCH probe
Every entry in `verification.outcomes` maps to concrete produced evidence.
**Quote the evidence.** An outcome you cannot quote evidence for is unmet, not
"probably fine", and a partially-met bar may not be narrated away (`guard-2541`).

Quoting a citation is citations-EXIST. This check is also citations-MATCH: for
each sampled claim, diff what the claim asserts against what the FETCHED source
actually says, and REJECT on contradiction. The sample is not yours to choose —
run it:

```bash
bash core/scripts/q4-provenance-sample.sh --goal <goal-id> \
  --artifact <artifact path> --source-file <fetched source> --json
```

It reports `missing-citation` (no source token), `decorative-citation` (cited but
never fetched this session) and `direction-contradiction` (the artifact asserts
A -> B where the source asserts B -> A). The last is the one a citations-exist
check cannot see, and the producer vetoes an APPROVE on it mechanically. Read
`clusters_total` beside `sampled_count`: a clean verdict over 2 of 40 clusters is
a thin one, and the reviewer says so.

### 2. Source fidelity — MECHANISED, and the one that catches the founding class
Diff **every** enumerated entity in the description/source against the artifact,
verbatim. Do not eyeball this and do not count. Run:

```bash
py -3 core/scripts/close-review-verdict.py \
  --goal <goal-id> --reviewer <your-name> --closer <closing-agent> \
  --source-file <path to the goal description/source text> \
  --artifact-file <path to the produced artifact>
```

With no `--approve` or `--reject` it reports the diff and writes nothing
(exit 2) — that is the read-only probe. It reports `missing` (enumerated in the
source, absent from the artifact), `invented` (in the artifact, not in the
source), `counts_match`, and `substitution_signature`.

**`counts_match: true` beside a non-empty `missing` is the founding incident
exactly**: coach g-012-02 shipped 16 of 16 entities with 6 identities silently
substituted, and the count-based criterion went green. Read the diff, never the
count.

**There are TWO mechanical checks and they are complements** (g-357-44).
`source-fidelity` is the id-set difference — deliberately narrow, id-shaped
tokens only, because it shares its regex with the tier classifier and widening it
would drag ordinary prose into tier 2. `direction-fidelity` is the citations-MATCH
half from check 1. The trade-direction fixture shows why neither can cover the
other: for the claim "Miami sent the first-round pick to Denver" against a source
saying "Denver sent ... to Miami", `named_entities` returns the EMPTY SET for both
sides, so `source-fidelity` passes a claim that is exactly backwards — same entity
set, same citation, reversed relation. A record carrying only one of the two is
incomplete.

### 3. Criteria adequacy — judgment
Construct one plausible **WRONG** artifact that would still pass the goal's
stated criteria, then test the real artifact for exactly that flaw. If you
cannot construct one, say so explicitly — that is a finding about the criteria,
and a strong one.

### 4. Adversarial mandate, including side-effects and scope — judgment
Your job is to find the reason this close should not happen. "I could not find a
defect" is a legitimate and valuable outcome, but it must be **asserted after
probing**, never arrived at by default.

Probe scope explicitly, not only correctness: files touched outside the goal's
scope, conventions broken, dead code left behind.

## Writing the verdict

```bash
# REJECT — the normal outcome when any check fails
py -3 core/scripts/close-review-verdict.py \
  --goal <goal-id> --reviewer <your-name> --closer <closing-agent> \
  --source-file <source> --artifact-file <artifact> \
  --reject --finding "<what is wrong, specifically>" --write \
  --route-to-goal <world|agent>

# APPROVE — only after all four checks pass
py -3 core/scripts/close-review-verdict.py \
  --goal <goal-id> --reviewer <your-name> --closer <closing-agent> \
  --source-file <source> --artifact-file <artifact> \
  --approve --check "traceability: <what you verified>" \
  --check "criteria-adequacy: <the wrong artifact you constructed>" --write

# APPROVE_WITH_NOTES — the close is sound AND you have non-blocking observations
py -3 core/scripts/close-review-verdict.py \
  --goal <goal-id> --reviewer <your-name> --closer <closing-agent> \
  --source-file <source> --artifact-file <artifact> \
  --approve-with-notes --finding "<the non-blocking observation>" --write \
  --route-to-goal <world|agent>
```

Exit codes: `0` an APPROVAL written (`APPROVE` or `APPROVE_WITH_NOTES`), `3`
REJECT written, `1` refused (a failing fidelity diff under either approving
flag, self-review of an APPROVAL, or `--approve-with-notes` carrying no
`--finding`), `2` no verdict asserted.

Every written verdict carries `reviewed_at` (naive ISO-8601, UTC wall time).
Artifacts written before 2026-09-03 do not have it and were deliberately NOT
backfilled — inventing a timestamp on another reviewer's attestation is worse
than the gap — so treat it as optional when reading old rows.

**Pick APPROVE_WITH_NOTES over a REJECT when the defects do not block the
close, and over a plain APPROVE when you have any.** It releases the close
exactly like APPROVE (`close_review_gate.RELEASING_VERDICTS`) and, unlike
APPROVE, it ROUTES its findings to the goal record. Before it existed the
binary forced a reviewer with a minor observation either to block sound work or
to drop the observation, because a plain APPROVE routes nothing.

Three refusals are deliberate and must not be worked around:

- **`--approve` is REFUSED when the fidelity diff is non-empty.** The machine
  may VETO an approval on its own evidence and may never GRANT one, so the
  label never asserts more than the predicate supports (guard-2564).
- **Self-review is REFUSED** when `--closer` equals `--reviewer`. Producing the
  artifact anyway would only add a record the gate will reject.
- **No verdict is invented.** Without `--approve` or `--reject` nothing is
  written, so "the reviewer did not say" stays distinguishable from "the
  reviewer said no".

`--check` and `--finding` are recorded verbatim in the artifact, and the
mechanical fidelity evidence is stored alongside the conclusion so a later
reader can recompute the verdict from the record itself (guard-3743) — the record
carries the source set, the artifact set, both directions of the diff, AND the
`direction` block with every directed pair from each side.

**Record all four checks in `--check`, whether each passed or failed. A check
that was not run must not be reported as one that passed.** `--check` is
`action="append"` and the string is stored VERBATIM — the producer does not parse
it, so there is no required `name:pass|fail` grammar. Write whatever a later
reader needs, and say plainly which checks failed.

## After a REJECT

The REJECT blocks the close — that is `close-review-gate.py`'s job and it is
already pinned. Route the defects so the work resumes rather than stalling:

1. `--route-to-goal <world|agent>` appends the findings to the goal's
   `progress_note` — a scoped call to `goal-field-append.sh`, the framework's
   one goal-field append writer. It fires on any WRITTEN non-APPROVE verdict —
   REJECT and APPROVE_WITH_NOTES: a plain APPROVE has nothing to rework and a
   dry run leaves no trace. Without it the defects live only in the verdict
   artifact, which nothing reads at claim time. The appended header names the
   verdict, so an APPROVE_WITH_NOTES is never announced as a block.
2. Leave the goal claimable — release rather than closing it.
3. Re-review after rework: the same command, a fresh diff, a new verdict.

The append marker is keyed on a digest of the FINDINGS, not the goal, so a
repeat of the same review is idempotent while a re-review that finds
DIFFERENT defects appends a fresh note. A goal-keyed marker would swallow the
second review — the case this routing exists to serve.

If routing fails the verdict still stands (the artifact is already on disk) and
the failure is printed as `ROUTING FAILED ... NOT annotated`. Do not read past
that line: an unrouted REJECT is indistinguishable from a goal nobody found
defects in.

An APPROVE written for a superseded artifact is stale, not valid — re-run the
review after any rework rather than reusing the earlier verdict.

## Cost posture

Deliberate, per user directive: a tier-2 close spawns a full fresh-context
reviewer every time. **Tiering is the cost control** — tier 0 and tier 1 goals
skip this skill entirely and pay nothing.

## Verification

```bash
STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_close_review_verdict_producer.py -q
```

## Chaining

- **Called by**: the user (`/fresh-eyes-close`); a peer's review request for a
  tier-2 goal; a Body whose close was refused by `close-review-gate.py`
- **Calls**: `core/scripts/close-review-verdict.py` (the producer),
  `core/scripts/liveness-check.sh` (peer availability)
- **Consumed by**: `core/scripts/close-review-gate.py` at the `do_verify`
  chokepoint in `core/scripts/iteration-close.sh`

## Return Protocol

See `.claude/rules/return-protocol.md` — the last action of any turn MUST be a
tool call, not a text summary. The terminal action of this skill is the `Bash`
call that writes the verdict (`close-review-verdict.py ... --write`), or, when
the review ends in a REJECT that must be routed, the `Bash` call appending the
findings to the goal's `progress_note`. Never end on a prose summary of the
verdict.
