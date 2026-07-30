# Check Team-State Before Concluding Partner Silent

## Principle

Before any code path or narrative conclusion declares a partner agent
silent, absent, crashed, or unresponsive — read
`world/team-state.yaml` `agent_status.<partner>.last_active` first.
"I haven't heard from bravo this session" is not evidence of silence
when the team-state file already records bravo's last activity 30
minutes ago. Trust the signal you already have.

## The Threshold

Default: **6 hours**. If `agent_status.<partner>.last_active` is within
6h of now, the partner is NOT silent — they are working in another
session, between iterations, or finishing a long goal. Use a longer
threshold only when the partner's normal cadence is documented as
slower (e.g., a daily-cadence reviewer agent).

## Rules

1. **Probe before concluding**: Run the canonical probe before any
   sentence (in narration, in a board post, in a goal description,
   in pseudocode) that asserts the partner is silent / absent /
   crashed / unresponsive / inactive / stalled.

   **Preferred (multi-signal, cross-box-safe) — `liveness-check.sh`** (g-115-2149):
   ```bash
   bash core/scripts/liveness-check.sh --agent <partner> --json
   ```
   Returns `alive` | `dormant` | `unknown` | `retired`. It reads `last_active` (daemon-routed)
   AND, only when that looks stale, cross-checks an INHERENTLY-FRESH signal — the
   partner's team-state shard's last-write time read from the AUTHORITATIVE store
   rather than the local mirror. This defeats the read-side lie where the daemon
   composes team-state from a STALE LOCAL shard mirror (`_team_state.load_rows`
   reads shard files locally with no re-fetch from the authoritative store), so a
   busy partner whose fresh shard already reached the authoritative store reads as
   days-stale on every OTHER box (observed 2026-07-14: a partner's live
   `last_active` age was 6.5 days on this box while its shard had been pushed 2
   minutes earlier → verdict correctly `alive`). Conclude silence ONLY on
   `dormant`; `unknown` means the fresh signal was unreadable — do NOT conclude
   dormant on `unknown`.

   `retired` (g-115-3702) means the agent is DECOMMISSIONED, not merely quiet —
   its shard carries a retirement tombstone. Do not route work to it, do not
   wait on it, and do not file a "partner silent" finding about it; it is not
   coming back unless someone revives it (a heartbeat newer than `retired_at`
   un-retires the row automatically). This verdict exists because retirement is
   a tombstone rather than a delete: the shard SURVIVES and keeps getting
   written, so the freshness signals alone reported a decommissioned agent as
   `alive` indefinitely — and the retirement write itself refreshed that signal,
   making a just-retired agent look MORE alive. Measured on the `meta-tiebreaker`
   phantom: `retired_at 17:08:19`, authoritative-store push `17:08:20`, verdict
   `alive` 2.8h later. Note `retired` is deliberately NOT `dormant`: consumers
   that act on dormancy (goal-selector's `_liveness_confirms_dormant`) see False
   and keep goals routed, which is the fail-safe direction.

   **Underlying raw signal** (what the helper wraps; use directly only when you
   just need the pushed snapshot value, not a liveness verdict):
   ```bash
   bash core/scripts/team-state-read.sh --field agent_status.<partner>.last_active --json
   ```
2. **Compare against threshold**: If the returned timestamp is within
   6h of now, do NOT conclude silence. The signal is positive
   evidence of recent activity.
3. **Missing field is silence**: If the field is missing or null,
   AND no other liveness signal exists, then silence is a valid
   conclusion. The pre-silence check passes negatively.
4. **Probe applies to all consumers**: Backoff escalation, take-back
   triggers, "partner is dead, change strategy" branches, and casual
   narrative diagnostics all route through the same probe. There is
   no exemption for "I'm just thinking out loud" — the probe is one
   shell call.
5. **THE SIGNAL IS ASYMMETRIC — this is the load-bearing rule.**
   A **FRESH** `last_active` (within threshold) IS positive evidence of
   life. Trust it; stop there; do not conclude silence. That direction is
   sound and is the fast path.
   A **STALE** `last_active` is **NOT** evidence of death. It is *ambiguous*,
   because a stale value has two causes that look identical from outside:
   the partner is idle, **or the partner's heartbeat writer is broken while
   the partner keeps working.** The signal cannot distinguish them, and it
   has been observed failing this way in production (see 2026-07-14 below).
   Never conclude silence from a stale `last_active` ALONE.
6. **A stale `last_active` obliges the 2-branch decision tree.** Never
   conclude silence without walking it. The two branches were found a day
   apart and are *complementary* — one is about your box, one about theirs.

   **Branch 1 — is EVERY peer stale simultaneously?** (g-115-2119, rb-3150)
   Then `last_active` is telling you about **YOUR** box, not the fleet. The
   field reflects the *observer's last successful pull* of the peer's shard,
   not the peer's health; a local pull-merge wedge freezes it for all peers
   uniformly. Suspect your local pull path first. Cross-check the
   coordination board (partition-surviving) or a direct authoritative-store
   HEAD. **Stale-for-all is evidence about you.**

   **Branch 2 — is only SOME peer stale** (others fresh, so your pull path
   is demonstrably fine)? (g-115-2181) Then the stale peer may be **alive
   with a broken heartbeat writer.** Corroborate with a signal that has an
   *independent writer*, read from the **store of record — never the local
   cache** (a never-pulled local file is absent for cache reasons and proves
   nothing; guard-980, and the `owncloud-local-cache-staleness` tree node):
   ```python
   # canonical: S3/daemon HEAD, never Path.exists() on the local mirror
   b.s3.head_object(Bucket=b.bucket,
                    Key=b._s3_key(Path("agents/<partner>/session/execution-diary.jsonl")))
   ```
   - diary **fresh** + `last_active` stale → partner is **ALIVE, heartbeat
     broken.** Do NOT reassign, take back, or escalate. File the heartbeat bug.
   - diary **stale** + `last_active` stale → two independent signals agree.
     Silence is now a **verified** conclusion.

   `working-memory.yaml` in S3 is an equally valid second signal. Which
   branch applies is decided by *whether any peer is fresh* — on 2026-07-14
   foxtrot's 4.7h freshness is precisely what proved Branch 1 did not apply
   and sent the diagnosis to Branch 2.

   `liveness-check.sh --agent <partner> --json` (Rule 1) automates the
   team-state half of this corroboration per-peer (g-115-2149) — it compares
   the shard's authoritative-store push time, never the local mirror. Prefer
   its verdict over hand-reading `last_active` whenever a silence conclusion
   is at stake; the hand-rolled `head_object` shape above remains the way to
   read signals the helper does not wrap (execution-diary, working-memory).

## Anti-patterns

- "<partner> hasn't posted in a while, must be silent" — without reading
  `agent_status.<partner>.last_active`
- "<partner> is unresponsive, taking back the goal" — when the team-state
  probe would have shown the partner mid-execution at phase 4
- Backoff escalation that ramps "because the partner is silent"
  without checking the team-state field that would falsify the
  premise
- Treating session-start as the last evidence of partner activity —
  the team-state file outlives sessions
- **Concluding a partner is dead from a stale `last_active` alone** —
  a broken heartbeat writer and an idle agent are indistinguishable at
  that field. Corroborate (rule 6). This is the failure the 2026-07-14
  incident below produced *in the rule itself*.
- **Probing the partner's execution-diary on the LOCAL filesystem** —
  under own-cloud the local tree is a read-through cache; a file nobody
  has read on this box never materializes locally, so `Path.exists()`
  returns False for a file that is alive and well in S3. Read the store
  of record (guard-980, and the `owncloud-local-cache-staleness` tree node).

## Status

Audit on 2026-04-19 found NO committed "partner silent" branches in
any skill at the time of writing — the incident that produced this
rule occurred in narrative reasoning, not in pseudocode. This rule
is preventive: it gates BOTH future pseudocode authors AND in-session
LLM narration from concluding silence without the probe. The
guardrail (`guard-321`) fires whenever the agent's narrative or
execution path approaches the conclusion.

### 2026-07-14 — the rule's own signal was found broken (g-115-2181)

The canonical signal this rule mandates trusting was **wrong by 59 hours
on a live agent**, and following the rule as written would have produced
exactly the error it exists to prevent.

Measured from cc-02 (canonical S3 reads, not the local cache):

| agent | `last_active` (team-state) | execution-diary in S3 | truth |
|---|---|---|---|
| alpha | **59.0h stale** | **0.4h fresh** | **ALIVE — heartbeat broken** |
| bravo | **65.9h stale** | **2.2h fresh** | **ALIVE — heartbeat broken** |
| foxtrot | 4.7h | 1.8h | alive |
| echo | 30.5h | 31.0h | genuinely quiet (signals agree) |
| zeta | 0.0h | 0.0h | alive (writer working) |

alpha was independently observed alive three times that day (completed
g-001-10 at 10:45, posted to the board at 15:37, filed g-115-2176 at
15:42) while its shard read 59h. The shards are NOT machine-local-excluded
(`OwnCloudBackend._machine_local` → False), and alpha's `working-memory.yaml`
reached S3 0.4h earlier — so cc-04→S3 sync works and the **team-state write
specifically is not firing**. Root cause (writer vs push) is tracked in
g-115-2181; rules 5–6 above are the safety fix, which does not wait on it.

The near-miss: a HALT on a harmful fleet-wide write was being routed to
alpha at the time. Had the rule been followed literally, alpha would have
been written off as 59h dead and never told.

## Cross-references

- `world/guardrails.jsonl` → `guard-321` (the active guardrail
  enforcing this rule at execution time)
- `world/reasoning-bank.jsonl` → `rb-350` (the 2026-04-19 incident
  trace with rb-245 + rb-258 "trust the signal you already have"
  lineage)
- `core/config/conventions/coordination.md` → "Team State Protocol"
  section, `agent_status.<agent>.last_active` field documentation
- `core/scripts/team-state-read.sh` → the raw pushed-snapshot probe script
- `core/scripts/liveness-check.sh` + `core/scripts/liveness_check.py` →
  the multi-signal liveness helper (g-115-2149); unit tests in
  `core/scripts/tests/test_liveness_check.py`
- `core/scripts/goal-selector.py` → `_get_idle_agents` /
  `_liveness_confirms_dormant` (g-115-2315): the intended_agent
  idle-reallocation is a CODE consumer of the silence conclusion, wired
  through the helper — idle only on a dormant verdict; alive/unknown keep
  goals routed. Before g-115-2315 it used raw `last_active` age and leaked
  an ACTIVE agent's routed goal cross-agent (foxtrot, 2026-07-16). Tests:
  `test_goal_selector_idle_reallocation.py` (liveness cross-check block).
  When adding any new code path that ACTS on agent staleness (backoff,
  take-back, reallocation), wire it the same way.
- `world/reasoning-bank.jsonl` → `rb-3150` (peer-shard merge-handler
  gap) — the pull-wedge mechanism behind rule 6 Branch 1; fix lane
  g-115-2133 / g-115-2001; hypothesis trace g-115-2119
