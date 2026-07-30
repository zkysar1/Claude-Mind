# Governed-Store Write Classes: Merge-Protected vs Fence-Only

Every governed store falls into exactly one of two write classes, and the class
decides what a writer MUST do. Nothing recorded this before g-115-3295, so the
two classes were treated identically and conclusions drawn about one silently
transferred to the other — which is how `meta_yaml` was left on the unsafe
pattern while two of its own siblings were fixed.

## The two classes

| | Class (a) MERGE-PROTECTED | Class (b) FENCE-ONLY |
|---|---|---|
| Definition | `coordination_merge.merge_handler_for(path)` returns a handler | it returns `None` |
| What happens on a conflict | the backend reconciles the two versions commutatively | the backend **freezes** the path — nothing reconciles below the write |
| Therefore the fence is | one defense among two | **the whole defense** |
| A stale fence is | recoverable — the merge lands the peer's write | a **permanent wedge**, no self-recovery |
| Writer requirement | fenced write is sufficient | **MUST** use `locked_rmw` + a `force_fresh` read, both inside one cycle |

The asymmetry is the entire point: for class (b) a stale If-Match token means
every PUT fences against an etag the remote no longer has, and the 412 repeats
forever against a remote that never changes (rb-2639 — a per-object, per-box
DEADLOCK, not transient contention). Class (a) degrades; class (b) wedges.

## How to classify a store (one lookup, no judgment)

`merge_handler_for` in `core/scripts/coordination_merge.py` dispatches on
**basename**, against the `_HANDLERS` dict (79 entries as of 2026-07-30), plus
one path-pattern branch for per-agent team-state shards
(`.../team-state/agents/<name>.yaml`, whose basenames are dynamic).

```bash
# authoritative, and cheaper than reasoning about it
grep -n '"<basename>": merge_' core/scripts/coordination_merge.py
```

A hit means class (a). No hit means class (b). Registration is by basename
only — a store's directory, purpose, and sibling files are all irrelevant.

## The prior question: does this store sync at all? (class (c), g-115-3992)

The lookup above answers "is there a handler." It does **not** answer "can this
path ever conflict," and for a machine-local store the answer is no — it is
never mirrored to S3, so no peer can diverge from it, so there is no 412, no
freeze, and no wedge. Such a store reads as class (b) by the lookup while
carrying **none of class (b)'s risk**, and a handler registered for it is dead
code that will never be reached.

Ask the prior question first. The authoritative predicate is
`owncloud_backend._machine_local`:

```python
# the _EXCLUDE_DIRS directory prune AND the basename policy — both legs
from owncloud_sync import _is_machine_local, _EXCLUDE_DIRS
any(seg in _EXCLUDE_DIRS for seg in rel.parts[:-1]) \
    or _is_machine_local(p.name, prefix, full_path=p, root_path=root)
```

**Calling `_is_machine_local` alone is the trap** — it deliberately does not
test `_EXCLUDE_DIRS` (that is the sync-walk's dirnames prune, not a per-file
rule; see the NOTE in its caller). Measured 2026-07-30: the one-leg form reports
`world/presence/<agent>.jsonl` as syncing when `presence` is an excluded
directory. One leg gives a confidently wrong answer, not an error.

Two further traps in the same family, both measured on the same pass:

- **`.gitattributes merge=ayoai-ledger` is not evidence a path is governed.**
  The whole `/.mind-data/` tree is gitignored, so git never merges any of it —
  yet `git check-attr merge` still reports `ayoai-ledger` for all 255 candidate
  files, because it matches path STRINGS regardless of tracking. A population
  derived from `check-attr` therefore looks authoritative and is inflated. The
  live consumer of the registry for these paths is
  `owncloud_backend._coordination_merge_handler` (the both-diverged 412
  reconcile), **not** git.
- **"Absent from `_HANDLERS`" is not "untriaged."** Several stores are
  deliberately unregistered with the reason recorded in a *comment block*
  adjacent to the dict — derived caches, basename collisions, writerless stubs,
  rewrite-path stores whose union would resurrect deletes (guard-1816). A
  predicate that reads only the dict re-opens settled questions and can lead a
  reader to "fix" a disqualification back into a regression. Read the comments,
  not just the keys.

Net effect when these were applied to one real 18-store population: 9 were
machine-local (out of scope entirely), 1 was already disqualified by a recorded
writer read, 1 could not be certified append-only, and 2 were genuinely
registerable — so the lookup-only predicate had overstated the work ~6x and
pointed at one change that would have been an active regression.

## The trap this convention exists to prevent

**Sibling files are not the same class.** Of the six strategy files written by
the same code paths (`strategy_apply.py`, `meta_yaml.set_field`):

| Store | Class |
|---|---|
| `goal-selection-strategy.yaml` | **(a) merge-protected** |
| `reflection-strategy.yaml` | (b) fence-only |
| `evolution-strategy.yaml` | (b) fence-only |
| `aspiration-generation-strategy.yaml` | (b) fence-only |
| `encoding-strategy.yaml` | (b) fence-only |
| `skill-quality-strategy.yaml` | (b) fence-only |

One of six. Same writers, same directory, same shape — different protection,
decided purely by which basename someone remembered to register. So "we checked
the strategy files and they're fine" is never a valid inference: check the one
you are writing.

The same trap in its original form: `g-115-1899-b` assessed the **aspirations**
writers, found the fenced write sufficient, and concluded "no handler-level
`locked_rmw` or idempotency shim warranted." That conclusion is *correct for its
scope* — `aspirations.jsonl` is registered, class (a). It does not extend to
`meta/`, and nothing recorded the distinction, so it was read as general.

## Census: every bare-lock writer under `mind_api/src`, classified

67 `with file_locks.locked(...)` call sites across 23 files (measured
2026-07-28, cc-05/Linux). **Enumerate by the LOCK CALL, not by a helper name** —
a first pass keyed on the per-module `_persist` helper found only the seven
`meta/` modules and missed `experience_write.py` and `wm_write.py` entirely,
because those take the lock inline.

**Read the per-row counts as LOCK SITES, not callers.** Every `meta/` module
holds its single bare lock inside one `_persist` helper, so its remediation cost
is ONE conversion however many callers that helper has; `experience_write.py` and
`wm_write.py` lock inline and pay per site. The first version of this table
reported 4-6 for three `meta/` modules by counting callers — the same
count-the-wrong-thing error the paragraph above warns about, made against this
table's own instruction. Corrected 2026-07-28 by the fresh-eyes pass on this file
(board `msg-20260728-185900-bravo-4796`).

| Module | Store(s) | Class |
|---|---|---|
| `changelog.py` | `changelog.jsonl` | (a) |
| `endpoints/aspirations_write.py` | `aspirations.jsonl`, `-archive.jsonl`, `-meta.json` | (a) |
| `endpoints/board_write.py` | the five board channels + `*-reads.jsonl` | (a) |
| `world/pipeline_write.py` | `pipeline.jsonl`, `-archive.jsonl`, `-meta.json` | (a) |
| `world/tree_write.py` | `_tree.yaml`, `tree-debt.jsonl` | (a) |
| `world/pattern_signatures_write.py` | `pattern-signatures.jsonl` | (a) |
| `world/skill_relations.py` | `skill-relations.yaml` | (a) |
| `meta/spark_questions_write.py` | `spark-questions.jsonl` | (a) |
| `endpoints/experience_write.py` | `experience.jsonl`, `experience-archive.jsonl` | **(b) — FIXED** (5 of 6 sites; the 6th writes `experience-meta.json`, unfenced — see below) |
| `endpoints/wm_write.py` | `working-memory.yaml` | (b) by basename, **NOT a hazard — unfenced write path** (see below) |
| `meta/meta_yaml.py` | generic dotpath writer + 2 side effects | **(b) — FIXED** |
| `meta/meta_backpressure.py` | `backpressure.yaml` (all 6 `_persist` callers) | **(b) — FIXED** (5 mutating handlers; `status`/`cooldown_check` are read-only) |
| `meta/meta_generations.py` | `strategy-generations.yaml` (all 4 callers) | **(a) — NO CURE NEEDED** (`merge_strategy_generations`); was listed (b) here, see correction below |
| `meta/meta_transfer.py` | `transfer/_index.yaml` (b), `reflection-` (b), `encoding-strategy.yaml` (b), **`goal-selection-strategy.yaml` (a)** | **MIXED — 3 FIXED, 1 correctly left bare** |
| `meta/meta_experiment.py` | `active-experiments.yaml`, `completed-experiments.yaml` | **(b) — FIXED** (both, 2 handlers) |
| `meta/strategy_apply.py` | `aspiration-generation-strategy.yaml` (b), **`goal-selection-strategy.yaml` (a)** | **MIXED — FIXED, routed per-basename at run time** |
| `meta/meta_dead_ends.py` | `dead-ends.jsonl` | (b) — **UN-CURED, 2 sites, tracked by g-115-4017** (confirmed on BOTH axes: no handler, AND it writes via `_atomic_write_with_fallback` while reading via `ensure_local` — the wedged shape, no raw-write exemption) |
| `meta/meta_impk.py` | `improvement-velocity.yaml` | **(a)** — `merge_improvement_velocity`; already carries the cure regardless. Its one apparent "bare lock" is a COMMENT describing the idiom it replaced, not a call — grep the executable line. |

#### Two corrections this table forced, both measured (g-115-3834, 2026-07-30, cc-04/Linux)

**1. A per-module row cannot carry a class — three of these modules are MIXED.**
The rows above used to give one class per module. `meta_transfer` writes four
paths through one `_persist` and `strategy_apply` drives two through one loop,
and in BOTH cases the paths straddle the split: `goal-selection-strategy.yaml`
is merge-protected while its same-directory, near-identically-named siblings are
fence-only. `meta_generations` was listed (b) on the stated grounds that its
`_persist` is "byte-identical in shape" to the fence-only ones — it is, and it is
class (a) anyway. Shape-similarity is precisely the inference `guard-1733`
forbids; registration is by BASENAME. Run the one lookup per PATH, never per
module, per directory, or per sibling.

**2. "Remediation cost is ONE conversion however many callers" is FALSE for the
`meta/` modules — it is one per CALLER.** The paragraph above (correctly, for its
own question) says to read the per-row counts as lock sites rather than callers.
That is right about how many *locks* exist and wrong about how much *work* the
cure is, and the two got conflated. Every `meta/` `_persist(ctx, path, data)`
receives `data` **already computed by its caller, outside the lock** — so
converting the helper alone cannot satisfy `locked_rmw`'s contract, which
requires the READ and the MODIFY to re-run inside the cycle on every attempt.
The read has to move to each call site. Measured: `meta_backpressure` is one
lock site and **five** handler conversions.

**3. A conflict-retry cure is not complete when the STORE is correct.**
`locked_rmw` re-runs the whole cycle, so anything else the cycle touches runs
again too. Two hazards the mechanical conversion introduces, both invisible to a
store-level assertion:

- **Non-idempotent side effects.** `meta_backpressure.evolution_check`'s cycle
  calls `_evolution_rollback`, which restores a file, appends a world stream,
  posts to the board, and **emails a human**. Wrapping it naively sends the
  email twice per conflict (measured: fired 2×). Cache the executed record per
  monitor and replay it on later attempts.
- **Hoisted accumulators.** Response lists built outside the cycle (`check`'s
  `rollback_actions` / `graduated` / `audit_only_skipped`) gain one duplicate set
  per retry while the persisted store stays correct, because each attempt
  re-reads fresh. A duplicated rollback is indistinguishable from a real one to
  every downstream consumer. Build them inside the cycle and return them.

**4. This table is a POPULATION, and a goal that inherits it inherits its
omissions.** g-115-3834 was titled "7 un-cured sites"; re-deriving the
population from the tree rather than from that list found an **eighth**
(`meta_dead_ends.py`, now g-115-4017). The module was in this table, correctly
classified (b), and simply never carried into the goal's target list. An
enumeration that under-counts reads exactly like a complete one — `guard-1715`:
read the COUNT, not the word "all". So re-derive from the tree before trusting
any remediation list built from this table, including one built from the
corrected table above. The probe is cheap:

```bash
grep -rn 'with file_locks\.locked(' mind_api/src --include=*.py
```

and note it also matches PROSE — `meta_impk`'s only "bare lock" is a comment
describing the idiom it replaced. Read the executable line before counting it.

Regression-guarded by `core/scripts/tests/test_meta_write_class_conflict_retry.py`
(stub backend; each invariant proven RED under reversion, including the
mixed-class routing in both directions).

`meta_yaml.py` is FIXED: `set_field`/`append_item` (g-115-3177) and
`_create_backpressure_monitor`/`_trigger_generation_transition` (g-115-3295).
Its `_persist` is retained but has **no callers** — treat a new call to any
bare-lock `_persist` on a class-(b) store as a defect.

**Exposure is not uniform across class (b)** — but the original basis for that
claim was wrong, and the correction is worth more than the ranking. This
paragraph used to say `experience.jsonl` and `working-memory.yaml` are per-AGENT
paths, so contention "needs the same agent live on two boxes — real (transplant,
multi-box fleets) but rarer." **Falsified** (g-115-3783): that is not the
trigger. Staleness is per-object AND per-box (rb-2639/rb-3280), and every box
mirrors every agent's files, so ANY cross-box write to a file stales the
OBSERVER's mirror — routine traffic in a ≥5-box fleet, not an edge case.
Measured: alpha's `experience.jsonl` read [DRIFT] on cc-05 during ordinary work,
with no transplant and no dual-live agent, and it wedged on EIGHT consecutive
write attempts before an external `refresh()` cured it in 0.7s. Rank remediation
by how many distinct writers a store has, not by call-site count — but do not
discount a per-agent path as low-exposure.

### The classifier is necessary but NOT sufficient (g-115-3719)

`merge_handler_for(path) → None` puts a store in class (b). It does **not** by
itself put the store in the class-(b) *hazard*. The wedge requires BOTH:

1. **no merge handler** (the classifier above), AND
2. **the writer goes through the fenced backend path** — `get_backend()`
   `.append_jsonl_record` / `.atomic_write` / `_atomic_write_with_fallback`,
   which is what takes the If-Match token.

A store written with a raw `open()` + `os.replace` never issues a fenced PUT, so
it has **no fence to go stale** and `ConflictError` cannot be raised on that
path. Wrapping such a writer in `locked_rmw` retries an exception the path
cannot produce, and falsely implies the store participates in the backend
conflict path.

Two stores in the table above are exactly that shape, and both are deliberate:

| Store | Writer | Why it is not converted |
|---|---|---|
| `working-memory.yaml` | `wm_write._write_wm` | Documented raw local write, audited 2026-06-02: per-agent single-writer (the DDB runner claim pins the agent to one box), hot path (RMW many times per iteration), and tier-model integrity — it reaches S3 via the own-cloud SWEEP, which carries its own conflict handling. Its real race (stale-lock-steal) already has a purpose-built cure: the `update_count` CAS retry in `_fileops.py` (g-115-1394). |
| `experience-meta.json` | `experience_write.meta_update` + `_update_meta` | Raw `json.load` + raw tmp/`os.replace`; a recomputable sidecar, not primary state. |

So: **check the write path, not just the basename registration.** The
grep in "How to classify a store" answers question 1 only. Question 2 is
`grep -n 'get_backend\|_atomic_write_with_fallback' <module>` — and note the
answer can be a docstring rather than a call, so read the executable line.

Both non-conversions are pinned by `test_scope_*` in
`core/scripts/tests/test_experience_conflict_retry.py`, which fail if either
writer ever joins the fenced path — the signal to convert it *then*, rather than
a claim that it never should be.

The class-(a) rows are safe **because they are registered**, not because their
writers are careful — every one of them uses the identical bare-lock pattern.
Registering a store is therefore an alternative cure to rewriting its writers,
and often the cheaper one.

**But registration is only valid for an APPEND-ONLY store.** A merge handler
reconciles two versions commutatively, so it can only ever produce the UNION of
what the two sides hold. If the store has a path that REMOVES records, a
concurrent removal and a stale peer mirror union back to the pre-removal state —
the merge resurrects exactly what the removal deleted, and does it silently,
reporting success.

That decided the cure for `experience.jsonl` (g-115-3719). Registration was the
cheaper option and was evaluated first, as the goal asked. It was rejected on
evidence: `archive_sweep` phase 2 rewrites live as
`[r for r in live_items if r.get("id") not in archived_ids]` — a removal. A
union-by-id handler would restore archived records to live while the archive also
holds them, which `_check_no_duplicate_id` treats as a corrupt state. So the
writers were converted to `locked_rmw` instead.

The general test, before reaching for the cheaper cure: **does any writer of this
store delete or filter records?** If yes, registration is not merely more
expensive to reason about — it is wrong. If no (a pure append log such as
`changelog.jsonl` or a board channel), registration is the better cure.

## The required pattern for a class-(b) writer

```python
def _cycle():
    data = _read_yaml(path, force_fresh=True)   # INSIDE the cycle — see below
    ...mutate data...
    _persist_unlocked(ctx, path, data)          # NOT _persist: locks aren't reentrant

file_locks.locked_rmw(path, _cycle)
```

Three separable invariants, each of which must be pinned independently:

1. **`force_fresh=True`** — re-takes the If-Match fence per attempt. Retrying
   against the same stale token conflicts identically forever.
2. **`locked_rmw`** — so a conflict retries at all.
3. **the read is INSIDE the cycle** — otherwise the retry re-applies a mutation
   computed from a pre-conflict snapshot, and the unlocked-RMW lost-update
   window stays open.

Read-only work that does not touch the RMW target (computing a baseline from a
different file, building a snapshot from other files) belongs OUTSIDE the cycle,
to keep the locked section small.

`file_locks.locked` is a plain `threading.Lock` and is **not reentrant** —
calling `_persist` (which takes the lock) from inside `locked_rmw` deadlocks the
daemon thread. That is why `_persist_unlocked` exists.

### Known residual: exhaustion is silent in the non-fatal side-effect writers

`_create_backpressure_monitor` and `_trigger_generation_transition` are each
wrapped in `except Exception: pass` (they are side effects and must not fail the
caller's write — a deliberate, pre-existing contract). Adding `locked_rmw` fixed
the wedge, but when the retries EXHAUST, the conflict is still swallowed: the
monitor is never created, or the generation never transitions, and nothing
records it.

Note the direction of that trade. Before the retry, these failed on the FIRST
conflict, so a wedge was total and eventually noticeable by absence. Now
transient conflicts are absorbed and the ONLY remaining failure is the
persistent one — the case most worth knowing about — and it is the case that
stays invisible. Anyone hardening these should keep the non-fatal contract and
make exhaustion *observable* (a counter or stderr line distinguishing "conflict
retries exhausted" from the other swallowed exceptions) rather than convert it
to a raise. The same `except Exception: pass` shape appears in the sibling
bare-lock modules in the table above; assume the same hole until checked.

## Testing a class-(b) writer

**A green suite proves nothing here.** Under `STORAGE_BACKEND=local`,
`conflict_error` is the empty tuple and `locked_rmw` degrades to a transparent
single pass, so all three invariants above can be reverted with the suite still
green — which is exactly why the original defect survived. Use the stub-backend
pattern in `core/scripts/tests/test_meta_yaml_conflict_retry.py` (the reference
suite) and **mutation-proof each invariant**: revert it, watch the test go red.

Assertion points that actually discriminate:

- invariant 1 → `backend.refresh_calls >= 2` on a retry (separates "re-read the
  local mirror" from "force-pull and re-fence").
- invariant 2 → the flaky-write call count reaches 2.
- invariant 3 → the lock is held **at READ time**. Asserting at *write* time
  does not work: a read hoisted out of the cycle still writes under the lock, so
  a write-time assertion passes the very revert it is meant to catch.

## Cross-references

- `core/scripts/coordination_merge.py` — `merge_handler_for` + `_HANDLERS`, the
  authoritative registry; adding a store to class (a) is a commutative handler
  plus one line here
- `core/scripts/_fileops.py` — `locked_rmw` / `_rmw_with_conflict_retry`
- `core/scripts/tests/test_meta_yaml_conflict_retry.py` — the reference suite
- `rb-2639` — per-object stale-IfMatch deadlock (why class (b) wedges)
- `rb-3636` — own-cloud write_conflict triage: retry-less / silent-loss /
  fence-wedge sub-mechanisms
- `rb-5250` — mutation-proof, because local-backend green is not evidence
- `guard-472` — new `locked_*` callers in `_fileops.py`
- `g-115-3177` (the meta_yaml cure), `g-115-3295` (this classification),
  `g-115-1899-b` (the correct-but-non-transferable aspirations conclusion)
