# `times_active` has TWO independent writers on TWO surfaces, and neither surface is the live count

Decision record for g-115-10078. Measured 2026-09-18 (alpha worker Body, cc-07,
Linux 6.8.0-139-generic, own-cloud), replicating and then correcting the
2026-09-16 reading the goal was filed on.

Read this before "fixing" `_utilization_store.utilization_of` to prefer the
embedded block for this counter. That fix is wrong, and so is `max()`.

## THE WRITER, identified by direct observation (not inferred)

`core/scripts/guardrail-check.py::_check_store` — the line is

    util["times_active"] = util.get("times_active", 0) + 1

inside `locked_modify_jsonl` on the CONTENT store. It never touches the spool,
so the sidecar never sees it. `guardrail-check.sh` `exec`s the .py directly with
no daemon in the path, so this IS the live writer.

Observed, one call, bracketed:

| moment | guardrails.jsonl md5 | guardrails-utilization.jsonl md5 |
|---|---|---|
| after `--dry-run` | `c9f1ea34…` (unchanged) | unchanged |
| before the real call (T0) | `c9f1ea34…` | `ed5e97cd…` |
| after the real call (T1) | `eda68c91…` **changed** | `ed5e97cd…` **unchanged** |

Attribution is exact, not circumstantial. That write left its own copy-on-write
record in the store's history dir, `2026-09-18T01-33-22.893399_alpha.yaml` — a
195-byte METADATA record, not a copy of anything: `encoding: full`,
`size_bytes: 19120482`, `hash: 4debb797ad79…f48cae`. The content behind it was
reconstructed WITHOUT touching the live file, through the framework's own
non-destructive reader — `core/scripts/history.py:141 _read_snapshot_text`, the
call `cmd_diff` uses. (Never `history.py restore`: that is not a read at all,
it OVERWRITES the live store, guard-4165.) The reconstruction is 19,120,482 B,
matching the record's own `size_bytes` exactly, and its md5 is
`c9f1ea345388e8c3dd3eef193acd7fc5` — byte-identical to the T0 store in the table
above, so it is the provable pre-write state.

The .yaml record's OWN md5 is `4cb9e5e41126edb0b1c15c76566afb9b`, and this
paragraph quoted the STORE's md5 against the RECORD's filename until the Q4
provenance sample caught it. Worth keeping as a correction rather than a silent
fix: it reads as though the 195-byte pointer were the 19 MB snapshot, so the
next reader md5sums the .yaml, gets a number that can never match, and concludes
the attribution below is fabricated. Diffing the reconstruction against the live
store through the framework reader:

* `times_active` changed on **414** records, delta histogram **{+1: 414}**
* the changed set **equals** the dry-run's matched set exactly (414 == 414,
  empty in both directions) — a concurrent writer would appear in that
  difference and none did
* **0** records changed in any other way; 0 added, 0 removed
* the other 6,106 active guardrails did not move (the negative control)

## THE MECHANISM: seed once, then two accumulators that never meet

`utilization-flush.py` states its own contract: a first-touch sidecar entry is
**seeded from the record's embedded counters** and the spooled delta is applied
on top, and "after the first touch the sidecar is authoritative for that id and
this path is never taken for it again."

So for one record, with `seed` = the embedded value at its first spool touch:

    embedded_now = seed + A        A = guardrail-check bumps since first touch
    sidecar_now  = seed + B        B = spooled increments since first touch
    true         = seed + A + B  =  embedded_now + sidecar_now - seed

`seed` is not recorded anywhere. Therefore:

* **both surfaces understate the truth**, and `true >= max(embedded, sidecar)`;
* **the true count is not recoverable from the two surfaces** — no read-side
  choice, including `max()`, computes it;
* the disagreement is expected in **both** directions, which is why no
  direction-based heuristic survives (guard-4631 recorded the same, from the
  other side).

## THE POPULATION, 2026-09-18 (and the two-day trend)

Full corpus, both stores, embedded vs sidecar `times_active`:

| store | embedded > sidecar | equal | sidecar > embedded | no sidecar row | total gap | max gap |
|---|---|---|---|---|---|---|
| guardrails | 5758 (89.3%) | 473 | **215** | 135 | 191,754 | guard-1720 (248) |
| reasoning-bank | 1534 (22.2%) | 5357 | **4** | 3755 | 75,018 | rb-7331 (171) |

The 215 + 4 rows where the SIDECAR is higher are the half a one-way fix would
destroy: those are the agent-attested increments that went through the spool.
(93.0% of them carry `times_helpful|times_cited|times_inferred_helpful > 0`
against 85.4% of the embedded-high rows — suggestive of an agent-increment
origin, NOT a clean discriminator, and not asserted as one.)

Growing, measured against the goal's own 2026-09-16 reading two days earlier:
guardrails understatement 161,347 -> 191,754; max gap 210 -> 248; and on the
goal's exemplar **guard-2115 the sidecar stood still at 11 for 14 days across
two independent measurements while the embedded went 15 -> 22 -> 24.**
`guard-001` is the same shape and is load-bearing elsewhere: sidecar frozen at
**23** since at least 2026-08-22, embedded now **40**.

## THE DECISION

1. **Do NOT flip `utilization_of` to prefer the embedded block** — for this
   counter or wholesale. Wholesale would break `retrieval_count` /
   `times_helpful`, where the sidecar genuinely IS live; per-key would silently
   discard the 219 rows whose spooled increments exceed the embedded count.
   The wholesale-replace defence in that function's docstring stands unchanged.
2. **Do NOT `max()` them, and do NOT add `times_active` to the reconcile's
   `INT_FIELDS`.** g-358-24 pinned it OUT with a test because "a max() here
   would write a stale value over a live one in one direction or the other and
   the direction was unsettled." The direction is now settled and it is BOTH:
   a max still loses the smaller side's independent contribution.
3. **The repair is write-side**: make `guardrail-check.py` spool its increment
   like every other counter, then re-seed once. Until that lands, every reading
   of `times_active` from either surface is a FLOOR and must be cited as one.
4. **Nothing that GATES should read `times_active` at all** — and today nothing
   does (next section). Treat it as scan telemetry, per guard-3995.

## WHAT THIS FALSIFIES — the retirement-slate consequence, consumer by consumer

g-115-10078 was filed on the premise that a systematically-low `times_active`
"biases that slate toward proposing live guardrails for retirement." Checked
against the three consumers it names, that does not reproduce:

* `mind_api/src/endpoints/utilization.py::_is_candidate` keeps a record on
  **`_attested_evidence(util) > 0`** (line 209, with the comment "ATTESTED, not
  the composite"). `ATTESTED_WEIGHTS` is `times_helpful` / `times_inferred_helpful`
  / `times_cited` — **`times_active` is deliberately excluded**, because it
  "is incremented on a bulk text match with no agent decision in the path".
  `_candidate_sort_key` uses the attested form too. The goal's claim that it
  gates on `_evidence` is wrong — and so is the same claim in
  `_utilization_store.utilization_of`'s docstring, which is where it was
  inherited from (corrected in the same change as this file).
* `core/scripts/guardrail_retire.py` REPORTS `times_active` on a candidate row
  but gates on `staleness_days`, derived from
  `effective_relevance = max(last_retrieved, last_active_at, last_relevant_at-or-created)`.
* `core/scripts/scar-tissue-check.py` uses `times_active` as a displayed
  discriminator on subset/superset pairs, not as a gating predicate. Its
  never-marked-helpful population is keyed on the attested counters (guard-849).
* `core/scripts/weakness-signals.py` computes `guardrail_times_active` for the
  baseline and its own docstring records that lane as RETIRED and unconsumed,
  for exactly this reason: "times_active increments on keyword scans, so even a
  windowed delta carries no genuine-fire information."

The blindness is real; the harm the goal predicted is not, because four
independent consumers had already ruled `times_active` out as evidence.

## THE REAL DEFECT NEXT DOOR: `last_active_at` has no writer at all

`guardrail_retire.effective_relevance` reads `last_active_at`, whose docstring
says it is "stamped when times_active increments". **It is not stamped by
anything.** Measured with positive controls on the same files:

    last_active_at in guardrails.jsonl              0
    last_active_at in guardrails-utilization.jsonl  0
    times_active   in guardrails.jsonl           6581   (positive control)
    last_retrieved in guardrails.jsonl           6581   (positive control)

and in PRODUCTION code `last_active_at` appears in exactly one file —
`guardrail_retire.py` — where every occurrence is a READ. (Corrected during Q2:
a *tree-wide* grep also hits `core/scripts/tests/test_guardrail_retire.py`,
which constructs the field as a FIXTURE — that pins the reader and says nothing
about a writer — plus design notes in agent experience files. Confined to
`mind_api/src/`, `last_active_at` has 0 hits against a same-directory,
same-tool positive control of 8 for `last_retrieved`.)

THE WRITER WAS DESIGNED AND NEVER BUILT, which is sharper than "no writer".
The g-303-31 / s93 design pass specifies it verbatim: "Daemon `last_active_at`
stamp — in mind_api `/v1/store/increment`, when … `utilization.last_active_at
= now`. Engine reads it via `_last_active()`". That endpoint exists and does
NOT stamp it — read end to end here rather than grepped:
`mind_api/src/endpoints/store.py`, `def increment(ctx)` at L689 through its
`register()` binding at L802-807, BOTH write paths included — the L753
`utilization` spool branch (returns `{"ok", "spooled", "record"}`) and the L778
`_cycle` legacy RMW, whose only mutation is `rec[parent_key][counter] += 1` at
L786 followed by `spec.recompute(rec)`. Neither path writes any date field, and
the `except Exception: pass` at L769 means the spool branch can only ever fall
BACK to that same RMW. So the READER
shipped and the WRITER did not — and `_last_active`'s own docstring says so
("Absent today; read gracefully so the engine works pre-stamp"), which is why
nothing ever failed loudly.

So one of the three terms of the staleness clock is permanently `None`, and the
writer that would stamp it is the very one identified above: it bumps the
counter and never stamps the date. A guardrail that matched 7,463 times ages
toward retirement as though it had never fired. **That** is the
retirement-slate bias the goal was reaching for, it is a gating predicate
rather than a reported one, and it is one line away from the fix in item 3.

## WHAT IS NOT MEASURED, and must not be asserted from here

* WHY reasoning-bank sits at 22.2% against guardrails' 89.3%. The call shapes
  differ, and each shape is quoted from its literal call site:
  `.claude/skills/aspirations-precheck/SKILL.md:1119` runs `guardrail-check.sh
  --context any --phase pre-selection --type both` every iteration, while
  `core/config/execute-protocol-digest.md:315` (Phase 4.1) runs
  `guardrail-check.sh --context infrastructure --outcome <succeeded|failed|any> --phase
  post-execution` (succeeded = the goal's primary action worked; failed = it did
  not; any = match both) — passing NO `--type`, so taking the parser default
  `guardrail` (`core/scripts/guardrail-check.py`, `--type
  {guardrail,reasoning-bank,both}`) — and only when the goal resolves as
  infrastructure or testing (`.claude/skills/aspirations-execute/SKILL.md`
  L973-980). The call shapes are measured; the LINK from them to the
  22.2%/89.3% split is a PLAUSIBLE MECHANISM,
  INFERRED. The sidecar-write volume per store was not measured.
* Whether the 219 sidecar-high rows are agent increments. Correlation only.
* Any claim about boxes other than cc-07.

## PROVENANCE OF THE CITATIONS ABOVE

Run through `q4-provenance-sample.sh` on 2026-09-18. Two corrections it forced
are already folded in above — the snapshot-md5 mis-attribution in the first
section, and the over-broad `last_active_at` grep scope in the fifth (that one
caught at Q2). Two citations remain that this session's provenance manifest
CANNOT attest either way, and recording which is the point: a checker must not
report what it declined to look at as a pass, and neither must its subject
(guard-1760).

* `mind_api/src/endpoints/store.py` — READ end to end here, and the sampler
  still reports it `decorative-citation` (a BLOCKING kind). TWO facts compose,
  and the first is the proximate one; an earlier draft of this bullet named only
  the second, which is the causal-attribution error this file already commits
  once above.
    1. The token extracted is `src/endpoints/store`, NOT the path as written.
       `ground_truth_citation._NODE_KEY`'s segment class is
       `[a-z0-9]+(?:-[a-z0-9]+)*` — no underscore — so the match begins AFTER the
       `_` in `mind_api` and stops before `.py`. That truncated token names no
       file on disk.
    2. `q4_provenance_sample.expressible_predicate` demotes a citation to the
       NON-blocking `unadjudicable-citation` only where the token "positively
       resolves to a real file that the recorder's own scope predicate excludes".
       `mind_api/src/endpoints/store.py` IS exactly such a file — `mind_api/**`
       is outside every prefix in `core/scripts/context-reads.py`
       (`TRACKED_PREFIXES` = `core/config`, `.claude/skills`,
       `world/knowledge/tree`, `world/conventions`; `ADVISORY_EXTRA_PREFIXES`
       adds `core/scripts` for the recorder only), so no read of it can ever be
       recorded and the demotion is the correct verdict. The truncated token
       never reaches that branch and falls through to the default-True fail-safe.
  So the blocking finding is produced by an underscore, and the fail-safe that
  carries it there is correctly biased toward keeping the check ON. Relayed as an
  observation, not fixed from here: tightening an extractor weakens a negative
  assertion, and this module's own comments record a measured instance of that
  trade being got wrong.
* `.claude/skills/aspirations-precheck/SKILL.md:1119` — 133,389 B / 2,067 lines,
  past the Read-tool cap, and `read_tracker()` counts FULL reads only by
  contract ("peeking at one region of a file is not evidence you read the
  claim's source"). So that line is line-anchored GREP provenance, quoted
  verbatim, and cannot be upgraded without splitting the file.

Everything else cited above was read whole in the same session:
`core/scripts/guardrail-check.py` — both the writer at its `_check_store` and
the `--type` default in its argparse — `core/config/execute-protocol-digest.md`,
and this file. One precision that whole read adds: the digest's Phase 4.1 gates
on `involved_infrastructure` ALONE (L314-315), which is NARROWER than the
SKILL.md's "each applicable context (infrastructure and/or testing)". The digest
is the documented hot path after a compaction, so the narrower shape is the one
that usually runs.
