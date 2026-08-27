# Pull-Promotion Protocol (staging → downstream Minds)

How a downstream Mind ADOPTS a framework release from Claude-Mind staging.
The mirror image of `promotion-runbook.md`, which is the PUSH side.

`promotion-cycle.md` owns the chain (dev → staging → prod) and is the single
source for it. Dev→staging stays a PUSH (the dev operator plants).
Staging→downstream is a **PULL**: the adopting Mind decides *when*, inside its
own idle window, and no upstream actor touches its disk.

## Why a separate file (decision recorded per the goal's mandate)

Folded into `promotion-runbook.md` this would be one document two different
roles read for opposite halves — the runbook's actor is the dev operator
planting outward; this file's actor is the adopting Mind pulling inward, with
different triggers, different failure modes, and a different blast radius.
A distinct name also keeps it retrievable as `load-conventions.sh
pull-promotion`, which a merged file would bury. The runbook is unchanged and
cross-references this file.

## Existing-machinery inventory (EXTEND, NOT REBUILD)

Measured 2026-08-24 on the dev Mind's `main`. Reuse-or-extend decision per
component:

| Component | What it actually does | Decision |
|---|---|---|
| `core/scripts/release.sh` | "the SOLE creator of v* git tags (M2). It NEVER pushes" — bumps the version SSOT, appends a `RELEASES.json` entry, commits, tags | **REUSE unchanged.** It is the tag scheme (C1). The pull side never cuts tags. |
| `core/scripts/_release_lib.py` | library behind `release.sh` | **REUSE unchanged.** Push-side only. |
| `core/scripts/check-releases-current.sh` | seed-preflight check #7: newest `RELEASES.json` entry == version SSOT (`mind_api/src/__init__.py __version__`) | **REUSE at the SOURCE.** Its own header records that `RELEASES.json` is NOT seeded downstream, so it cannot run at the adopter. |
| `core/scripts/check-tag-in-releases.py` | M2 pre-push gate kernel behind `core/githooks/pre-push`; takes BARE semver (hook strips the `v`); FAIL-CLOSED | **REUSE unchanged.** Push-side gate. |
| `core/scripts/promotion-preflight.sh` / `.py` | READ-ONLY cross-repo drift gate; owns the zone map and `content_divergence` | **EXTEND (C2).** Already the reconcile gate; the pull side calls it with source/target swapped. |
| `core/scripts/promotion-plan-triage.sh` | READ-ONLY classifier for a blocked `--plan` verdict; emits the evidence ledger | **REUSE unchanged**, and make it the input to the decision registry (§a). |
| `core/scripts/promote-to-upstream.sh` | the push-side plant | **NOT USED by the pull side.** Named here so a reader does not reach for it. |
| `core/scripts/aspirations-release.sh` | **NOT release machinery.** It is the daemon wrapper that releases a goal CLAIM | **EXCLUDE — name collision.** The filing description grouped it with the tag tools; "release" means two different things. Do not wire it into any promotion path. |

Verified state at time of writing: newest tag **v2.12.3**, version SSOT
**2.12.3**, newest `RELEASES.json` entry **2.12.3 (2026-08-23)** — all three
agree. Note `git tag --list` sorts LEXICALLY, so `v2.9.4` tails above `v2.12.3`;
always `--sort=v:refname`.

## C1 — Tag scheme: reuse, never invent a second

`vMAJOR.MINOR.PATCH` annotated tags, cut only by `release.sh`. The adopting
Mind treats a tag as the ONLY adoptable unit. No second scheme, no
deployment-specific suffixes.

`RELEASES.json` entries already carry
`{version, previous_version, date, breaking, cross_world, summary,
upgrade_recipe, rollback_recipe, min_source}`. **Reuse these fields** — the
pull side reads `breaking` and `min_source` to gate adoption, `upgrade_recipe`
and `rollback_recipe` for C4, `cross_world` for C7. Do not add parallel
metadata.

## C2 — Pull is a RECONCILE, not a mirror

A naive `git pull` is THE named failure mode (`promotion-cycle.md`; 2026-06-24
ZDS led staging on 18 files). Mechanism:

```bash
bash core/scripts/promotion-preflight.sh --source <staging-clone> --target <this-repo>
```

* **exit 0** — target framework is a subset of source. Proceed.
* **exit 2** — DRIFT. For EVERY flagged file, run
  `bash core/scripts/promotion-plan-triage.sh` and record a decision in the
  registry (§a). Never adopt past an unresolved exit 2.

Deployment divergence is **PERMANENT by design, not transitional** — downstream
Minds legitimately carry their own gates, env names, and a domain-adapted
`CLAUDE.md`. "Target is ahead" is therefore an expected steady state, not a
defect to flatten.

## C3 — Pull from TAGS, never HEAD

Adoption compares the **installed tag** against the newest staging tag:

```bash
git -C <staging-clone> tag --list 'v*' --sort=v:refname | tail -1
```

**Where the installed tag is recorded — this did not exist and is defined
here.** Measured 2026-08-24: nothing in `core/scripts` or `core/config` records
an installed/adopted tag, and `check-releases-current.sh` records that
`RELEASES.json` does not travel downstream. So an adopting Mind had no durable
record of what it runs.

The adopting Mind writes `world/installed-release.yaml` (world/, so it is
per-deployment, survives a pull, and is never overwritten by an adoption):

```yaml
installed_tag: v2.12.3
adopted_at: 2026-08-24T04:00:00
adopted_from: claude-mind
source_sha: <sha the tag pointed at>
verified: true          # set only after C4 passes
```

Never infer the installed tag from the working tree — a reconcile leaves the
tree deliberately unequal to any tag.

## C4 — Verify after adopt, and the rollback is one command

Adoption is not complete until the suite is green **on the adopting box**:

```bash
bash core/scripts/run-full-suite.sh          # chunked pytest + invisible + domain halves
```

Read the `VERDICT:` line first, never the totals — `INVALID (contended)` means
the numbers mean nothing, and `GENUINE` can still be false on a chunk-confined
cluster (`.claude/rules/run-full-suite-after-deep-code.md`). Only on a clean
verdict set `verified: true` in `world/installed-release.yaml`.

Rollback, single sequence:

```bash
git reset --hard <source_sha of the previous installed_tag>
bash core/scripts/mind-api-start.sh --restart
```

Prefer the entry's own `rollback_recipe` when it carries one — it is
version-specific and outranks this generic form.

## C5 — Quiesce: time adoption inside YOUR OWN idle window

The design advantage of pull is that each Mind picks its own moment. But
**quiesce is NOT zero-writes** (measured 2026-08-11: a parked Mind with its
loop down since Aug 2 still had presence heartbeats and a user assistant
session writing until 90 min before the fast-forward).

The window check is a conjunction, and BOTH halves are required:

1. **Loop parked, multi-signal** — `bash core/scripts/liveness-check.sh --agent
   <agent> --json` returns `dormant` (never conclude from a stale
   `last_active` alone), corroborated by `heartbeat-stale.sh`.
2. **Disjointness** — the set of locally dirty files and the set of files the
   incoming range would change must not intersect:

```bash
git status --porcelain | awk '{print $2}' | sort > /tmp/dirty
git diff --name-only HEAD..<new-tag> | sort > /tmp/incoming
comm -12 /tmp/dirty /tmp/incoming     # MUST be empty
```

Non-empty intersection ⇒ do not adopt; resolve or stash first. This
disjointness check is what protected 14 dirty files in the 2026-08-11 run.

C5 says WHERE in time to adopt. What brings the Mind to ask the question at
all is the seeded self-update recurring goal in §e.

## C6 — Transport: GitHub, with the existing fleet token

Downstream Minds cannot see the dev workstation's disk; they read Claude-Mind
over GitHub using the fleet token already provisioned per
`core/config/conventions/fleet-secret-provisioning.md`. Token scope: **read-only
on the staging repo** — the pull side never pushes upstream, so a write scope is
an unnecessary blast radius.

Failure behaviour is FAIL-CLOSED and non-escalating: if the fetch cannot
authenticate, the adoption **does not start** — do not fall back to an
unauthenticated clone, a mirror, or a hand-copied tree. The Mind keeps running
its installed tag, which is a correct and safe state, and files an Unblock.

## C7 — Announcements MAY happen; the pull MUST NOT depend on them

`core/scripts/peer-board-post.sh` plus the peer registry in
`core/config/environments/*.yaml` (6 registered: ayoai-mind, claude-mind,
coach-mind, local, serene-mind, zds-mind) MAY announce a release.

**The pull is a POLL, never a subscription.** An adopting Mind discovers a new
tag by listing tags at its own cadence; it must adopt correctly with every peer
dead and the board empty. An announcement is an optimisation that lowers
latency, never a precondition — a missed announcement must be indistinguishable
from a late poll (`cross-deployment-channel.md`: ack ≠ completeness).

## a — Persistent reconcile-decision registry (normative)

A one-shot temp ledger is wrong for a 24–72h cadence: the same exit-2 set
recurs every cycle. Each deployment keeps a DURABLE store the executor reads
and updates at every pull — `world/promotion-decisions.yaml`:

```yaml
decisions:
  - path: core/scripts/example.py
    class: keep-prod-ahead      # re-apply the graft/restore on EVERY pull
    reason: "operator-offload gate, deployment-specific"
    recorded: 2026-08-11
  - path: core/config/example.yaml
    class: back-port-filed      # cite the dev goal that will retire this row
    dev_goal: g-115-XXXX
  - path: core/kernel/example
    class: KERNEL-escalate      # NEVER auto-back-port (guard-097 / guard-098)
```

KERNEL is **down-only**. A `KERNEL-escalate` row stops the adoption and goes to
a human; it is never resolved by the executor.

## b — Seed-delta lane (normative)

World seeds fire only at INIT, so a new seed record shipped in a release never
reaches an already-initialised Mind (measured: the sprint-planning cadence had
to be hand-filed downstream). At every pull, diff the seed file across the
range and surface new records for per-deployment filing:

```bash
git diff <installed_tag>..<new-tag> -- core/config/world-aspirations-initial.jsonl
```

Each new record is filed as a goal in the adopting Mind's own aspirations, or
explicitly declined with a recorded reason. Silence is not a decision.

## c — Quiesce is not zero-writes

Folded into C5 above, which is where an executor looks. Kept as a named
addendum only so the 2026-08-11 measurement stays attached to it.

## d — Daemon recycle at adopt (CORRECTED — the filing's stated cause was wrong)

The filing said the post-commit hook "does NOT fire on an ff-pull (no commit
object)" and concluded adoption must always restart the daemon by hand. The
observation (a stale daemon serving old validation contracts after a
fast-forward) is REAL; the **cause is not**.

Measured 2026-08-24:

* `core/githooks/post-merge` has existed since **2026-07-12** (g-115-2045)
  precisely to close that gap. Its header states git runs no post-commit for a
  merge and that a fast-forward creates no commit at all, and it diffs
  `ORIG_HEAD..HEAD` specifically so the FULL fast-forward range is captured.
  Hooks are live via `core.hooksPath=core/githooks`.
* The ZDS observation is **2026-08-11** — a month AFTER that hook landed. So
  the ff gap was already closed and cannot be the cause.
* The real cause: the recycle predicate `core/scripts/mind-api-code-changed.sh`
  is keyed on daemon **CODE** paths — `mind_api/src/**`, `core/scripts/_*.py`,
  and seven named modules (`gates/**`, `storage_backend.py`,
  `owncloud_backend.py`, `coordination_merge.py`, `owncloud_sync.py`,
  `retrieve.py`, `tree_idf.py`). **`core/config` is not among them** (0 hits),
  yet the daemon reads `core/config` at runtime. A release that changes
  validation contracts under `core/config/**` therefore correctly fails the
  predicate and no recycle fires.

**Mechanism:** after adopting, restart the daemon explicitly when the adopted
range touches `core/config/**` (or any daemon-read surface outside the
predicate):

```bash
git diff --name-only <installed_tag>..<new-tag> -- core/config \
  | grep -q . && bash core/scripts/mind-api-start.sh --restart
```

For daemon-CODE changes `post-merge` already recycles automatically; an
unconditional restart there is redundant but harmless. Do not "fix" this by
widening the recycle predicate to `core/config/**` without measuring — that
would recycle the daemon on every convention edit.

## e — The trigger: a seeded self-update recurring goal (normative)

C1–C7 say how a pull must BEHAVE; none of them says what makes a Mind ask the
question at all. Until this addendum that trigger existed nowhere — measured on
the dev world 2026-08-25: **0 self-update-shaped recurring goals** among 89
recurring goals across 2,975 goals (matcher anti-vacuity-controlled against
`sprint`, which fires). A protocol nothing invokes is indistinguishable from one
that was never written.

The trigger ships in the world seed: goal **`g-002-02`** of `asp-002`
("Operating Rhythm") in `core/config/world-aspirations-initial.jsonl`,
`interval_hours: 72`, `participants: [agent]`, `skill: null`,
`offload_decision: stays-mind`. A Mind initialised from the seed carries the
pull cadence from birth — no per-deployment ceremony, nothing for an operator to
remember, and the default is ON.

### The dev origin opts out by a GATE, not by absence

Dev never pulls from staging (`promotion-cycle.md` owns the direction). It is
tempting to encode that by simply leaving the record out of dev — but the seed
is ONE file shared by every deployment, and a transplant carries it: measured,
`core/` is a **directory** include in `core/config/seed-manifest.yaml` with 9
`exclude_patterns`, **none** matching `config/world-aspirations-initial.jsonl`
(anti-vacuity: that same matcher does fire on `scripts/migrate-to-phase-2-6.sh`).
A re-initialised or transplanted dev origin would therefore receive the record.
So the opt-out is a runtime precondition carried on the goal itself:

> This deployment is NOT the dev origin: `bash core/scripts/env-read.sh value
> ENVIRONMENT_ID` (the subcommand is `value`, **not** `get`) returns a non-empty
> id that is NOT `ayoai-mind`, and `core/config/environments/<that-id>.yaml`
> exists. FAIL-CLOSED: an empty, unreadable, or unregistered id SKIPS the run —
> a Mind that cannot identify itself must not adopt a framework release.

Measured against the real registry, 2026-08-25:

| `ENVIRONMENT_ID` | verdict |
|---|---|
| `ayoai-mind` (this box; live read rc=0) | SKIP — dev origin |
| `claude-mind`, `coach-mind`, `local`, `serene-mind`, `zds-mind` | RUN |
| unregistered (no `environments/<id>.yaml`) | SKIP — fail-closed |
| empty / unreadable | SKIP — fail-closed |

Both verdicts occur over the registry, so the gate is discriminating rather than
vacuously true in either direction.

**Why the subcommand is pinned in the precondition text.** `env-read.sh get
ENVIRONMENT_ID` is the plausible guess and it is wrong — it exits 2 with an
empty value. Under a fail-OPEN predicate that one slip reads as "not the dev
origin", and the dev origin starts pulling from staging: the single direction
the promotion chain forbids. The slip was made while drafting this section,
which is why the gate fails closed AND the wording names the subcommand.

### Two limits, stated because neither is closed here

1. **Seeds fire only at INIT.** `g-002-02` reaches a *fresh* Mind; it never
   reaches an already-initialised one. That gap is precisely what §b (seed-delta
   lane) exists for, and this record is its first customer — an adopting Mind
   picks it up by diffing `core/config/world-aspirations-initial.jsonl` across
   the adopted range and filing it locally. Seeding and §b are complements, not
   alternatives.
2. **No executor exists yet.** Measured 2026-08-25 across four surfaces:
   `.claude/skills/` (no pull/adopt/promotion skill), a repo-wide grep for
   `pull-promotion` (documentation and agent narrative only — no caller),
   `world/forged-skills.yaml` (no match), and retrieval. The recurring goal
   SCHEDULES this protocol; it does not implement it. Whoever builds the
   executor should wire into the seeded cadence rather than invent a second
   trigger.

## Cross-references

- `.claude/rules/promotion-cycle.md` — the chain and the push/pull split
- `core/config/conventions/promotion-runbook.md` — the PUSH side
- `core/config/conventions/fleet-secret-provisioning.md` — the fleet token (C6)
- `core/config/conventions/cross-deployment-channel.md` — peer registry (C7)
- `.claude/rules/run-full-suite-after-deep-code.md` — the verify half of C4
- `.claude/rules/check-team-state-before-silent.md` — liveness semantics (C5)
- guard-097 / guard-098 — KERNEL is down-only (§a)
