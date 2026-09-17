# Rationale: Closing a Reducer/Observer Carrier at Graceful Stop

Referenced from `.claude/skills/aspirations-graceful-stop/SKILL.md` D6.55.
Why a clean /stop needs its own carrier close, and why the value it writes is
deliberately not the one the sweep writes.

## Why anything has to run here at all

A reducer or observer Body has a `body-manifest.yaml` but `forked_wm_hash:
null`. `body-manifest.close_body_on_genuine` gates on a forked per-Body working
memory and returns `no-forked-wm` for exactly that shape, and `stop-hook.sh`'s
own close sits behind two gates that a reducer never passes:

- `:276` the runner-exclusion — a reducer IS the runner, so `RUNNER_FILE`
  exists and `HOOK_SID == RUNNER_SID`; the branch is never entered. The comment
  at `:542` states the same identity independently.
- `:299` the forked-WM test, backed by `close_body_on_genuine` above.

Both gates are correct as written and must NOT be widened — widening `:276`
would have the runner close itself mid-run. So nothing closed this population at
stop time, and the manifest plus the published carrier both stayed
`body_state: active` until the stale-binding sweep eventually arrived.

`worker_stall` reads the CARRIER, not the manifest (`sessions/` is walk-pruned
by the sync layer, so a peer structurally cannot read a manifest). For the whole
window between a clean stop and that sweep, the row graded `stalled_no_close`
and ALERTED. Every clean fleet stop minted a fresh cohort of false live-stalls.

Measured on cc-04 before the change (alpha, Linux 6.8.0-139-generic,
2026-09-15): the live reducer's manifest read `role: 'reducer'`,
`body_state: 'active'`, `forked_wm_hash: null`; its carrier read
`"body_state":"active"`.

## Why this is a missing CALL, not a missing writer

`body-manifest.close_body_late` already closes this population — its
no-forked-WM branch is the `marked-stale` verdict, and its docstring says so:
"no forked WM (a reducer or observer Body) ... closed-stale is the closed value
that says a sweep closed it." `set_state` already mirrors the manifest write to
the carrier, deliberately after it, so one call writes both files.

Its only caller was `cleanup-stale-bindings.sh`, a stale-binding sweep that by
construction runs LATE. Nothing invoked it at stop time. Do not write a second
writer, and do not model one on `close_body_on_genuine` — that reads the
manifest and gates on a forked WM, so a manifest-shaped implementation passes a
manifest fixture and transitions nothing the stall classifier reads.

## Why a distinct value, and why it touches SIX sites

> **CORRECTION (g-115-10026, 2026-09-15).** This section said FIVE and the table
> below lists five. That enumeration was WRONG, and a stated enumeration that is
> wrong is worse than none, because the next reader trusts it. There is a SIXTH:
> the daemon's cross-box carrier liveness probe in
> `mind_api/src/endpoints/aspirations_write.py`, which keeps its own
> hand-written closed-set and gates whether a claim is takeable. It did not
> receive `closed-graceful` when this shipped, so a gracefully-stopped Body read
> LIVE to the daemon for its whole freshness window and its claims stayed
> un-takeable — the exact class this change exists to remove.
>
> The miss was not carelessness about the table; it was SCOPE. The sweep that
> built it covered `core/scripts`, the tree being edited, and the partition has
> consumers outside it. guard-1127 says enumerate ALL consumers before widening
> a shared constant; the lesson this adds is that "all" means repo-wide.
>
> ⚠ This paragraph first prescribed *two-or-more closed-state literals on one
> line*, run from the repo root. **That grep finds 5 of the 7 sites and is the
> wrong tool** — re-measured 2026-09-15T17:45 (alpha, cc-04) by running it
> against this very repo. It requires each literal to be DOUBLE-QUOTED, which
> assumes every copy is a Python-style quoted list. Two are not: the
> `stop-hook.sh:488` shell alternation
> (`"^body_state: '?(closed-pending-merge|merged|closed-stale|closed-graceful)'?…"`,
> pipe-separated inside ONE quoted pattern, no member individually quoted) and
> the `deadman-directive.sh:201` prose enumeration ("one of closed-pending-merge
> / merged / closed-stale") inside an LLM-directed heredoc. Note the table below
> already lists the stop-hook grep as a site, so the doc was contradicting its
> own prescription. Correcting the DIRECTORY scope while keeping a
> SYNTACTIC-FORM assumption is half a fix, and it fails precisely on the copies
> least like the file you have open. **Use one distinctive member, unquoted:**
> `grep -rn 'closed-graceful' --include='*.py' --include='*.sh' . | grep -v '/\.git/'`
> — noisier by design, because a partition sweep must over-return: a missed site
> is a live defect, an extra line is two seconds of reading. (rb-11069 carries
> the generalised form.)
>
> Site six is now pinned against `CLOSED_STATES` by
> `core/scripts/tests/test_graceful_body_close.py::test_daemon_claim_probe_is_the_sixth_partition_site`,
> so it can no longer drift by hand.
>
> A seventh copy exists and is deliberately NOT patched: the worker deadman
> prompt in `core/scripts/deadman-directive.sh`. It is worker-only while
> `closed-graceful` is written on the reducer path, and its documented fail-safe
> is that an unheard-of state resolves toward RESUMING rather than stopping
> dead. Widening a fail-safe default with no reachable failure is how a safe
> default gets quietly inverted.

`close_body_late`'s default is `closed-stale`, byte-identical to
`orphan_carrier_repair.REPAIR_STATE`. Reusing it would hide a clean stop among
that module's own abandoned specimens. Hence `closed-graceful`, passed via
`--graceful`; the default call keeps its literal two-positional-arg form, so
both existing callers are unchanged by construction.

Writing a new value onto the carrier alone is NOT sufficient: `worker_stall`
grades benign only via `state in CLOSED_BODY_STATES`, so an unlisted value still
falls through to `stalled_no_close` while the diff looks right. The value must
join the partition:

| site | file | pinned by |
|---|---|---|
| `VALID_STATES` | `body-manifest.py` | `set_state` raises on an unlisted value |
| `CLOSED_STATES` | `body-manifest.py` (the SSOT) | — |
| `CLOSED_BODY_STATES` | `worker_stall.py` (consumed at the benign test) | `test_worker_stall.py::test_state_partition_matches_body_manifest` |
| `CLOSED_BODY_STATES` | `_frontier.py` | nothing — silent half |
| closed-state grep | `stop-hook.sh` | nothing — silent half |

The last two are unpinned by any test and must be changed in the same commit;
`test_graceful_body_close.py` now asserts both, including a regex read of the
stop-hook grep.

## Why it is addressed rather than a sweep

The close names only the sid that is stopping. `worker_stall.classify_body`
returns `V_ALIVE` on FRESHNESS before it reads `body_state` at all, so a live
Body is never in scope on freshness grounds either — but the addressing is the
stronger guarantee, and it is what makes "a live reducer's carrier is never
transitioned" true by construction rather than by measurement.

## Cross-references
- `guard-6558` — mutate `body_state` alone; never restamp `ts` (a restamp trades
  a false stall for a phantom live Body, which is strictly worse)
- `guard-1179` / `guard-3356` — fsync before rename; assert the read-back
- `core/scripts/orphan_carrier_repair.py` — the ungraceful counterpart, whose
  `>=3.0d` selection this change deliberately leaves untouched
- `.claude/skills/aspirations-graceful-stop/SKILL.md` D6.5 — the sibling
  best-effort write this one is modelled on, guard included
