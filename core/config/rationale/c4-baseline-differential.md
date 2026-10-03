# C4 baseline differential: why it exists and what was measured

Normative text: `core/config/conventions/pull-promotion.md`, "C4 baseline
differential". This file keeps the reasoning that would bloat it.

## The failure it answers

A downstream production deployment did not run `--adopt`, because strict C4 needs
`VERDICT: CLEAN` and that deployment's last full run, taken BEFORE any adoption, had
144 failing tests across about 40 files. Five files carried 65 of the 144 (26, 13,
10, 8, 8), which the deployment described as assuming a multi-agent roster on a
one-agent deployment. It named two fixes and said it would adopt the first tag
carrying either: gate C4 on NEW reds against a same-box pre-adopt baseline, or mark
the roster-shaped tests `fleet_layout`.

## Why the baseline and not the marker

The marker route needs the failing TEST names, and `pytest.ini` records why a fleet
box cannot supply them: the set "is only observable from a `run-full-suite.sh
--triage` run ON a single-agent deployment", and marking whole files from a box where
their tests pass would delete real coverage. How far a fleet box gets anyway was
measured. The five files named above hold 123 tests. A `git archive` of HEAD was run
twice with provider credentials stripped and the same external world and meta paths:
once with the agent roster HEAD tracks (five agent dirs carrying a `self.md`), once
reduced to a single agent dir. The roster-shaped copy passed 123 of 123; the
single-agent copy failed 1 of 123
(`test_duplicate_from_another_agent_is_refused_and_recorded_then_override_sends`).
The deployment sees 65 reds in those same files, so reducing the roster reproduces 1
of the 65. What else the deployment's one-agent state changes is not known (inferred,
not verified: the world state those tests read), and the marked set cannot be derived
from here. A baseline differential is blind to the cause, which is the point: it
handles every deployment-specific red, present or future, with no per-test curation.

## Three traps the design avoids, each pinned by a test

1. **The captured stdout carries no node ids.** On a GENUINE verdict the runner prints
   failing FILES with counts, not test node ids. A parser over that log reads an empty
   set, and a design that treats "empty" as "nothing new" would pass everything.
   Node ids live in `<out-dir>/chunk-NN.log`, so those are what is read, and an empty
   set beside a GENUINE verdict is a refusal, not a pass.
2. **The runner's own `failing_tests()` drops the class and `[param]`.** A differential
   on names would let a new failing parameter of an already-red test hide behind it.
   The parser keeps the full id.
3. **Stale chunk logs.** The runner rotates the previous run's logs into `prev/` before
   starting and the reader's glob is not recursive, so the adopt-commit run's ids
   cannot leak into the baseline's set. A recursive glob is one of the mutants the
   tests kill.

## Why opt-in

Strict C4 is a fail-closed gate and its `suite_is_green` carries the rule "missing
evidence never turns a red run green". Loosening it by default would change the
verification every downstream adopter gets. The flag makes the weaker mode an explicit
operator decision, and the adoption records which mode it ran in.

## Cost

Zero when strict is green. When strict is red and the flag is set, one more full
suite run, on the pre-adopt commit: the adopt-commit run's cost again.

## Not covered

A test that fails on both runs for a NEW reason is invisible to a node-id comparison
(same id, different cause). The report carries the counts so a reader sees how many
reds were inherited.
