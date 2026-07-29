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
**basename**, against the `_HANDLERS` dict (73 entries as of 2026-07-28), plus
one path-pattern branch for per-agent team-state shards
(`.../team-state/agents/<name>.yaml`, whose basenames are dynamic).

```bash
# authoritative, and cheaper than reasoning about it
grep -n '"<basename>": merge_' core/scripts/coordination_merge.py
```

A hit means class (a). No hit means class (b). Registration is by basename
only — a store's directory, purpose, and sibling files are all irrelevant.

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
| `meta/meta_backpressure.py` | `backpressure.yaml` | (b) — 1 site |
| `meta/meta_generations.py` | `strategy-generations.yaml` | (b) — 1 site |
| `meta/meta_transfer.py` | transfer index + 3 strategy files | (b) — 1 site |
| `meta/meta_experiment.py` | `active-experiments.yaml`, `completed-experiments.yaml` | (b) |
| `meta/strategy_apply.py` | strategy files | (b) |
| `meta/meta_dead_ends.py` | dead-ends records | (b) |
| `meta/meta_impk.py` | `improvement-velocity.yaml` | (b) — already carries the cure |

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
