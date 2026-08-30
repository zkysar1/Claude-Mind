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
**basename**, against the `_HANDLERS` dict (91 entries measured 2026-08-26 by
`py -3 -c "import sys;sys.path.insert(0,'core/scripts');import coordination_merge as m;print(len(m._HANDLERS))"`
— re-derive rather than trusting this number, and do NOT use
`grep -c '^\s*"[^"]*":\s*merge_'`, which this file prescribed until 2026-08-26:
it is not scoped to `_HANDLERS` and also matches the split-store kind->handler
map in the same file, overcounting by exactly the 2 extension-less keys
`guardrails` and `reasoning-bank` — 93 vs 91, measured. A counting command that
matches a second dict in the same file is the same defect class this section
already warns about one paragraph down), plus **SEVEN path-pattern branches that run
BEFORE the dict lookup**:

| # | Pattern | Effect |
|---|---|---|
| 1 | `.../team-state/agents/<name>.yaml` | registers — basenames are per-agent |
| 2 | `.../health/<YYYY-MM-DD>.jsonl` | registers — basenames are dates (g-306-118-e) |
| 3 | `<kind>-<YYYY-MM-DD>.jsonl` for reasoning-bank / guardrails | registers — routed to that store's OWN id-keyed handler, not append-only (g-358-05) |
| 4 | `gate-firings-<YYYY-MM-DD>.jsonl` | registers — routed to the legacy file's `merge_append_only_jsonl` (g-358-08 / g-328-51 cutover, 2026-08-17). Measured BEFORE registration: `merge_handler_for("meta/gate-firings-2026-08-17.jsonl")` was `None` — the hottest dynamic-basename store in the fleet (every box flushes into the same live segment at every iteration close) was class (b) while the legacy file it replaces had always been class (a). Cure chosen per guard-1816: handler-registration, NOT a writer conversion, because the writer (`gate-firings-flush.py` → `locked_modify_jsonl`) already satisfies the class-(b) pattern AND the segment has no removal path (store-hygiene G5's age-cap keys on the legacy basename), so a line-union can never resurrect a deletion |
| 5 | `core/config/**` | **un**-registers — a registered basename that also names an immutable framework config is deliberately NOT merged (g-115-3997) |
| 6 | `world/knowledge/tree/**/*.md` | registers — section-union (g-115-7071). 1,555 unique basenames make per-node registration structurally impossible, so this is the same unenumerable-basename cure as 1-4 applied to tree nodes rather than to a JSONL segment |
| 7 | `**/telemetry/**/*.jsonl` | registers — line-union (g-115-6947). **EXTENSION-DISCRIMINATED**, and matched at ANY depth. The same tree holds 1,145 `*.json` SNAPSHOTS against 6 `*.jsonl` streams; a snapshot is one JSON object (last-writer-wins) and a line-union over it concatenates two versions into invalid JSON — so widening this to a bare `telemetry/` directory prefix would turn the cure into the next corruption. The snapshots are additionally session-UUID / port-keyed machine-local telemetry, which guard-1055's scope correction (g-115-3863) routes to `.gitignore` rather than to this registry. Path-pattern rather than basename because the wedge is a property of the directory's write pattern: `zakpod1-thermal.jsonl` froze for 60 consecutive `diverged_skipped` sweeps, and a basename cure was re-litigated ONE DAY later when a second specimen (`bridge-sessions/<port>/…`) turned up frozen two levels down |

```bash
# authoritative, and cheaper than reasoning about it
py -3 -c "import sys; sys.path.insert(0,'core/scripts'); import coordination_merge as m; print(m.merge_handler_for('<repo-relative path>'))"
```

A non-`None` return means class (a). `None` means class (b).

**Do NOT substitute a grep of the dict for that call — it is wrong in BOTH
directions.** Branches 1-4 make a store class (a) with *no* basename entry,
so a grep reports a false (b) on every agent shard, every health ledger and
every date segment; branch 5 makes a path class (b) *despite* a basename entry,
so a grep reports a false (a) on the three colliding `core/config` names. The
grep form was
documented here as authoritative until 2026-08-02, and its first failure mode
(shards) already existed when it was written. Resolve per PATH, through the
function; a basename is an input to the answer, not the answer.

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

## The other prior question: may THIS BOX write this store? (ownership, g-365-12)

Classes (a), (b) and (c) all answer questions about the STORE. None of them
answers whether the BOX running the writer is permitted to push it — and under
own-cloud that is a separate gate with its own failure mode.

Agent-dir ownership is the LIVE DDB runner claim (`owncloud_sync._owned_agents`,
fail-safe empty set). A box owns only the agents whose runner claim it holds, so
an assistant-mode chat session — which holds no claim — owns NOTHING, and
`sync_file` skips every push under `agents/<peer>/**` with reason `peer_agent`
(`owncloud_sync.py:1858`). The write lands locally and never reaches S3. The
owned-prune is scoped to the agents root (`owncloud_sync.py:1541`), so `world/**`
is NOT ownership-gated — that asymmetry is what makes a cure possible at all.

Measured 2026-08-22 (zeta, `hostname` cc-02, own-cloud): `_owned_agents()` →
`['zeta']` with six other agent dirs present locally, every one a peer cache.
Local-vs-S3 id diff across all six: **`LOCAL_ONLY=0`** — so the stranding class
below is NOT a property of merely holding peer caches. It requires a session that
WRITES a peer-owned store, i.e. assistant-mode on a non-owning box.

**This is why a class-(b) store can be fully FIXED and still fail from one box.**
`agents/<agent>/experience.jsonl` has BOTH cures in place — `locked_rmw` at
`experience_write.py:506`, and a `force_fresh` in-cycle read via `_read_jsonl`
(`experience_write.py:164-168`) — and an add for a peer-owned agent from a
claimless box still cannot converge, because no fence can push an object the sync
layer declines to push. Read as a fence problem, it sends you to re-fix a writer
that is already correct; the symptom (`write_conflict`, identical on every retry)
looks exactly like a stale fence.

### `experience.jsonl` is PERMANENTLY disqualified from class (a)

Do not register a handler for it. `experience.py:933-936` (`cmd_archive_sweep`
phase 2) rewrites live as a strict subset —
`[r for r in live_items if r.get("id") not in archived_ids]` — which is
guard-1816's disqualifying signature exactly. A union handler would silently
resurrect every archived record at conflict time, on the box that lost the race,
and duplicate it against `experience-archive.jsonl`, which already holds it.

### The spool cure has a location trap

"Spool it like the utilization sidecar" does not port. That spool
(`_utilization_store.spool_path`) is **machine-local and never pushed**; it works
only because every box runs its own flusher against a SHARED world store. A
claimless box's flusher cannot write `agents/<peer>/**` either, so a
machine-local spool reproduces the stranding one level down — the rows sit in a
file no owner can ever read. Any spool cure here must land where the writing box
may actually push (`world/**`, per the asymmetry above) and be drained by the
OWNING box.

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
| `meta/meta_backpressure.py` | `backpressure.yaml` (all 6 `_persist` callers) | **RECLASSIFIED (b) -> (a) on 2026-07-31** (`merge_backpressure`). Was **(b) — FIXED** (5 mutating handlers; `status`/`cooldown_check` are read-only); that cure is RETAINED as redundant belt-and-braces — see the reclassification note below |
| `meta/meta_generations.py` | `strategy-generations.yaml` (all 4 callers) | **(a) — NO CURE NEEDED** (`merge_strategy_generations`); was listed (b) here, see correction below |
| `meta/meta_transfer.py` | `transfer/_index.yaml` (b), `reflection-` (b), `encoding-strategy.yaml` (b), **`goal-selection-strategy.yaml` (a)** | **MIXED — 3 FIXED, 1 correctly left bare** |
| `meta/meta_experiment.py` | `active-experiments.yaml`, `completed-experiments.yaml` | **(b) — FIXED** (both, 2 handlers) |
| `meta/strategy_apply.py` | `aspiration-generation-strategy.yaml` (b), **`goal-selection-strategy.yaml` (a)** | **MIXED — FIXED, routed per-basename at run time** |
| `meta/meta_dead_ends.py` | `dead-ends.jsonl` | (b) — **UN-CURED, 2 sites, tracked by g-115-4017** (confirmed on BOTH axes: no handler, AND it writes via `_atomic_write_with_fallback` while reading via `ensure_local` — the wedged shape, no raw-write exemption) |
| `meta/meta_impk.py` | `improvement-velocity.yaml` | **(a)** — `merge_improvement_velocity`; already carries the cure regardless. Its one apparent "bare lock" is a COMMENT describing the idiom it replaced, not a call — grep the executable line. |

#### Reclassification: `backpressure.yaml` (b) -> (a), 2026-07-31 (g-115-4310, alpha, cc-04/Linux)

`f6d6bd7eb` (closing g-115-4253) registered `merge_backpressure`, moving this
basename across the split. `test_scope_write_classes_are_what_the_cure_assumed`
fired exactly as its docstring intends — "the signal to re-derive the cure, not
a silent behaviour change in production." Re-derived here so the next reader
inherits the decision rather than repeating it (guard-1816 step 4):

- **The handler STAYS; the pin updates.** Unregistering it restores class (b)
  and re-opens the measured incident that motivated it — cc-06 could not
  integrate for 6.2 hours with 54 commits stranded behind
  `no ayoai-ledger handler for basename backpressure.yaml`.
- **The `locked_rmw` cure is RETAINED, not removed**, though class (a) no longer
  requires it. A reconciler now exists below the write, so a stale fence is no
  longer a permanent wedge and the cure is belt-and-braces rather than
  load-bearing. Stripping five handler conversions out of a store that has
  already stranded a box once is an unrequested behaviour change carrying real
  risk against no demanded benefit; the cost of keeping it is one extra refresh
  per write cycle.
- **The class change was a SIDE EFFECT, not a considered decision — and that is
  precisely why the pin exists.** g-115-4253 never mentions write classes,
  `locked_rmw`, or this convention; it asked for a *class-closing* remedy and
  explicitly warned "do not just add a third handler and close." The handler is
  sound on its own terms (its docstring carries the full guard-1816
  append-only-vs-draining-queue analysis), but its write-class consequence was
  invisible to the goal that produced it. A registry move nobody reasoned about
  still reaches this table, and only an executable assertion catches it.

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
store delete, filter, OR MUTATE records?** If yes, registration is not merely more
expensive to reason about — it is wrong. If no (a pure append log such as
`changelog.jsonl` or a board channel), registration is the better cure.

**"or MUTATE" was added 2026-08-09 (g-115-5457) — the test previously asked only
about removal, and that omission silently passes two of the four stores it was
applied to.** `merge_append_only_jsonl` dedups by SERIALIZED LINE and its
docstring is explicit that records must be immutable ("two non-identical lines
are distinct events"). So an in-place edit is not a milder version of a delete —
it is a *different* corruption with the same cure-invalidating force: the two
sides hold two versions of one logical record, the union keeps BOTH, and the
store now has two records for one id, each missing the other's edit. Removal
resurrects; mutation duplicates. A reader applying the removal-only test to a
store whose only hazard is mutation gets a clean answer and ships a regression.
(The journal.jsonl triage in `coordination_merge.py` had already found both legs
independently and called them "(1)" and "(2)"; this line lifts that into the
general test so it is not rediscovered per-store.)

#### Measured application: the five per-agent stores (g-115-5457, 2026-08-09, alpha worker Body, hostname cc-07, `uname -r` 6.8.0-136-generic)

The goal proposed registering five per-agent JSONL stores, noting they "are all
APPEND-ONLY by contract" and that registering them "may be a small, low-risk
change". Reading every writer disqualified **four of five** — a fourth
independent reproduction of this convention's "the lookup-only predicate
overstates the work" finding, and the first where the overstatement came from the
*append-only* claim rather than from machine-locality.

| store | verdict | disqualifying writer |
|---|---|---|
| `session/desync-warnings.jsonl` | **REGISTERED** (`merge_append_only_jsonl`) | none — 3 writers, all pure appends |
| `session/execution-diary.jsonl` | refused — REMOVAL | `execution-diary.py cmd_trim`, wired at `iteration-close.sh:2842` every iteration |
| `insights.jsonl` | refused — MUTATION | `insights-read.sh --mark-processed` sets `processed=True` on every entry and rewrites |
| `experience.jsonl` | refused — REMOVAL | `archive_sweep` phase 2 filter (already recorded above) |
| `experience-archive.jsonl` | refused — MUTATION | `experience_write.set_field` targets the archive when the id lives there |

Three things worth carrying beyond the table:

- **Two of the four were caught only by the MUTATION leg** added above. Under the
  previous removal-only test both would have registered clean.
- **The store that most needs the cure is the one that cannot have it.**
  `execution-diary.jsonl`'s trim is itself a backend-bypassing
  `open(tmp,"w")`+`os.replace` with `ensure_ascii=False` — i.e. it *causes* the
  byte divergence that strands the fence — and that same trim is what forbids
  registering the handler that would heal it. Its cure has to be a different
  shape (route the trim through the backend), not this one.
- **`locked_rmw` does not cure this wedge**, so the two `experience` stores are
  not covered by being "(b) — FIXED" in the census above. That cure retries a
  conflict; this failure is a fence that can never match, so the retry loops.
  Both facts are true at once and the table's "FIXED" is scoped to the first.

The `insights.jsonl` basename collision with
`agents/<a>/.history/snapshots/insights.jsonl` was checked and is **not** a
hazard: `.history` is in `_EXCLUDE_DIRS`, so the snapshot is machine-local and no
handler can reach it. Note which leg answered that — the **directory prune**, not
the basename policy. `_is_machine_local` alone returns `False` there and would
have reported a collision hazard that does not exist, which is the same one-leg
trap this convention documents above, firing in the opposite direction (a false
POSITIVE rather than a false negative).

### Registration is not a cure if the HANDLER itself can lose data (g-115-5294, 2026-08-08, alpha worker Body, hostname cc-07, `uname -r` 6.8.0-136-generic)

Everything above decides whether a store may be *registered*. This applies
AFTER registration: a class-(a) store is only ever as safe as its handler, and a
handler that picks a whole sub-document by LWW drops every key the loser held
alone. `team-state.yaml` is registered and passes both axes of the table above —
and `_merge_strategic_focus` (`coordination_merge.py` L800-826) was losing
one-sided keys the whole time. So "class (a)" answers *whether* a reconciler
runs below the write, never *whether it conserves*.

**The cure, and why its two halves are ONE change.** The merge half is a
loser-first overlay — `out = dict(lose)` then `out.update(win)` — so every key
the winner carries still wins (including an explicit `None`) while a key absent
from the winner survives from the loser. The writer half stamps
`strategic_focus.set_at` inside `team-state.py`'s `strategic_focus` branch
when a `strategic_focus.*` field is written and the write does not set `set_at`
itself. Fixing only the merge leaves a real amendment losing to a stale
peer, because nothing bumped the field the ordering reads; fixing only the writer
makes the winner correct and still discards the loser's one-sided keys. Neither
half is sufficient, which is why they share one suite
(`core/scripts/tests/test_strategic_focus_merge_and_stamp.py`) and one
coupling assertion that fails if EITHER is reverted.

**The bump must cover the LWW-RESOLVED fields and NO OTHERS (g-115-8292,
2026-08-29).** This clause read "whenever ANY `strategic_focus.*` field is
written" until then, and that breadth was itself a defect, because one of the
five schema keys is not LWW-resolved at all: `_merge_strategic_focus` UNIONS
`acknowledged_by` after the winner overlay, so its value never depends on which
side won the `set_at` pick. Acknowledging a directive is therefore not an
amendment, and bumping on it falsified the very ordering the g-115-5294 cure
installed — measured first-person 2026-08-29T14:32:34 (`history.py diff`): one
`--field strategic_focus.acknowledged_by --operation append` moved `set_at`
02:50:03 → 14:32:34 while `set_by` stayed `zachary`, erasing 11.7h of directive
age and handing the ACKER a stamp newer than the OWNER's real one. Since the
merge resolves `primary` by that stamp, an ack from any box could then overwrite
a peer's genuinely newer owner directive — and acking is the correct, expected
response to a directive, so the field was corrupted by agents doing exactly the
right thing (three peers had already bumped it before the defect was seen).

The generalisation for any store adopting this pattern: **a same-mutation
timestamp bump belongs on exactly the fields whose merge outcome READS that
timestamp.** Enumerate the handler's per-key rules first — a key resolved by
union, by max, or by any rule independent of the ordering stamp must be exempt,
or normal writes to it silently forge the ordering for every other key.

**Write it as an ALLOWLIST, never a denylist**, and this is the half worth
carrying to any other store, because both shapes pass every test you would
think to write today. The landed form names the content subfields that DO bump
(`primary` / `rationale` / `set_by`, plus a whole-map `strategic_focus` write)
and bumps on nothing else. A denylist — bump everything EXCEPT
`acknowledged_by` — is behaviourally identical **on today's five-key schema**
and silently wrong on tomorrow's: a future subfield that the handler unions,
maxes, or otherwise resolves independently of the stamp would default to
BUMPING and reintroduce this exact defect, with no test failing. Under the
allowlist a new subfield defaults to NOT bumping, and the asymmetry decides it:
the cost of not bumping is a fall back to the `_canon` content tiebreak (mild),
while the cost of bumping is directive loss.

This was settled by measurement, not preference. Two Bodies of one agent fixed
g-115-8292 concurrently on 2026-08-29 (a claim-overwrite collision) and landed
the two shapes independently — denylist and allowlist. The allowlist was kept
for the reason above and the denylist discarded.

A whole-map `--field strategic_focus` write bumps under both shapes, and should:
`_set_nested` REPLACES the sub-document, so even a payload carrying nothing but
`acknowledged_by` DELETES `primary`/`rationale`/`set_by`/`set_at`. A draft that
exempted it by key-set was caught by a regression test failing with a bare
`KeyError: 'set_at'` — worth knowing before anyone "simplifies" the whole-map
branch back out.

**Both writer copies carry the predicate, and BOTH must be under test.**
`core/scripts/team-state.py` is the mirror; `mind_api/src/world/team_state_write.py`
is the LIVE path, because `team-state-update.sh` is daemon-only. guard-2323 is
recorded against this very block for exactly that reason — the g-115-5294 bump
originally landed CLI-side only and was inert for as long as nobody looked. The
CLI arm alone is NOT sufficient coverage: measured 2026-08-29, reverting only
the daemon copy to the unconditional bump left every CLI test GREEN. The suite
therefore carries a daemon arm and a CLI/daemon parity matrix that asserts both
copies return the same verdict per field AND that the expected verdict is still
what the matrix claims (so it cannot go vacuous by both copies ceasing to bump).
Note `test_daemon_cli_mirror_parity.py` does not cover this: it asserts
`EMPTY_STATE` field-sets, not writer behaviour.

**The general test is NOT "does the handler union one-sided keys".** Two cure
shapes are both valid, and demanding the wrong one produces false findings:

| shape | example | conserves because |
|---|---|---|
| enumerate every schema key | `merge_team_state` overrides all 5 merge-worthy top-level keys after its LWW base | no named key's fate depends on which side won; only opaque/future keys ride along, deliberately |
| union-backfill the key set | `_merge_goal` / `_merge_aspiration_record` loop `sorted(set(a) | set(b))` | a key absent from the base is taken from whichever side has it |

`_merge_strategic_focus` did NEITHER — its 5 schema keys were neither enumerated
nor unioned — which is the actual defect signature. So the question to ask of any
handler is: **for every key in the schema, is there a rule whose outcome does not
depend on which side won the base-pick?**

**Population, measured not asserted.** Nine LWW-base sites in
`coordination_merge.py`; re-derive rather than trusting that number:

```bash
grep -n 'out = dict(win)\|out = dict(lose)' core/scripts/coordination_merge.py
```

Three carry a union-backfill (`_merge_aspiration_record` g-115-4163, `_merge_goal`
g-115-5017, and this one) — so this is a RECURRING class with two prior cures at
other sites, not a one-off. I audited exactly two of the remaining six:
`merge_team_state` conserves by enumeration (so the child fix above is the
complete fix for this path, not a partial one) and `merge_team_state_shard`
whole-snapshot LWW is deliberate and documented. **The other five are unaudited by
me** — do not read the count above as five defects, and do not read it as five
non-defects either.

**Two corrections to g-115-5294's own record, both measured.** It locates
`_merge_strategic_focus` at L782-790 (actually L800-826 — line numbers had
drifted, so grep for the symbol rather than editing from the cited range), and it
states the amendment "currently wins by being longer". It does not: `_canon`
orders **lexicographically, not by length**, so on an exact `set_at` tie the
winner is whichever canonical form sorts higher — measured, a 63-char document
beat a 101-char one. That error is not cosmetic; it made the first two versions
of this cure's own coupling test set up the *opposite* of the adverse case they
claimed, and pass vacuously.

**WHAT AN AMENDER MUST DO ABOUT IT — the tiebreak is not a corner case, it is the
merge rule whenever the LWW key is COARSER THAN THE EDIT CADENCE** (`rb-6977`;
third instance g-115-5171, bravo, cc-05). The correction above explains the
mechanism; this is the operating procedure.

- **Where it binds.** Two prior instances had a FROZEN timestamp by house style
  (forged-skills `triggers`, g-115-3638; `strategic_focus` text, rb-6977) — an
  amender who bumped the stamp escaped it. The `_tree.yaml` per-node case is
  STRUCTURAL and offers no such escape: `_classify_tree_field` is TOTAL and
  defaults to BASE, BASE rides the newer-`last_updated` LWW base, and a node's
  `last_updated` is DATE-granular by contract (g-001-67; g-115-1683 deliberately
  does not bump it on a field poke). Two same-day edits therefore can NEVER be
  ordered by recency, so `_order_by_ts` falls to the content tiebreak **every
  time**. Ask of any store: is my LWW key coarser than how often this field is
  edited? If yes, you are always in the tiebreak.
- **Simulate the tie before you append.** The comparison is
  `_canon(va) >= _canon(vb)` — lexicographic, and it decides at the FIRST
  DIVERGENT CHARACTER. Run both candidate strings through `_canon` and compare
  them yourself; do not reason about it from length or from which edit came
  later. (`guard-1703` — never rely on this tiebreak to preserve an amendment.)
- **The remedy is the leading character.** Because the append's first divergent
  character decides, a **space-led** append sorts BELOW the pre-amendment text
  and is silently reverted by the next peer merge, while a **newline-led** append
  sorts above and survives. Lead the appended block with a newline.
- **Why this is written as a hazard rather than a preference.** The loss is
  SILENT and it is invisible to the amender: two prior amenders of the same field
  were exposed unknowingly, and the revert happens on a peer's merge, not on the
  amending box. There is no local signal to notice.

**Verified vs not.** Mutation matrix: baseline 11/11; restoring `out = dict(win)`
kills `test_loser_only_key_survives`; removing the stamp bump kills
`test_writing_primary_bumps_set_at` AND the coupling test. Writer-side tests run
as subprocesses with `STORAGE_BACKEND=local` pinned explicitly (guard-955). NOT
verified: no two-box live convergence run — the handler is exercised against
fixtures, so the argument that both machines emit identical bytes rests on the
commutativity tests, not on observation.

### Decision: tree-node `.md` — the BASE IS ABSENT, and that decides the shape (g-115-6954, 2026-08-21, echo, hostname cc-03, `uname -r` 6.8.0-137-generic)

> **LANDED — tree-node `.md` is now class (a), not class (b) (g-115-7071,
> 2026-08-22, echo, cc-03).** Candidate (d) below is implemented as
> `coordination_merge.merge_tree_node_md`, dispatched by a sixth path-pattern
> branch in `merge_handler_for`, with the shared core extracted to
> `core/scripts/_section_merge.py` so the hyphen-named journal merge driver and
> this handler hold ONE copy. The analysis below is the decision record and
> stands as written; read this box for what actually ships.
>
> Two things the implementation added that the decision did not anticipate:
>
> 1. **A canonical argument order is REQUIRED for commutativity, and the naive
>    handler does not have it.** `merge_sections` emits ours-then-theirs, so with
>    the base absent every section is an addition and output order follows the
>    CALLER's arguments — `merge(a,b)` and `merge(b,a)` keep identical content but
>    differ byte-for-byte. Two boxes see opposite `(ours, theirs)` by construction,
>    so each writes a different byte string for the same logical merge: different
>    hashes, perpetual mirror divergence, on a file the merge just reported success
>    for. The handler now sorts the two SIDES into a canonical order first. Note
>    this is the exact failure the "mirror requires identical bytes" argument above
>    predicts, and it was still missed at first — the pin that claimed to enforce
>    commutativity compared `sorted(out.split())`, a token multiset blind to
>    ordering, so it passed against a non-commutative handler.
> 2. **The resurrection residual is DEMONSTRATED, not assumed** — pinned by
>    `test_KNOWN_RESIDUAL_a_deliberate_section_eviction_resurrects`, with detection
>    tracked by **g-115-7176** (sibling of g-115-4357, which covers the same
>    resurrection class for `ayoai-ledger`'s id-union over experience/journal).
>
> Pins: `core/scripts/tests/test_tree_node_md_merge.py`, 12 tests, each
> mutation-validated (drop the conflicts refusal → 1 F; drop the decode guard →
> 1 F; drop the canonical order → 2 F; widen dispatch to a catch-all → 3 F).
> **Still NOT verified:** no two-box live convergence run — same caveat the
> paragraph above this section records for the other handlers.

**1,555 live nodes** under `world/knowledge/tree/` are unregistered, so every one
is class (b): a both-diverged 412 freezes it permanently with no operator step.
Measured cost of that freeze on one node — `vinheim-revenue-economics.md`, on the
owner's stated top revenue priority — 516 sweeps frozen, this box reading 35,959
stale bytes against an authoritative 42,201, missing an entire measurement-correction
section. Detection worked; nothing cured it.

**The deciding measurement is that no base exists, and none can.** The contract is
`merge_handler_for(path) -> Optional[Callable[[bytes, bytes], bytes]]`
(`coordination_merge.py:5021`), called as `handler(body, remote_bytes)`
(`owncloud_backend.py:1632`) — **two** arguments. `.history` holds 111,264 files but
**ZERO** snapshots for tree-node `.md` (positive control: 1,555 live nodes; the 5,229
files under `.history/knowledge/tree/` are all `_tree.yaml`). And a recovered base
would not help even if it existed: history is **per-box**, and the mirror requires both
machines to independently compute *identical bytes*. Git converges on one commit; the
mirror does not. A per-box base is disqualifying by construction, not merely awkward.

**What that costs the surviving candidate.** Base-free section-union cannot distinguish
"ours ADDED section B" from "remote DELETED section B" — it resurrects, which is
guard-3645's evicted-section hazard, and not hypothetical here: read-cap work trims
nodes deliberately.

**But resurrection does not favour the status quo, and this is easy to get backwards.**
Under the freeze this box keeps reading its stale local copy — *which still contains B*.
So resurrection leaves this box's view of B unchanged while it GAINS every other section
that landed remotely. Against the freeze specifically, section-union is a strict
improvement on the axis actually measured. Resurrection is a real defect of the
candidate that needs its own detection, not a reason to keep freezing.

**Losing candidates, and why each loses for a DIFFERENT reason:**

| candidate | verdict | why |
|---|---|---|
| (a) whole-file LWW on front-matter `last_updated` | REJECTED | **The same-date two-section case.** Two agents appending *different* sections on the same date produce equal `last_updated`; LWW picks one whole file and the loser's section is lost entirely. The tie-break is also lexicographic, not by length (see the `_canon` correction above), so which file survives is arbitrary. |
| (b) front-matter LWW + body **line**-union | REJECTED | Not for (a)'s reason. rb-3683: line-level auto-merge on markdown can DROP an interleaved section body while KEEPING its heading — silent corruption *inside* a section. |
| (c) status quo (freeze + hand-union) | REJECTED as the DEFAULT, retained as the RESIDUAL | Narrows from "every concurrent edit freezes" to "same-heading divergence freezes", which is the only case where freezing is the right answer. |
| (d) section-union by `## heading` | **SURVIVES** — and LANDED in g-115-7071 (see the box above; not landed in g-115-6954 itself) | Section-granular: a section is wholly present or wholly absent, never half-merged. That granularity is the entire distinction from (b). Wins the same-date case outright, because the two sections have different headings. |

**Why (d) is cheap:** the pure core already exists and needs no new merge logic —
`core/scripts/git-merge-journal-md.py::merge_sections`, the same relationship
`ayoai-ledger` has to this file's handlers. Shape as specified here, and as
shipped in g-115-7071 — except that the real handler also sorts the two sides
into a canonical order before this call, without which it is not commutative
(see the LANDED box above), and it decodes STRICTLY so an undecodable side
refuses rather than being read as empty:

```python
merged, conflicts = merge_sections(b"", ours, theirs)   # b"" == the absent base
if conflicts:            # REFUSE -> backend keeps its safe-freeze for this path
    return None
return merged
```

It must **NEVER** write git conflict markers into a knowledge node — that is the
`.jsonl` marker-corruption class applied to prose.

**Precondition landed with this decision (g-115-6954):** two defects in that core, both
live for weeks. `split_sections` reattaches a trailing blank line to the *preceding*
block, so raw block-list comparison found byte-identical content unequal whenever the two
sides' *neighbouring* sections differed — a spurious conflict in the normal cross-box
case; fixed at all three raw-comparison sites by comparing `_join(...)`, the form the
conflict emitter and final assembly already use. Separately the both-sides branch never
consulted `base_map` at all, so **any** one-sided section edit conflicted even when the
other side was provably unchanged since base; fixed with standard 3-way semantics. The
second was found by a control whose expected value the author got *wrong* — the failure
is what surfaced it.

**BLOCKING CONSTRAINT ON (d), found by the fresh-eyes pass on this very change and
NOT predicted — the recommended shape does not converge as-is.** `merge_sections` is
content-commutative but **NOT byte-commutative for UNTIMED headings**. `_sort_index`
returns `(1, 0)` for every heading without a leading `HH:MM`, and the stable sort then
preserves INSERTION order, which is ours-first-then-theirs-only. Swapping the arguments
swaps that order. Measured:

```
base="", ours="## Alpha\n1\n\n## Beta\n2\n", theirs="## Gamma\n3\n"
  fwd: ## Alpha / ## Beta / ## Gamma   md5 47e36a69
  rev: ## Gamma / ## Alpha / ## Beta   md5 0b5f8504     <- SAME content, DIFFERENT bytes
```

Timed sections are byte-identical (md5 `ce8e825f` both ways) — which is why the daily
journal, whose headings are `## HH:MM`, has never hit this. **Tree-node headings are
prose and therefore almost always untimed**, so this fires on exactly the population
(d) was recommended for. Two boxes would each emit a different byte sequence for the
same merge and the mirror would re-diverge on the next compare — the failure this whole
decision exists to prevent. The implementation (g-115-7071) MUST impose a TOTAL order on
untimed headings (lexicographic by heading text is the obvious candidate) before (d) can
be registered; content-commutativity alone is NOT sufficient for a mirror handler, and a
merge that "works" in every content test can still fail to converge.

**Verified vs not.** VERIFIED: content-commutativity on clean merges (5 cases, both
argument orders — equal line multisets, which is what that test actually asserts);
BYTE-determinism for TIMED headings only, REFUTED for untimed (above); the absent-base
measurement above with its positive control;
the handler signature read from source, not inferred; `merge_sections` matrix 7/7 with
four conservative controls (genuine divergence still conflicts, deliberate deletion still
honoured, repeated heading not collapsed, disjoint append both kept); the driver's own
suite 23/23. **NOT VERIFIED:** no two-box live convergence run for tree nodes — the
commutativity argument rests on fixtures, not observation; and the resurrection *rate* is
unmeasured, since nothing counts how often a node section is deliberately evicted
fleet-wide.

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
- `guard-1816` — enumerate the writers before choosing a merge semantic; step 4
  is "record the decision here so the next reader inherits it"
- `guard-1153` — the cross-granularity transplant a one-sided-key cure must NOT
  become (preserve absent keys; never field-level tiebreak a key both sides hold)
- `core/scripts/tests/test_strategic_focus_merge_and_stamp.py` — the coupled
  merge+writer suite for the class-(a) handler-conservation defect above
- `g-115-3177` (the meta_yaml cure), `g-115-3295` (this classification),
  `g-115-1899-b` (the correct-but-non-transferable aspirations conclusion)
