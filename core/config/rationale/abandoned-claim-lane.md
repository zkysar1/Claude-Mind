# Rationale — the abandoned-claim lane (precheck 0.5b.23, g-306-445)

## The class

A goal can sit `status=in-progress`, `claimed_by=<agent>`, while NO live Body
holds it. Measured 2026-09-04: one critical-path goal sat that way from 16:26 to
20:17. On release it scored 21.45 — rank 1 of 1871, next candidate 14.93 — and
was claimed within 2 minutes. Two dependent goals waited behind it the whole
time, one of them for 7.3 days.

## Why the three existing claim tools cannot see it

Each name suggests coverage its predicate does not have (guard-6002; tree node
`enumerator-all-clear-boundary`):

| tool | predicate | verdict on an abandoned claim |
|---|---|---|
| `aspirations-clear-stale-claims.sh` | status is TERMINAL (claim residue self-heal) | reports 0 — an in-progress goal is not terminal |
| `claim-liveness-check.sh` | "is this claim still MINE" (guard-1151 supersession) | `LIVE` — `verdict()` returns LIVE on `status==in-progress and claimed_by==agent` and never reads `in_flight_bodies` |
| `claim-integrity-check.sh` | reconcile damage / partial field survival | clean — every field is intact |

None is wrong. The population falls between them, which is why this is a
separate lane rather than a widened predicate on any of the three.

## Why it is a separate lane from stranded-claim-sweep

`stranded-claim-sweep` is OWNING-AGENT-ONLY by design (it compares each claim's
`claimed_by_sid` against this process's `MIND_SID`). This class is fleet-wide:
the holder is usually another agent, so the owning-agent sweep cannot reach it.

## Both in-flight shapes are required

`in_flight` is REDUCER-owned — `team-state-in-flight.sh` stamps it only when the
box's running-session-id equals `MIND_SID`, and SKIPs for every other Body,
writing `in_flight_bodies.<sid>` instead. Reading only `in_flight` would report
every worker-Body-held goal as abandoned — the exact inversion the lane exists to
prevent (g-306-276; same defect g-306-160 repaired in
`goal-pickup-coordination-check.py`).

## Keep-safe direction (guard-4000)

Report is the default. A release fires only when ALL FOUR hold:

1. no in-flight row of either shape names the goal,
2. `claimed_at` is older than `threshold_minutes` (default 180, matching
   `DEFAULT_REAP_STALE_MINUTES`),
3. the goal is non-terminal, and
4. the team-state read was AUTHORITATIVE.

Condition 4 is load-bearing, not ceremony. The local tree is a read-through
cache under own-cloud (guard-980); a mirror read that came back thin shows zero
in-flight rows, which makes every claim in the fleet look abandoned. So an
unauthoritative read reports but releases nothing. This is also why the `.sh`
wrapper is required: only `_paths.sh` resolves `WORLD_PATH` from the per-agent
`local-paths.conf`, and the wrapper is the only thing that passes
`--authoritative` (g-115-6188 / guard-3864).

Condition 2 bounds a specific race: a Body between its claim write and its first
`in_flight` row write holds a REAL claim with no row yet. Measure that window
before lowering the threshold.

Release uses `--reason-kind progress` so notes survive — the motivating record
carried 219,496 chars of prior work.

## The apply path was dead on arrival, and the tests could not see it

Detection worked from the first live run; **remediation had never once fired.**
Three defects, all in the wrapper, all invisible to a unit-tested pure predicate:

1. `--reason-kind progress` was passed with **no `--reason`**.
   `aspirations-release.sh` refuses that shape (exit 1, *"the token types the
   reason; it does not replace it"*), so every release attempt failed — and
   `>/dev/null 2>&1` swallowed the message that says so.
2. The apply loop was gated on `$APPLY -eq 1 && $JSON -eq 0`, making
   `--apply --json` — the natural machine-readable form — a **silent no-op whose
   stdout was byte-identical to a dry run**.
3. `RELEASABLE_IDS` was emitted only in text mode, so even with (2) lifted the
   loop found no ids and reported *"nothing met all four keep-safe conditions"*
   over a report reading `releasable_count: 1`. The report and the action
   disagreed, and nothing said so. One `_emit_releasable` now serves both modes.

`test_abandoned_claim.py` had four passing tests throughout. They covered
`find_abandoned` — pure, dependency-free, correct — and nothing covered the
**production arg shape** of the script that acts on its output. That is
`guard-920` exactly, moved from tests to a wrapper. Checks 5-7 pin the shape:
`--reason` must accompany `--reason-kind`, apply must not be gated on output
mode, and the marker must appear in both.

Found and fixed 2026-09-05 (echo, cc-03) on the lane's second live run, against
a real finding: `g-326-280` (HIGH) had sat `in-progress` under bravo's claim for
**63.9 h** with no in-flight row, while bravo was alive and in flight on a
different goal — the "alive agent holding an abandoned claim" case above, seen a
second time. `g-326-256` (HIGH) was deferred behind it on
`blocked_on_dependency`. Released through the fixed canonical path; **57,963
chars of notes survived** (outcome_note 7,216 + progress_note 50,747, both
byte-unchanged), which is the first end-to-end confirmation that
`--reason-kind progress` does what this section claims.

## Holder liveness and claim abandonment are independent

The lane's first live run found a claim 62h old with no row whose holder was
ALIVE (last_active 4m, own writer — not the guard-3604 cross-agent-clear false
positive). An alive agent can hold an abandoned claim: it moved on and left the
claim behind. So the predicate must NOT gate on holder liveness, or it would
miss exactly this case. Liveness is still worth reading before RELEASING — it is
input to the disposition, never to the detection.

## Trace

g-306-445; guard-6002, rb-10174; g-306-438 (sibling: vanished-goal phantom
rows); g-306-191 / g-306-412 (the reaper this complements); board
`msg-20260904-202041-alpha-5486`, `msg-20260905-000140-echo-5607`.
Implementation: `core/scripts/_abandoned_claim.py` (pure predicate),
`abandoned-claim-check.py` (CLI), `abandoned-claim-check.sh` (wrapper);
tests `core/scripts/tests/test_abandoned_claim.py`.
