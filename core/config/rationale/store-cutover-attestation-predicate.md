# Rationale: Store-Cutover Attestation Predicate

Referenced from `core/scripts/store-cutover-check.py` (`_prove_commit`,
`_symbol_report`, `_local_report`). Records the DECISION taken in g-358-23 on
what a box must prove before a store cutover flag may flip, and why.

Decision date 2026-08-21 (zeta, cc-02). Status: **IMPLEMENTED 2026-08-22**
(zeta, cc-02) — prerequisite `_calls_symbol` fix in g-358-27, the tier itself
in g-358-28. See § Implementation route for the per-step record and the
post-implementation measurement.

## The question

`store-cutover-check.py` refuses to flip a cutover flag until every live box is
proven reader-capable. For `STORES['utilization']` the gate has been UNSAFE and
is close to unsatisfiable: measured 2026-08-20 on cc-07, 3 boxes attested and 2
(echo, foxtrot) did not — **both of them actually reader-capable**, blocked by
62 insertions in `core/scripts/retrieve.py` containing zero seam tokens
(unrelated `_BLEND_STATS` work from g-115-6860). Consumer churn on origin/main
runs ~2.9 commits/day, one roughly every 8h; SAFE requires all five boxes to
each commit after the newest consumer change with nothing landing in between.
On a continuously-moving dev tier that is close to unsatisfiable by
construction.

This is NOT a bug report. `--help` derives the byte-identity rule verbatim and
calls the gate fail-closed BY CONSTRUCTION, because it guards a silent false
all-clear on a shared own-cloud store. The conservatism is deliberate. What was
never measured is its COST.

## Why byte-identity is the wrong predicate HERE

The three checks in the file answer two different questions:

| check | scope | question it answers | predicate BEFORE g-358-28 |
|---|---|---|---|
| `_symbol_report` | once, vs `origin/main` | is MAIN reader-capable? | symbol-presence (`seam_symbol`) |
| `_prove_commit` | per remote agent | is THAT BOX reader-capable? | **byte-identity to main** |
| `_local_report` | this box | is THIS box reader-capable? | byte-identity **+** symbol-presence |

(The column is deliberately past-tense: this section is the decision-time
analysis. `_prove_commit` gained tier 2 and the symbol field became a SET in
g-358-28 — see § Implementation record. `_local_report` kept byte-identity as a
hard requirement by design; only its symbol half widened to the set.)

`_symbol_report`'s own docstring states the architecture and, in doing so, names
the defect: symbol-presence is checked once against main as a fleet-level veto,
and *"byte-identity then carries it to every box that matches."*

**Byte-identity is being used as a TRANSPORT for the symbol property.** That is
why it demands equality over the entire consumer file set: it is moving a
narrow property ("these files route to the seam") using a maximally broad
carrier ("these files are identical"). The carrier is sound and it is far
stronger than the cargo. Every unrelated edit to any of 17 consumer files
breaks the transport without touching the property.

The registry already says so in the `gate_firings` entry, for the store that
declares a seam symbol: *"this cutover's local predicate was never 'match
origin/main', it was 'the consumers CALL the seam' — a strictly stronger
content check that byte-identity cannot express."* That lesson was recorded and
then not applied to the stores that lacked a `seam_symbol`. Unit 14 of g-358-05
independently reached *"BYTE-IDENTITY IS THE WRONG PREDICATE AND I RECORD IT SO
THE NEXT UNIT DOES NOT REPEAT IT"* — but scoped it to the downstream
(Claude-Mind) leg only, so it never reached the fleet half, which is the half
actually blocking. A correct lesson recorded against the wrong scope is
invisible to the code path that needed it.

## The decision

**Per-file-scoped symbol-set routing, layered UNDER byte-identity, fail-closed
by default.** Four parts:

1. **`_symbol_report` vs `origin/main` is unchanged.** It is the only check that
   answers "is main itself reader-capable", and no per-box check can replace it
   — if main loses the call, every box matches a broken main and the whole fleet
   reports proven. Ancestry cannot see that either: the seam commit stays an
   ancestor of a commit that reverts it.

2. **`_prove_commit` gains a SECOND TIER.** Byte-identity is tried first and
   still wins when it holds — it is strictly stronger and costs one `git diff`.
   Only on divergence does the second tier run, and it is scoped to the
   **diverging files only**: for each path in `changed`, read it at the box's
   own proof commit (`git show <commit>:<path>`) and require it still routes to
   the seam. Files that did not diverge need no check; byte-identity already
   settled them.

3. **`seam_symbol` (one string) becomes `seam_symbols` (a set), matched
   "calls ≥ 1".** This is forced by measurement, not preference — see below.
   `gate_firings` migrates as a one-element set, so its behaviour is unchanged.

4. **A store that declares NO `seam_symbols` keeps byte-identity as its sole
   predicate, unchanged.** This preserves the fail-closed property: the narrower
   tier is opt-in per store and cannot be reached by a store whose seam nobody
   has characterised. `utilization` declares none today — which is the direct
   reason it is stuck, and the first thing implementation must fix.

The tier MUST report a DISTINCT reason (e.g. `seam_routed_despite_divergence`)
rather than reusing the byte-identity `proven` shape, so a reader can always
tell which predicate carried a verdict. Never silently equate the two.

## Why a symbol SET, not a symbol — measured

Checked all 17 registered `utilization` consumers at `origin/main` against the
7 reader-API symbols (2026-08-21, cc-02):

| symbol | consumers calling it |
|---|---|
| `load_counters` | 12 / 17 |
| `utilization_of` | 12 / 17 |
| `store_paths` | 4 / 17 |
| `load_all_counters` | 3 / 17 |
| `counters_path` | 1 / 17 |
| `segment_name` | 1 / 17 |
| `UTILIZATION_COUNTERS_SPOOLED` | 0 (it is a CONSTANT — see below) |

**No single symbol exceeds 12 of 17.** The `gate_firings` model — one symbol
every consumer calls — is structurally unable to express this store, whose
consumers legitimately use different parts of one reader API. A set matched
"calls at least one" covers 16 of 17; the 17th is a false negative, not a gap
(next section). Requiring "calls ALL" would be unsatisfiable by design.

## Two measured false-negatives that MUST be fixed first

Both are fail-closed, so neither can produce a false all-clear. Both make the
new tier **permanently unsatisfiable**, which is the failure mode to fear here:
a gate that can never be satisfied is worse than one that is merely strict,
because its refusals look identical to genuine ones.

**(a) Aliased deferred imports.** `_calls_symbol` tests for `NAME(` after
deleting the substring `import NAME`. `core/scripts/_curation_predicate.py`
reads as calling zero of the 7 symbols — but it *does* call one, at L88-89:

```python
from _utilization_store import utilization_of as _uo
util = _uo(rec, counters)
```

The call site is `_uo(`. The only literal `utilization_of` in the file is on the
import line, which `_calls_symbol` strips. So the sole zero-token consumer in
the table above is an artifact of the checker, not a consumer off the seam.

**(b) Constants are untestable.** `_calls_symbol` matches `NAME(` only, so it
can never match a bare constant. `UTILIZATION_COUNTERS_SPOOLED` — the cutover
FLAG itself, and the most seam-defining token in the set — appears bare in 2
consumers and is invisible to the checker. Verified by direct probe: source
containing `if UTILIZATION_COUNTERS_SPOOLED:` returns `_calls_symbol → False`.

This is the mirror of the trap `_calls_symbol`'s own docstring defends against.
It correctly refuses false ALL-CLEARS from bare imports and comments; it has
never been asked to avoid false REFUSALS from aliases and constants. Both
directions are needed once the predicate becomes load-bearing per box.

The honest fix for (a) is `ast` — resolve import aliases and match call nodes —
which the docstring already names as the right answer if a consumer ever defeats
the regex. One now has. For (b), a symbol entry needs to declare whether it is a
call or a name.

## What the narrowing GIVES UP (guard-4315 inventory)

guard-4315: when you narrow an over-broad gate predicate, the discarded breadth
was providing coverage nobody inventoried. Enumerated adversarially — what
NEWLY passes that byte-identity refused:

1. **A diverging consumer that still calls the seam but with changed arguments
   or changed semantics.** Byte-identity caught this; symbol routing does not.
   Genuine residual risk.
2. **A diverging consumer that calls the seam AND retains a legacy fallback
   branch.** Passes the routing check while still able to read the old location
   under some condition. Genuine residual risk.
3. **Any non-seam change in a consumer file.** This is the intended admission
   and the entire point — the 62 unrelated insertions in `retrieve.py`.

(1) and (2) are real and are accepted, bounded by: the tier is reached only on
divergence; it is reported under its own reason string; the fleet-level
`_symbol_report` veto against main is untouched; and the seam-ancestry check
still runs first, so the box provably contains the seam commit.

What is NOT given up, contrary to the intuitive worry: a consumer file MISSING
at the box's commit still fails, because `git show <commit>:<path>` returns
non-zero and the checker is fail-closed on unreadable.

## Explicitly rejected

- **Narrowing `--consumers` to force SAFE.** Laundering evidence past a gate.
- **Flipping the writer flag on a local probe.** Overriding a fail-closed gate
  on one's own probe (guard-3882's shape).
- **"Re-run the gate later."** Prescribed by units 13 and 14 of g-358-05 and
  retired in this goal's own note: re-running cannot succeed against 2.9
  consumer changes/day across five boxes. This is the advice that kept the item
  moving for three units without resolving it.
- **Lowering `ATTESTATION_MAX_AGE_DAYS` or otherwise tuning a threshold.** The
  predicate's SHAPE is wrong for this store; no constant fixes a shape.

## Implementation route

Deliberately NOT shipped with this decision — this is a fail-closed safety gate
guarding a shared own-cloud store, and its predicate change deserves its own
goal with its own tests rather than a mid-goal edit (the same reasoning that
kept the g-358-05 worker from redesigning it in-flight). Order matters:

1. Fix `_calls_symbol`'s two false-negatives (alias resolution via `ast`;
   call-vs-name declaration). **Prerequisite** — without it the new tier is
   unsatisfiable. This is future-proofing for `gate_firings`, NOT a live
   repair: measured 2026-08-21 (cc-02), all 3 of its consumers call
   `firings_paths(` directly — no alias, no bare-constant reference — so its
   live symbol report is correct today. An earlier draft of this line said
   `gate_firings` "gains correctness for free", which implied a latent
   mis-read that does not exist.
2. Widen `seam_symbol` → `seam_symbols` set, "calls ≥ 1"; migrate
   `gate_firings` as a one-element set; declare `utilization`'s 7.
3. Add the second tier to `_prove_commit`, scoped to diverging files, with its
   own reason string; leave `_local_report` and `_symbol_report` alone.
4. Re-measure the `utilization` gate verdict on the same 5-box fleet and
   confirm echo/foxtrot attest for a stated reason rather than by luck.

### Implementation record (g-358-28, 2026-08-22, zeta, cc-02)

Steps 1-3 landed as specified. Step 4 was measured, and its result needs the
caveat below rather than a bare "SAFE".

**The gate now returns SAFE for `utilization`** — all 5 boxes attested, and the
fleet-level `_symbol_report` veto (which could not run at all before, because
the store declared no symbols) passed with all 17 consumers routing. That
includes `_curation_predicate.py` matching via `utilization_of`, the aliased
deferred import that read as zero-token before g-358-27 — so the 17/17 is real
and not a widened-until-green artifact.

**But all five boxes proved via TIER 1, not tier 2.** Byte-identity happened to
hold fleet-wide at that moment, so the SAFE verdict does NOT attribute to the
new tier and must not be quoted as evidence that it works (guard-2781: a green
that your change did not cause is not verification of your change). Step 4's
"for a stated reason, not by luck" bar is therefore only half-met by the live
verdict: the symbol half is attributable, the tier half was not exercised.

The tier was instead verified by DISCRIMINATION against real repo data — the
honest form, since it does not depend on the fleet being divergent on the day
you look. Replaying `_prove_commit` over recent `origin/main` commits with and
without `seam_symbols`:

| proof commit | diverging consumer | tier-1 only (pre-change) | two-tier (post-change) |
|---|---|---|---|
| `2b15cfbe6` | `retrieve.py` | `proven=False consumers_diverge_from_main` | `proven=True seam_routed_despite_divergence` (`load_counters`) |
| `a2131bbb9` | `retrieve.py` | `proven=False consumers_diverge_from_main` | `proven=True seam_routed_despite_divergence` (`load_counters`) |

The diverging file is `retrieve.py` in both — the same file whose 62 unrelated
`_BLEND_STATS` insertions produced the 2026-08-20 blockage that motivated this
decision. That is the intended admission of § "What the narrowing GIVES UP"
item 3, observed doing exactly what it was designed to do.

**Re-measure before quoting any of this.** The 2.9 consumer-commits/day figure
and the fleet's byte-alignment are both DATED observations of a moving repo.

## Cross-references

- `g-358-23` — this decision; `g-358-05` unit 14 — the same finding scoped to
  the downstream leg only, which is why it never reached this code path
- `guard-4315` — narrowing a gate predicate discards uninventoried coverage
  (the § inventory above exists because of it)
- `guard-1790` — define a gate predicate on the DEFECT, not on the surface the
  defect appears on. Byte-identity is the surface; seam routing is the defect.
- `guard-487` — suppression gates fail CLOSED when input is unreadable
  (preserved: unreadable consumer at the box's commit still refuses)
- `guard-3882` — do not override a fail-closed gate on your own probe
- `guard-1685` — the referent trap the `_calls_symbol` comment-stripping already
  defends against; the alias/constant gap is its unguarded mirror
- `core/scripts/store-cutover-check.py` — `_prove_commit`, `_calls_symbol`,
  `_symbol_report`, `_local_report`. Symbol names, deliberately NOT line
  numbers: the very commit that created this doc also inserted 10 lines into
  that file, silently invalidating three of the four `~L` refs it had just
  written (506/522/548 → 516/532/558). A symbol name resolves with one
  `grep -n '^def _name'` and cannot rot. guard-4398, guard-2310, guard-4457.
