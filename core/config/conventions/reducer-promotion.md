# Reducer Self-Promotion — relaxing NEVER-PROMOTE behind six gates

**Status: SPEC SHIPPED, RUNTIME DEFAULT-OFF.** The decision logic exists and is
tested; nothing is wired to act on it. Promotion cannot occur until the
kill-test lane below has run and `fence_verified_at` has been written.

This is a deliberate spec-first S0 milestone (rb-4452): the invariant is fixed
in reviewable, merged form *before* its dependency exists, so it constrains the
kill-test lane's design instead of being retrofitted onto it afterwards.

## The problem

`worker-loop` Phase 0.5 declares **NEVER-PROMOTE**: no poll result yields
"become the reducer". Every ambiguity resolves toward wind-down. That is a
correct default and it has a cost — a stale claim waits for a human `/start`
however long it takes.

Measured 2026-08-14/15: a **15.7h reducer outage**. The claim sat 72min+ past
the ~65min ownership-stale threshold while a healthy same-agent worker on
another box had *already run the full discriminator set correctly* — peers
alive from that box (ruling out a local read-path wedge), an authoritative-store
claim read, and only-fresh-carrier-is-mine. Auto-promotion would have restored
the reducer roughly **14h earlier**.

## Why NEVER-PROMOTE existed — the steelman

**The broken-heartbeat-writer trap.** A reducer that is ALIVE with a stale
heartbeat is indistinguishable, from outside, from a dead one. This is not
hypothetical: an agent has been measured reading 59h stale while demonstrably
working. Promoting against a live reducer produces a **dual reducer**, i.e.
split-brain on every shared store.

Any relaxation must therefore refuse to promote on *unverifiability*. Winding a
worker down on "I cannot tell" is safe — it loses nothing, because the Body's
divergent working memory is staged for the reducer. Promoting on "I cannot
tell" is the split-brain itself.

## Why it is now relaxable

1. **The lease has a real `T_stepdown`.** `reducer_self_fence` gives the holder
   a defined step-down window: it stands down on seeing a different-holder
   claim, read from the *authoritative store* rather than from its own possibly
   broken heartbeat leg. The config invariant `T_stepdown < T_takeover` is
   already enforced and tested.
2. **Role derivation is already fluid.** `/start` rc=4 makes the framework pick
   the Body role, so a returning box auto-joins as a worker under a peer-held
   claim. Promotion at runtime extends the same philosophy rather than
   introducing a new one.

**The safety argument, in one line:** promotion waits for `T_takeover`, and
`T_stepdown` is strictly below it, so by the time a worker may promote the old
holder has already had a full step-down window to yield. Pinned by
`test_reducer_promotion.py::test_lease_ordering_invariant_holds_in_shipped_config`.
If anyone raises `stepdown_seconds` toward `T_takeover`, that test fails and
this argument must be re-derived before promotion is safe again.

## The three-module family — do not fuse them

| module | question | fail-safe |
|---|---|---|
| `worker_reducer_liveness` | is my reducer still alive? | wind-down |
| `reducer_self_fence` | have I been superseded? | hold (keep running) |
| `reducer_promotion` | may I BECOME the reducer? | hold (do not promote) |

Each carries its own fail-safe direction. Fusing them puts opposite defaults
behind one branch and the next edit silently inverts one of them (guard-2783).
The clearest expression of the divergence is what `rc=4` means in each:
**decisive** in liveness, **inert** in the fence, and
**necessary-but-not-sufficient** here. That three-way split is pinned against
the *real* sibling modules, not hand-copies, so a future fusion fails loudly.

`reducer_promotion` **reuses** the corrected poll rather than forking it: it
consumes the liveness verdict as an input and can only even consider promotion
when that verdict is already `wind-down`. It never re-implements polling or
parsing.

## The six gates

All must pass. There is no "mostly satisfied". Every unknown, unreadable,
unmeasured, or unrecognised input resolves to HOLD, and the returned reason
names *which* gate refused.

| gate | requirement | why |
|---|---|---|
| **G1 `enabled`** | config flag is literally `true` | master kill switch; truthy-but-not-True (`"true"`, `1`) does not count, so a YAML formatting accident cannot arm this |
| **G2 `fence_verified`** | `fence_verified_at` is a non-empty stamp | the fence's `T_stepdown` is proven by code and unit tests but not yet by kill-tests *on a live fleet*. Two independent flags mean neither is a single point of failure |
| **G3 `eligible`** | this box's machine id is in `eligible_machines` by **exact string equality** | see below |
| **G4 `liveness_decisive`** | liveness verdict is `wind-down` **and** its rc is decisive (`4`), not accumulated-transient | the load-bearing gate. Liveness also winds down on rc∈{1,2,3} and on a marker-less rc=0, which mean *unverifiable*, not *dead* |
| **G5 `claim_stale`** | claim age ≥ `T_takeover`, read from the canonical reader | guarantees the holder's step-down window has fully elapsed |
| **G6 `discriminators`** | all three are exactly `True` | rules out a local read-path artifact |

### G3 is exact identity, never a pattern

guard-2860: when loosening a fail-closed ownership/role gate, compute the exempt
set from **identity**, never relax the predicate to a glob or prefix. What a
loosened gate newly *admits* is not enumerable at review time when the predicate
is a pattern — its membership is decided later, by whatever happens to be on
disk.

So `eligible_machines` is a hand-listed set matched by exact string equality.
The cardinality of what it admits is a property of **that list**, reviewable in
the config, not of what a pattern happens to match on some box later. Near-miss
ids (a longer id containing a listed one, a prefix, a case variant, a
whitespace variant) are all refused, and non-string entries are dropped rather
than coerced. On-demand and laptop boxes stay off the list deliberately: a box
that sleeps is a box whose claim goes stale.

### G6 — the discriminator set

| name | rules out | measured by |
|---|---|---|
| **D1** `peers_alive_from_this_box` | a **local** read-path wedge. If peers read alive from here, "the reducer looks dead" is a fact about the reducer, not about this box | `liveness_check.py` (caller supplies) |
| **D2** `claim_read_authoritative` | a stale local read-through mirror standing in for the store of record | the claim read in `worker_reducer_liveness` (caller supplies) |
| **D3** `only_fresh_carrier_is_mine` | racing a live sibling Body that also holds a fresh heartbeat carrier | `reducer_promotion.measure_only_fresh_carrier_is_mine` |

`None` means **unmeasured**, and unmeasured is treated as **absent**, never as
satisfied. This is the archive-before-delete step-2 discipline applied to a
takeover decision: an unverifiable recovery layer is not a recovery layer.

#### D3 is measured as a UNION — store for siblings, local for self

`decide()` **takes** the discriminators and never measures them. For a while
nothing else measured D3 either, so the gate the whole safety argument rests on
was unmeasurable (g-306-297). The procedure below is the measurement; it is
written down here because both obvious implementations are unsound, **in
opposite directions**, which is exactly what makes picking one feel safe:

| read | failure | direction |
|---|---|---|
| **LOCAL only** | cannot see a sibling's carrier on another box — under own-cloud the local tree is a read-through cache (guard-980), so a carrier this box never pulled is simply absent, and D3 reads `True` while a live sibling holds one | **DANGEROUS** — this is the dual-reducer race G6 exists to prevent |
| **STORE only** | can miss this Body's **own** carrier in the window between the local write and the push, so D3 reads `False` **by absence** | safe, but it makes the gate unreliable rather than conservative — and an unreliable gate gets overridden |

So: **siblings from the authoritative store, self from the local file.** Reading
self locally is not a shortcut — it is the only read guaranteed to be able to
answer, because this process just ticked that carrier.

The sibling enumerator is **`worker_stall.enumerate_carriers`**, reused rather
than reimplemented: it already ranks authoritative-over-mirror, reports its own
completeness, and carries the g-306-247 read-error accounting. A private copy
here would be a second predicate for one question (guard-2783) and the two would
disagree the first time either is fixed.

#### D3 DEPENDS ON D2 — they are not three independent booleans

This is the part that is easy to miss when the list above reads as a flat set.
D3's sibling half needs the **same authoritative store** D2 is a statement
about. When the store did not answer, a mirror enumeration reports "no other
fresh carrier" for a reason that has nothing to do with the fleet — so:

> **D3 MUST be `None` whenever D2 is `False`, or whenever D3's own carrier
> enumeration did not read `authoritative` AND `complete`. Never `True`.**

Both conditions are required and are checked independently: the caller's D2, and
this measurement's own `read_via`/`complete`. An unreadable sibling carrier
(present, but with no parseable `ts`) also yields `None` — unknown freshness is
not absence, and treating it as stale would let a corrupt-but-live sibling be
promoted over.

#### Liveness-filter the population, and report what was scanned

Measured 2026-08-16 (zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic,
own-cloud) by enumerating every carrier in the authoritative store: **24
carriers across 5 agents, 7 fresh across 7 distinct hosts, 17 stale** — and
every one of the 17 belongs to a **dead session**, some 10+ days old. So ~71% of
the carrier population is dead-session residue, and a reader that does not
filter by freshness concludes "nothing is fresh" from a perfectly healthy fleet.
That error has already been made once on this same store.

The measurement therefore returns `(value, evidence)` and the evidence always
carries `carriers_scanned`, the fresh list, the stale and unreadable counts, the
window, and the read path. **A bare boolean here is unfalsifiable** (rb-245,
guard-1922) — and a `False` that came from an empty scan is indistinguishable
from a `False` that came from a live sibling unless the population is printed
beside it.

The freshness window is `worker_stall.DEFAULT_STALE_MINUTES`, deliberately not a
new constant. The fail-safe direction runs *with* that reuse: a **wider** window
over-counts fresh siblings and refuses a legal promotion, while a narrower one
under-counts them and promotes into a live sibling. If this measurement ever
needs its own window, it may only ever be **wider** than the stall probe's.

**Carrier accumulation is a different goal.** Nothing reaps dead-session carrier
files; that is the same 71% residue seen from the storage side rather than the
predicate side. Do not fold it in here.

## Promotion is LOUD

Post-notification, not pre-approval — the established notify-after /
revert-if-wrong trade. On promotion the implementing lane must, before acting as
reducer: post to the decisions board naming the old box, the new box, and both
claim ages; and notify the user through the registered notification path.
A failed announcement must never *block* the promotion (fail-open on the
announcement), but it must be recorded.

## Residual risks — stated, not eliminated

1. **A wedged (not dead) old reducer that unwedges mid-window** acts as reducer
   until its next heartbeat-tick sees the different-holder claim. The dual
   window is bounded by one tick cadence, and a wedged reducer was not merging
   anyway. Accepted, not closed.
1b. **A forked Body's carrier can be invisible to the store** — guard-3917
   failure mode (2): a same-box forked Body writes into an isolated worktree the
   sync layer never sees, so its carrier never reaches the authoritative store.
   A promoter measuring D3 from another box would find no fresh carrier for it
   and could read `True` while that Body is live. **This runs in the dangerous
   direction and the union does not close it** — the local half covers only
   `self`, and the store half cannot see what was never pushed. What bounds it
   is G3: `eligible_machines` is a hand-listed set of long-lived fleet boxes,
   and a forked Body on a listed box shares that box's agent dir, so its carrier
   *is* on the local path of a promoter running there. The exposure is a forked
   Body on a box that is not the promoter's. Accepted, not eliminated; closing
   it requires the fork to publish a carrier the store can see.

2. **A same-box reducer restart is invisible.** ~~The claims endpoint returns
   `{agent, machine_id, agent_state, heartbeat_at}` with no `runner_token`, so
   machine-id comparison cannot see it.~~ **CLOSED 2026-08-17 (g-306-224)** — but
   NOT by the closing condition this entry originally named, and the difference
   is the point. The entry read "closing the underlying gap needs the endpoint to
   expose the token"; that is a **capability leak, not a fix**, and any future
   reader who acts on the struck-through wording will build it. `runner_token` is
   the `ConditionExpression` bearer credential for `heartbeat` and
   `release_runner`: publishing it lets any reader forge a heartbeat for another
   agent — defeating `reclaim_if_stale`, so a crashed runner could never be
   reclaimed — or release a LIVE claim, forcing a healthy reducer down
   mid-flight. Both are the exact failures the lease exists to prevent, so the
   "fix" would have defeated the mechanism it was meant to strengthen.

   What actually closed it: the endpoint now returns `runner_token_fp`, a
   non-reversible truncated-SHA-256 digest. A consumer that only needs to notice
   CHANGE never needs the value — the digest is stable while the token is, moves
   on a re-mint, and authorises nothing. The raw token is not representable on
   `RunnerClaim` at all, so the projection cannot regrow the leak by one line.
   `worker_reducer_liveness` consumes it as a second takeover axis alongside
   `machine_id`. Full argument: `owncloud_backend.runner_token_fingerprint`.

   G4 remains the safety property regardless: a same-box restart produces a LIVE
   claim, so the liveness verdict is `continue` and promotion is never reached.
   The fingerprint improves DETECTION, not that guarantee.

Risk 1 is named here rather than omitted, because a design that reports only what
it looked at reads as coverage (guard-1760). Risk 2 is kept, struck rather than
deleted and NOT renumbered, for the same reason: a closed risk whose original
remedy was wrong is worth more on the page than off it, and the retirement
criterion below cites it by number.

## The kill-test lane — the bar for writing `fence_verified_at`

`fence_verified_at` may be written **only** after all three have been run on a
live fleet and recorded:

1. **Worker `/stop` leaves the reducer untouched.** Stopping a worker Body must
   not disturb the claim or the reducer's operation.
2. **Reducer kill lets takeover happen.** With the reducer killed, the claim
   goes stale past `T_takeover` and an eligible worker's gates all pass.
3. **The old reducer steps down on unwedge.** A wedged holder that recovers
   observes the different-holder claim on its next heartbeat-tick and stands
   down, rather than resuming as a second reducer.

Test 3 is the one that actually proves `T_stepdown`; tests 1 and 2 bound the
blast radius on either side of it. Until all three are recorded, G2 refuses
every input regardless of what G1 says.

### Test 2's bar is CIRCULAR without the dry-run — use `dry_run()` (g-306-296)

Test 2 says "an eligible worker's gates all pass". They cannot. `fence_verified`
IS G2 and `enabled` is G1, both default-off, and **G1 sits in front of G2** — so
`decide()` refuses before reaching any gate the test is about. Fed the most
favourable observation a real box could ever present (reducer provably gone,
claim age 999999s, every discriminator True, machine listed), it returns
`verdict=hold, gate_failed=enabled`. The bar is unsatisfiable by any fieldwork:
observing it requires first arming the flag the observation is the precondition
for.

The general form is worth carrying beyond this instance: **when a gate's
verification bar is stated as "observe the gated behaviour happening", check
whether the gate itself blocks that observation.** A fail-closed gate usually
does. The cheap test is to run the decision function with every input at its
most favourable value; if it still refuses, the bar is unreachable by
construction rather than merely unmet.

`reducer_promotion.dry_run()` is what test 2 runs instead. It evaluates G3-G6
against LIVE inputs while G1/G2 stay genuinely off, and three properties keep it
an observation rather than a bypass:

- `verdict` is **always** `hold`. The hypothetical answer lives in a separate
  `would_promote` key, so a caller branching on `verdict` — the field every
  other function in this family returns — can never be made to promote by it.
- It never writes config. The armed config is a shallow copy; the caller's dict
  is not mutated, and the simulated stamp is a deliberately implausible sentinel
  (`DRY-RUN-SIMULATED-NEVER-A-REAL-STAMP`) so a leak into any log or config
  announces itself instead of reading as a real verification date.
- It calls `decide()` rather than reimplementing the gates, so the two can never
  disagree about what G3-G6 mean, and `simulated_gates` names exactly which
  gates were faked. When the live config is already armed nothing is simulated,
  `armed_for_real` is True, and the run is reported as what it is — not a
  simulation at all.

Recording test 2 means recording `would_promote: true` together with
`real_gates_evaluated` and `simulated_gates`. A `would_promote` quoted without
the other two is not evidence: it does not say which gates were real.

### Composing the LIVE inputs — do NOT call `worker_reducer_liveness.poll()`

`dry_run()` is pure, so the lane supplies its own live inputs. The obvious way to
get the liveness pair is `poll()`, and it is the wrong one: **`poll()` writes
`sessions/<sid>/reducer-liveness-state.json` on every call and advances
`consecutive_errors`.** An observation would therefore mutate the wind-down
counter, and on a state-write failure `poll()` deliberately *flips its own
verdict to wind-down* — so a dry-run built on it could wind the observing Body
down. An observation mode that changes what it observes is not one.

Use the seam that already exists for exactly this:
`worker_reducer_liveness.py decide-only <rc> <observed> <expected> <consecutive>
[<threshold> <observed_fp> <expected_fp>]`
— its own docstring reads "Test/inspection seam: decide() over argv, no daemon,
no state file." Obtain `rc` and the holder from a read-only
`runner-claim.sh status`, pass them through `decide-only`, and feed the result
plus `measure_discriminators()` into `dry_run()`. Nothing on that path writes.

The two trailing `_fp` args are the runner-token FINGERPRINT axis (g-306-224),
and they are OPTIONAL in the argv sense only — omitting them silently drops a
whole class of takeover from the composed verdict. `machine_id` cannot see a
SAME-BOX reducer restart (a re-minted `runner_token` under an unchanged
machine_id), so a lane that passes only the machine pair will read that case as
CONTINUE. The observed fp is on the same `runner-claim.sh status` LIVE line the
holder comes from (`token-fp <digest>`, or the literal `unknown` from a daemon
predating the field), so obtaining it costs nothing extra. `unknown` and an
absent clause both mean UNKNOWN and are NON-discriminating — pass an empty
string, never the word.

The value is a non-reversible digest and never `runner_token` itself: the raw
token is the `ConditionExpression` bearer credential for `heartbeat` and
`release_runner`, so a lane that "improved" this by reading the token would hand
its holder the ability to forge a heartbeat or release a live claim. It is not
representable on `RunnerClaim` for that reason — see
`owncloud_backend.runner_token_fingerprint`.

## Retirement criterion (recorded at birth, guard-769)

The additive gradient has four enforcement layers; the subtractive gradient is
only advisory, so a new gate must say at birth what would show it is safe to
remove.

**Retire the `enabled` + `fence_verified_at` double gate** (collapsing to a
single always-on path governed by G3–G6 alone) when **both** hold:

- ~~the claims endpoint exposes `runner_token`,~~ **MET 2026-08-17 (g-306-224)** —
  the claims endpoint exposes `runner_token_fp`, a non-reversible digest, closing
  residual risk 2, so "reducer not live" no longer rests on a machine-id proxy
  alone. Do NOT read the struck wording as still-unmet and go expose the raw
  token: it is a bearer credential and publishing it would defeat the lease (see
  risk 2 above). This criterion asked for a signal that survives a same-box
  re-mint, and the digest is that signal; **and**
- promotion has fired at least 5 times across at least 2 distinct boxes with
  **zero** dual-reducer incidents recorded in the decisions board or the
  coordination channel.

**Retire this module entirely** if the fleet moves to an external lease manager
that provides leader election directly — at which point all three modules in the
family become adapters over that, and their hand-rolled fail-safe directions are
the thing to delete.

Telemetry that would show either: promotion attempts and their `gate_failed`
distribution. A gate that never refuses in production is either redundant or
never reached — check which before removing it.

## Files

| file | role |
|---|---|
| `core/scripts/reducer_promotion.py` | pure `decide()` (no I/O) **plus** the G6 measurement below it — `measure_only_fresh_carrier_is_mine` / `measure_discriminators`, which do I/O and sit below `decide()` so every gate branch stays drivable from a dict |
| `core/scripts/reducer_promotion.py` → `dry_run()` | the observation mode that breaks test 2's circular bar; pure, never returns PROMOTE, never writes config, calls `decide()` rather than reimplementing it. **No CLI** — the lane composes its own live inputs (see above) |
| `core/scripts/worker_reducer_liveness.py` → `decide-only` | the non-mutating seam for the liveness half of those inputs. `poll()` is NOT usable for observation: it writes state and can flip its own verdict to wind-down |
| `core/scripts/worker_stall.py` → `enumerate_carriers` | the sibling-carrier read D3 reuses; **not** reimplemented in `reducer_promotion` |
| `core/config/aspirations.yaml` → `reducer_promotion` | the three config knobs, all default-off |
| `core/scripts/tests/test_reducer_promotion.py` | negatives-first; pins the sibling divergence against the real modules |
| `core/scripts/tests/test_reducer_promotion_dry_run.py` | 26 tests, negatives-first; pins never-promote structurally (no value in the returned mapping may equal or embed `promote`), no-config-write, no-mutation, and that `decide()` cannot reach `dry_run()` |
| `.claude/skills/worker-loop/SKILL.md` Phase 0.5 | where NEVER-PROMOTE is declared; the **only** sanctioned wiring site |

**Wiring constraint (guard-2783):** wire promotion at the worker-loop poll site
only. It must **never** go into `heartbeat-tick.sh`, which both roles run — the
observation that means "you may promote" for a worker is meaningless-to-inverted
for the reducer executing the same line.
