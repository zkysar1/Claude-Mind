# Health Ledger — Self-Health Regression Detection & Tiered Revert

The per-agent self-health subsystem. It (1) durably records a per-iteration
time-series of "how the self is doing" signals, (2) detects when those signals
degrade beyond a triple-condition gate, (3) attributes the degradation to the
recent file changes that most plausibly caused it, and (4) reverts the culprit
under a constitutional-ring-tiered authority boundary.

It is the closed-loop complement to `productivity-stop-gate.sh`: the
productivity gate is the session-scoped circuit breaker (stops the agent when
productivity collapses); the health ledger is the **cross-session,
change-attributing, self-correcting** layer that catches a regression and
points at (or undoes) its cause *before* productivity falls to stop-worthy
levels.

Design provenance: the dialectical design workflow `wf_b82a26e9-958`
(2026-06-03) — winner "Bifurcated Health Ledger with Signal-First
Observability", a sublation of the per-agent managed-table thesis and the
zero-new-infra antithesis. Net: per-agent local JSONL, signals reused from
existing telemetry, change-history reused from git + S3 versioning, **no new
managed-database tables**, ~$0.05–0.15/month incremental.

---

## 1. Scope and non-goals

- **In scope**: detecting and (tier-permitting) reverting framework/state
  changes that demonstrably degrade an agent's measured self-health.
- **Not** a replacement for the productivity gate, the evolution engine, or
  `audit-baselines.yaml` — it reuses all three.
- **Not** a general undo system — reverts are gated by constitutional ring,
  confidence score, and a 30-day calibration period.
- Single-machine at launch (see §9 attribution limitation).

---

## 2. The ledger file

**Path**: `agents/<agent>/health/<YYYY-MM-DD>.jsonl` (daily-rotated).

Daily rotation caps S3 noncurrent-version growth under own-cloud: only the
current day's file is mutated (appended per iteration); prior days' files are
immutable and their noncurrent versions age out under the bucket's 90-day
lifecycle. The ledger syncs to S3 via the existing `owncloud_sync.py` mirror
sweep (it is NOT machine-local-excluded) so health history survives a
machine move. It is never `.history`-snapshotted: the per-iteration append is
a direct local `open(...,'a')`, not a write through `_fileops`, so the
copy-on-write snapshot path never fires for it (no `_SNAPSHOT_BLACKLIST`
entry is needed — adding one would be speculative).

**One record per iteration**, appended by the `health-snapshot` phase of
`iteration-close.sh` (after `productivity-check`). Schema:

```jsonl
{
  "ts": "2026-06-03T14:22:31",       // local system time (never UTC)
  "agent": "alpha",
  "session_id": "abc123",
  "iteration": 47,                    // session goals_completed at snapshot time
  "signals": {
    "composite_productivity": 0.72,   // productivity-snapshots `score`
    "encoding_ratio": 0.61,           // productivity-snapshots breakdown.encoding_ratio
    "deep_ratio": 0.70,               // productivity-snapshots breakdown.deep_ratio
    "tree_writes": 4                  // productivity-snapshots breakdown.counts.tree_writes
  },
  "composite": 0.68,                  // weighted health composite (see §4)
  "composite_trend": -0.03,           // LSQ slope over last 10 non-warmup composites (§5)
  "baseline": 0.71,                   // audit-baselines health_composite ratchet value (§6)
  "below_baseline": false,            // composite < baseline * (1 - tolerance)
  "warmup": false                     // excluded from trend/detection (§7)
}
```

---

## 3. Signal sources (VERIFIED 2026-06-03)

All signals are read from the agent's **latest record in
`world/productivity-snapshots.jsonl`** — they are already computed once per
iteration by `productivity-stop-gate.sh` (the G4 snapshot block). The health
ledger does NOT recompute them; it reuses them and adds cross-session
intelligence (trend, baseline, attribution). This is the lowest-cost source
and avoids a second pass over working memory.

| Health signal | productivity-snapshots field | Direction | Weight |
|---|---|---|---|
| `composite_productivity` | `score` | higher = better | 0.40 |
| `encoding_ratio` | `breakdown.encoding_ratio` | higher = better | 0.25 |
| `deep_ratio` | `breakdown.deep_ratio` | higher = better | 0.20 |
| `tree_writes` | `breakdown.counts.tree_writes` (÷ cap 8) | higher = better | 0.15 |

Notes:
- `composite_productivity` is itself a composite of encoding/deep/routine; the
  health composite re-weights it as the dominant term plus the two ratios and
  a normalized tree-write rate. This is intentional redundancy — the health
  composite is a *smoothed, trended* view, not a duplicate gate.
- **Arming**: `productivity-stop-gate.sh` writes a snapshot only after
  `goals_completed >= productivity_gate.min_iterations` (default 10). So no
  health record exists for the first ~10 goals of a session — correct, the
  early-session ramp is noise. If no snapshot exists for this agent yet, the
  `health-snapshot` phase is a silent no-op (fail-open).
- An optional 5th signal `imp_k` (from `meta/improvement-velocity.yaml`) joins
  only when that file's mtime is < 7 days (liveness gate); weights renormalize.
  Deferred — see open decision OD-1.

### Normalization

Each raw signal → [0.0, 1.0]:

| Signal | Normalization |
|---|---|
| composite_productivity | identity (already 0–1) |
| encoding_ratio | identity |
| deep_ratio | identity |
| tree_writes | `min(raw / 8, 1.0)` |

---

## 4. Composite & weight renormalization

```
composite = Σ(weight_i × normalized_i for available signals) / Σ(weight_i for available signals)
```

Renormalization handles signal dropout: if a snapshot field is missing, that
signal drops and the remaining weights renormalize proportionally. Minimum 2
signals; below that the record is written with `composite: null` and excluded
from trend/detection.

---

## 5. Trend (pre-computed at append time)

`composite_trend` = slope of a least-squares linear regression over the last
10 non-null, non-warmup `composite` values (this agent, walking day-files
newest-first across the rotation boundary). Stored in the record so the
detection sweep never recomputes history. A trend `< -0.02` per iteration is
the "slope" leg of the detection gate (§8, Phase 2).

---

## 6. Baseline (reuses the audit-baselines ratchet)

`baseline` is the `health_composite` value in `meta/audit-baselines.yaml`,
maintained by the existing ratchet machinery (seeded → stable → ratcheted →
regressed verdicts, monotonic-shrink, bounded history). No new ratchet
mechanism is introduced. `below_baseline = composite < baseline * (1 -
tolerance)` (default tolerance 0.10). Baseline seeding happens after the
calibration period (§10) from the first qualifying records.

---

## 7. Warmup exclusion

Records flagged `warmup: true` are written (for completeness) but excluded
from composite-trend and detection. Warmup = the first
`health_regression.warmup_records` (default 2) non-null records of a session —
prevents session-start ramp from registering as a degradation slope.

---

## 8. Detection gate (Phase 2 — detect-and-report only)

Runs as a budget-metered, medium-tier deferrable sweep in
`aspirations-precheck` (Phase 0.5h), every `health_regression.interval`
(default 10) goals. Fires only when ALL hold on the latest non-warmup record:

1. **Slope**: `composite_trend < -0.02`
2. **Floor**: `composite < 0.40` (no triggering on healthy agents' minor dips)
3. **Below baseline**: `below_baseline == true`

PLUS a **consecutive-below-baseline counter ≥ 3** (one-off bad iterations do
not trip). On trip it files an `Investigate:` goal with structured context
(the regression window, the composite/baseline values) and — before filing —
checks `meta/evolution-log.jsonl` for a meta-strategy change inside the window
(so the revert pipeline never fights the evolution engine). **No revert is
executed in Phase 2** — the system reports candidates only.

---

## 9. Change attribution (Phase 2)

When the gate trips, `health-attribution.py` identifies candidate causal files
in the regression window:

1. **Primary source: `git log`** (`--after`/`--before` the window) — works
   cross-machine because git history is shared. This is the authoritative
   change source.
2. **Supplementary (single-machine only): `world/changelog.jsonl`** — richer
   per-write detail BUT machine-local (excluded from S3 sync), so it is a
   best-effort local enrichment, never the sole source.
3. Score each candidate:
   `attribution_score = temporal_proximity × correlation_weight × recency_decay`
   where `correlation_weight` comes from
   `core/config/aspirations.yaml` → `health_regression.signal_file_correlation`
   (1.0 direct / 0.5 indirect / 0.3 unknown default). Folded into the config
   block rather than a standalone `meta/` file: single source of truth with the
   rest of the subsystem's params, and a new top-level `meta/` file would trip
   the L1 new-top-level-entry gate. Still agent-evolvable (core/config is
   agent-editable); seed lean and let it accrete from confirmed attributions.
4. Filter out candidates in the per-agent dead-end registry
   (`agents/<agent>/health-dead-ends.yaml`) for the degraded signal.

**Single-machine attribution limitation (launch constraint)**: cross-machine,
only git-tracked files can be attributed (changelog.jsonl does not sync).
World/meta JSONL/YAML state changes that are not git commits are attributable
only on the machine that wrote them. Documented; revisited when machine-2
lands.

---

## 10. Calibration gate (detect-only until satisfied)

The full pipeline ships **dormant**. Revert authority activates only after
BOTH conditions hold (AND, not OR):

- ≥ `health_regression.calibration.min_days` days (default 30) of records, AND
- ≥ `health_regression.calibration.min_records` records (default 50).

Until then the system runs in `detect-only` mode: it collects records and (in
Phase 2) may file Investigate goals, but executes no reverts. This seeds a
real baseline before any automated action.

**Day-counting reads the FULL ledger, not a recent window.** The gate's day
count comes from `_health_ledger.full_calibration_progress(health_dir)`, which
scans every day-file and counts distinct `ts[:10]` dates. It MUST NOT use
`calibration_progress` over a bounded `recent_records(d, 120)` window: an agent
that records many iterations per calendar day fills 120 records in far fewer
than 30 days, so the windowed form would under-count days and the 30-day gate
would never trip. The gate is monotonic on an append-only ledger — once
satisfied it never reverses — so the full scan runs each sweep only until the
first crossing, then short-circuits (see the marker below).

**Calibration-complete trigger (fires exactly once).** `health-regression-check.py`
evaluates the gate in EVERY mode (including `collect-only`, before the mode
early-return) via `_calibration_status()`. On the first crossing it writes a
per-agent marker `agents/<agent>/health/.calibrated` and sets
`calibration_just_completed: true` in the verdict (with `calibration_dedup_key:
"health-calibration-complete"`). `aspirations-precheck` Phase 0.5h reads that
flag and files a single `participants:[agent, user]` goal —
deduped (incl. `completed` status) on the dedup key so the FIRST agent to
calibrate files one team-wide goal and later agents skip. The marker makes the
edge idempotent per-agent; the dedup query makes it idempotent team-wide. The
goal distinguishes the two rollout steps: `collect-only → detect-and-report` is
low-risk (reports only) and agent-judgable, while `detect-and-report → full`
GRANTS auto-revert authority and is a deliberate, **user-paced** decision — the
goal asks the user to make that advance.

---

## 11. Tiered revert by constitutional ring (Phase 3)

Revert authority is gated by the constitutional ring of the candidate file
(`core/config/conventions/constitutional-rings.md`). The single canonical
classifier is `_classify_ring()` in `health-attribution.py`.

| Ring | Files (examples) | Action | Autonomy |
|---|---|---|---|
| **0** | `.claude/settings.local.json`, `settings-structural-validator.{py,sh}` | **NEVER touch** — skip entirely; an attribution here is wrong, log + discard | none |
| **1** | `world/program.md`, `agents/*/self.md`, `core/config/conventions/*`, `core/config/modes/*`, `.claude/rules/*` | **Notify user** (pre-notification) + file `participants:[user]` Unblock with evidence + proposed revert | user approves |
| **1.5 / critical-infra** | `core/scripts/*.py`, `mind_api/src/*.py`, `.claude/skills/*` | Treated as **Ring 2** (never auto) — critical framework code / skill pseudocode must not auto-revert | agent-gated |
| **2** | `core/config/aspirations.yaml`, `core/config/evolution-triggers.yaml`, `world/guardrails.jsonl`, `world/reasoning-bank.jsonl`, `world/conventions/*` | **Agent-gated** Unblock (`participants:[agent]`): revert → wait 5 iterations → keep if composite improves ≥ 0.05 else undo + dead-end | agent + post-verify |
| **3** | `meta/*.yaml` strategies, `agents/*/developmental-stage.yaml`, operational params | **Auto-revert** iff `attribution_score ≥ 0.7` (else demote to Ring 2 treatment) | autonomous |

Reverts are **file-granular** (`git show <bad-commit>~1:<path>` to restore one
file's pre-regression content, NOT `git revert` of a whole commit), tagged with a
`Health-Revert` git trailer (filtered out of future attribution scoring to
prevent feedback loops), and gated by a **bidirectional dead-end registry** with
a chain-depth cap of 1 (prevents revert↔undo oscillation).

**Executor: `core/scripts/health-revert.{py,sh}`** (Phase 3). Two subcommands,
both wired into `aspirations-precheck` Phase 0.5h:

- `route --verdict <json>` — takes a detection verdict, runs `route_candidate`
  on its top attribution candidate, and acts by ring: Ring 0 → skip; Ring 1 →
  emit a `participants:[user]` Unblock spec + notify; Ring 1.5/2/unknown (or a
  Ring-3 candidate demoted below `revert.confidence_gate`) → emit a
  `participants:[agent]` Unblock spec; Ring 3 with `authority == auto` →
  `revert_file()` immediately and record a PENDING entry. `route_candidate`
  re-checks `mode == full AND calibrated` itself, so a non-eligible verdict is a
  safe no-op regardless of caller — this is the master safety gate.
- `verify` — for each pending revert (`agents/<agent>/health-pending-reverts.yaml`)
  whose `verification_iterations` window has elapsed: KEEP if the composite
  improved ≥ `revert.improve_delta`, else UNDO (re-apply the bad-commit content)
  and write the `(file, signal)` pair to the dead-end registry
  (`agents/<agent>/health-dead-ends.yaml`). The undo is itself NOT re-tracked
  (chain-depth cap 1 → no revert↔undo ping-pong).

A file CREATED in the bad commit reverts to deletion (its parent had no such
file); a bad commit that is the repo ROOT aborts rather than wrong-deleting (its
parent ref does not resolve). The commit uses an explicit `-- <path>` pathspec so
it captures ONLY the reverted file — never other changes the concurrent
autonomous loop has staged in the shared index. Git failures are fail-loud AND
leave no residue: on commit failure (e.g. `index.lock` contention with the loop)
the file is restored to HEAD, so the loop can never later land the revert content
WITHOUT the `Health-Revert` trailer (which would defeat the feedback-loop filter).
The two per-agent registries are written on first use (Phase 3) — they trip no
L1 new-top-level gate because the script writes them via `open()`, not the
Write/Edit tools the L1 hook governs.

---

## 12. Economic profile

- **AWS incremental: ~$0.05–0.15/month.** Ledger ≈ 200 bytes/record × ~50
  iter/day × 6 agents ≈ 1.8 MB/month of new content, synced via existing
  `owncloud_sync.py`. Daily rotation caps per-file S3 noncurrent-version churn.
- No new managed-database tables, no serverless compute functions, no managed
  monitoring service, no new IAM beyond the existing object-store/database grants.
- Compute: signal collection piggybacks on `iteration-close.sh` (~200 ms,
  fire-and-forget, fail-open). Detection is a budget-metered medium-tier
  sweep. Attribution runs only on a (rare) regression trip.

---

## 13. Components

| Component | Path | Phase |
|---|---|---|
| Conventions (this doc) | `core/config/conventions/health-ledger.md` | 1 |
| Config | `core/config/aspirations.yaml` → `health_regression:` | 1 |
| Append script | `core/scripts/health-ledger-append.py` (direct in-loop python; no `.sh` wrapper, matching the `agent-watchdog.py` / `stale-sentinel-canary.py` pattern) | 1 |
| iteration-close wiring | `core/scripts/iteration-close.sh` (`do_productivity_check` tail — runs after the productivity snapshot is written, fail-open) | 1 |
| Daemon endpoints (read paths only) | `mind_api/src/endpoints/` — reconsidered in Phase 2 for detection/attribution/query reads; collection (append) is a direct local write, no endpoint needed | 2 |
| Correlation seed | `core/config/aspirations.yaml` → `health_regression.signal_file_correlation` (folded into config, not a standalone `meta/` file — single SoT + avoids the L1 new-top-level gate) | 1 |
| Detection sweep | `core/scripts/health-regression-check.{py,sh}` | 2 |
| Attribution engine | `core/scripts/health-attribution.{py,sh}` | 2 |
| Baseline ratchet | `meta/audit-baselines.yaml` `health_composite` + `health-composite-ratchet.{py,sh}` | 2 |
| Dead-end registry | `agents/<agent>/health-dead-ends.yaml` (written by the Phase-3 executor on undo) | 2 |
| Revert executor | `core/scripts/health-revert.{py,sh}` (route + verify subcommands; file-granular git restore, ring routing, pending tracker, keep-or-undo) | 3 |
| Pending-reverts tracker | `agents/<agent>/health-pending-reverts.yaml` (written by `route` auto-revert; drained by `verify`) | 3 |
| Query utility | `core/scripts/health-ledger-query.{py,sh}` | 2 |
| Tests | `core/scripts/tests/test_health_*.py` | 1–3 |

---

## 14. Open decisions (from the design workflow)

- **OD-1** imp@k liveness — verify `meta/improvement-velocity.yaml` is
  populated before adding the 5th signal.
- **OD-3** revert verification window (5 iterations) may need tuning post-Phase-3.
- **OD-4** correlation-map re-seed cadence (TBD; Phase 3 follow-up).
- **OD-5** `.history` skip under own-cloud (D5 in `lodestar-rollout-status.md`)
  — attribution's git-diff fallback is compatible either way.
- **OD-7** cross-agent regression (alpha detects bravo's file) — courtesy board
  post at launch; mirror goal deferred.

## 15. Cross-references

- `core/config/conventions/audit-baselines.md` — the ratchet this reuses
- `core/config/conventions/constitutional-rings.md` — the revert authority model
- `core/scripts/productivity-stop-gate.sh` — the signal source + the sibling gate
- `mind_api/docs/lodestar-rollout-status.md` — own-cloud context (D5, S3 versioning)
- `.claude/rules/verify-before-assuming.md` — attribution must not over-claim causality
