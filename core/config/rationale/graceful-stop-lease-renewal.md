# Rationale: Graceful-Stop D3.5 Lease Renewal

Referenced from `.claude/skills/aspirations-graceful-stop/SKILL.md` D3.5. Why the
stop sequence renews the runner lease between D3 and D4, and why it uses
`runner-claim.sh heartbeat` rather than any of the three remedies the originating
goal proposed.

## Why a renewal is needed at all

D1 sets `agent-state=IDLE`, and `heartbeat-tick.sh` REFUSES to tick in IDLE
(`heartbeat-tick.sh:427-431` — the alpha-2026-05-13 desync gate, which is correct
and must keep its meaning). So from D1 onward nothing renews the DDB runner lease,
and a D4 that runs past `runner_heartbeat`'s stale threshold (3900 s) loses it.
Every fenced `agents/<agent>/**` write after that point fails `no_claim`.

MEASURED 2026-09-15 (bravo, hostname cc-05, uname -r 6.8.0-139-generic): last good
tick 16:59:38, stop started 17:07:35, lease lapsed ~18:04:38. Consolidate Step 2.6
`experience-archive.sh` returned `{"error":"no_claim"}` and the Step 9 handoff build
returned `write_failed`, so the stop produced NO handoff and the next `/start`
booted without it. The FW-11 resume path (g-317-09) exists precisely for stops
interrupted mid-D4, so this defect hits hardest exactly where recovery is needed.

## Why `runner-claim.sh heartbeat` and not the three proposed remedies

g-115-10040's description offered (a) a stop-scoped exemption inside
`heartbeat-tick.sh`, (b) reordering the fence-sensitive writes or renewing between
D-phases, and (c) teaching the `no_claim` fence to accept a stale SELF-holder.
(a) and (c) are unnecessary, and both trade a narrow, measured stop-time gap for a
fleet-wide ownership hole (guard-2851). D3.5 is (b), using a primitive that already
exists.

`runner-claim.sh heartbeat` is NOT `heartbeat-tick.sh --bypass-state` (which is
reserved for `/start`). It is not agent-state-gated, and it is TOKEN-CONDITIONAL,
so it can only refresh a claim this box already holds and can never steal a peer's.

Prior art: **rb-10942** (foxtrot, 2026-09-14) already recorded the lapsed-own-lease
mechanism and this remedy. bravo re-validated it live on the 2026-09-15 incident:
`status` STALE at 14,048 s → `heartbeat` ok (`beat=True`) → LIVE at 0 s, and the
`handoff-yaml-build.sh` write that had just raised `NoClaimError` then succeeded
(rc=0, 17 fields, `dropped_keys []`), from a resumed session whose SID differed from
`running-session-id` — so the path does not depend on the SID.

## Why the call stands alone and fails open

It is its OWN Bash call, with no status read or precondition chained to it
(guard-6424: never acquire a lease in the same invocation as the precondition that
gates it; guard-409: same shape for state writes). A nonzero rc must NOT abort the
stop — stopping is more important than the lease (guard-1562) — so D3.5 warns and
continues.

`--token` is deliberately not passed: `runner-claim.sh`'s usage header documents it
as defaulting to the framework-owned UUID4 at `agents/<agent>/session/runner-token`,
which avoids a `$(cat ...)` substitution inside SKILL.md pseudocode (guard-359 —
the flags here were read off that header verbatim before being named).

## Residual

D3.5 renews ONCE. A D4 that itself runs longer than the 3900 s window lapses again;
the remedy from inside consolidate is this same one-line renewal. Do NOT close that
by weakening the IDLE gate or the `no_claim` fence — see above.

Outcome 1 of g-115-10040 (a real stop whose D4 exceeds the threshold writes
`handoff.yaml`, confirmed by independent read-back) and outcome 3 (a regression test
pinning stop-time behaviour while the IDLE desync refusal still fires for a non-stop
IDLE caller) remain OPEN; the goal was released as a partial unit, not closed.

## Cross-references

- rb-10942 — the lapsed-own-lease mechanism and the validated renewal
- guard-2851 — publishability by ownership; why (a)/(c) are the wrong trade
- guard-6424 / guard-409 — lease and state writes stand alone
- guard-1562 — stopping a healthy loop on a plumbing fault is worse than the disease
- guard-359 — verify script flags verbatim before naming them in pseudocode
- `.claude/skills/aspirations-graceful-stop/SKILL.md` D3.5 — the consumer
