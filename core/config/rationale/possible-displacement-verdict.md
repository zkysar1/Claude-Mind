# Rationale: The `possible-displacement` Verdict Is Hedged On Purpose

Referenced from `.claude/skills/aspirations/SKILL.md` Phase -0.5c.1
(Stranded-Claim Sweep). Explains why `stranded-claim-sweep.py` emits an
ambiguous advisory rather than a `displaced` conclusion, and why the
orchestrator's documented response is a board read rather than an abort.

## Why the verdict exists at all

`coordination_merge.py` resolves a same-agent claim conflict — two live
instances of one agent claiming one goal — in favour of the older
`claimed_at`, and rewrites `claimed_by_sid` to the winner's. Nothing tells
the loser. The loser is mid-execution, so its own execution-diary entries
satisfy the sweep's `has_recent_diary` test and it early-returns "work is
happening" every iteration, forever. Before g-306-132-c that case was
completely invisible: no verdict, no counter, no line of output. An honestly
hedged advisory beats silence; a falsely confident one does not.

## Why it is `possible-displacement` and not `displaced`

The first version of the branch read "foreign sid AND local diary activity
⇒ I am the displaced holder." That inference rests on the premise that the
execution diary is box-local, and **the premise is false.**

Measured 2026-08-03: `session-manifest.yaml` gives `execution-diary.jsonl`
`sync_tier: continuity`, and `OwnCloudBackend._machine_local()` returns
False for it — the remote store is authoritative and the local tree is a
read-through cache. The diary is keyed per-AGENT, not per-session, and its
entries carry no session id at all (only `content` / `entry_type` /
`goal_id` / `phase` / `timestamp`). A peer instance's entries therefore
appear on this box as soon as it pulls. Contrast `execute-in-flight`, which
IS `sync_tier: machine_local`, precisely because a goal mid-execution on one
machine is not in flight on another.

So two readings fit the same evidence, and the branch fires on both:

1. You claimed the goal, a peer instance won on older `claimed_at`, and you
   were displaced.
2. A peer instance legitimately holds it and is working it; you never did.

Reading (2) is the **ordinary** peer-claim case. That asymmetry is the whole
argument for the name and for `ambiguous: true`: the common firing is the
benign one.

## Why the orchestrator checks the board instead of aborting

An automatic abort wired to this signal would abort a peer's legitimate work
every time reading (2) holds — which is most of the time. The signal is not
strong enough to carry a mutation.

The coordination board is the discriminator the claim record cannot be. Claim
posts are **append-only**, so both claims survive the merge overwrite that
destroyed the evidence in `aspirations.jsonl`. A claim post on the same
`goal_id` from a different, live `session_id` is the confirmation; its absence
is the ordinary case. This is the same board read `guard-1460` already
requires *before* starting work on a claimed goal — the verdict simply gives
the loser a reason to run it *again*, after the fact.

When displacement IS confirmed, the action is abort-without-release. Releasing
would be worse than doing nothing: post-merge the claim legitimately belongs
to the winner, so a release clears a live holder's claim and re-opens the goal
to a third instance — converting a detected conflict into a wider one.

## Why the sweep stays report-only

The consumer is the orchestrator, not the script. Aborting is a decision made
by an agent that has read the board; it is not a mutation this sweep may make
from the losing side. The branch therefore never writes, and a false positive
costs exactly one advisory line.

## What would change this

`g-306-143` owns finding a sound per-session discriminator. No box-local store
currently provides per-session attribution; the coordination board is the
likeliest source. If that lands, the Phase -0.5c.1 branch can be strengthened
from "prompt to check" toward a stronger consumer — but not before.

## Cross-references

- `guard-1460` — a claim is not proof no one else is working the goal when the
  other worker is another session of the same agent; carries the interim board
  -read protocol this branch reuses
- `rb-6503` — the producer-with-no-consumer / unreachable-guard shape
- `core/scripts/stranded-claim-sweep.py` — the emitter (verdict string,
  `ambiguous` flag, `summary["possible_displacement"]` counter)
- `.claude/skills/aspirations/SKILL.md` Phase -0.5c.1 — the consumer
- `g-306-132-c` (hedge + merge fix), `g-306-142` (this consumer),
  `g-306-143` (sound discriminator)
