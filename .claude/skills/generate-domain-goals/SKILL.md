---
name: generate-domain-goals
description: "Supply-side domain goal generation: read the domain's product surfaces through six generation lenses (metric-driven, journey-driven, code-reality, revenue-path, distribution, critic), generate evidence-backed user-story candidates, adversarially VERIFY every evidence claim BEFORE filing, then file survivors into the domain's aspiration lanes with dedup handling and a board announcement. Use when the user says 'generate product work', 'inject goals', 'we need more domain work', or when the recurring generation goal fires. User-invocable AND agent-callable."
user-invocable: true
triggers:
  - "/generate-domain-goals"
  - "generate product work"
  - "inject product goals"
  - "generate domain goals"
tools_used: [Bash, Read, Grep, Glob, Write, Agent]
conventions: [aspirations, goal-schemas, board]
minimum_mode: assistant
revision_id: "skill-bootstrap-generate-domain-goals-2026-08-12"
previous_revision_id: null
---

# /generate-domain-goals — Supply-Side Domain Goal Generation

Generates well-evidenced, user-story-shaped domain goals from the domain's
REAL surfaces (code, live apps, quality metrics, user journeys) and files
them into existing aspiration lanes. The counterpart to demand-side skills:
`/create-aspiration` looks inward (agent drives), `/aspirations-strategic-scan`
looks at portfolio health — this skill looks OUTWARD at the product and asks
"what would a best-in-class product manager file next?"

Formalized 2026-08-12 from a user-directed bulk injection (102 raw candidates
→ 52 filed → post-hoc fresh-eyes review found 6 false premises, 1 duplicate,
and 19 citation errors that had to be patched in the live queue). The lesson
this skill hard-codes: **verification runs BEFORE filing, not after.** A goal
with false evidence is worse than no goal — an executing agent inherits the
false premise and builds a duplicate of something that already exists.

**Hybrid skill**: user-invocable AND agent-callable (the recurring generation
goal invokes it in standard mode). Requires assistant or autonomous mode — it
writes queue state.

## Sub-commands

```
/generate-domain-goals            — Standard pass: ONE rotating generation lens,
                                    bounded batch, inline verification.
                                    This is the recurring-goal mode.
/generate-domain-goals --ultra    — USER-INVOKED ONLY: authorizes a multi-agent
                                    Workflow fan-out (parallel surface readers,
                                    per-lens generators, adversarial judges,
                                    independent evidence verifiers, synthesis).
                                    An agent-initiated (recurring-goal) run MUST
                                    NOT pass --ultra: Workflow orchestration
                                    requires explicit user opt-in, and a
                                    recurring firing is not one.
/generate-domain-goals --dry-run  — Full pipeline through verification, print
                                    the would-file package, write NOTHING.
```

## Step 0: Load Conventions

Bash: `load-conventions.sh` with each name from the `conventions:` front
matter. Read only the paths returned (files not yet in context). If output is
empty, all conventions already loaded — proceed.

## Phase 0: Mode Check + Domain Brief (Pattern B hook slot)

1. Bash: `session-mode-get.sh` → refuse politely if mode is `reader`
   ("generate-domain-goals writes queue state — requires assistant or
   autonomous mode").

2. Load the domain brief via the `goal-generation-brief` hook slot:

```
Bash: load-conventions.sh goal-generation-brief → IF path returned: Read it
# Procedural convention — gate on file EXISTENCE, not load status.
# Hook slot registered in core/config/conventions/domain-hooks.md (Pattern B).
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/goal-generation-brief.md" && echo "exists"
IF exists:
    The brief is the domain half of this skill. It supplies: target
    work_class, the lane table (aspiration IDs + what belongs in each),
    personas, journey stages, product-surface locations (repos, live apps,
    doc roots), quality-metric stores (rubric file + trend data), routing
    pins, supply thresholds (high_water_mark, batch_cap, tier2_holdback),
    and known ground truths from prior verification passes.
ELSE:
    Print: "No goal-generation-brief found for this world. Create
    world/conventions/goal-generation-brief.md (shape: Purpose / Parameters /
    Lanes / Personas / Surfaces / Metric Stores / Routing Pins / Known Ground
    Truths / Fail-Open / Downstream Consumers — see domain-hooks.md
    'Convention File Format') describing THIS domain's product surfaces,
    then re-invoke." → DONE (graceful no-op; a fresh world has no surfaces
    to generate against).
```

3. Read directional context: `world/program.md` (via `world-cat.sh
   program.md`) and the current strategic focus (`team-state-read.sh --field
   strategic_focus` — fail-open if absent). Generated goals must serve the
   standing directives, and lane weighting follows the strategic focus.

## Phase 1: Supply Governor (do NOT flood the queue)

Consolidate-before-expand applies to generation itself: new supply is only
warranted when the available backlog is thin.

1. Measure available supply: count goals across the brief's lanes with
   `status: pending`, no `defer_reason`, no blocker, matching the brief's
   target work_class. (`aspirations-query.sh` per lane; the brief names the
   exact query.)
2. IF available ≥ brief.high_water_mark:
   → Post a one-line board tick ("supply healthy: N available ≥ M, no
   generation this cycle") to the coordination channel, type `status`.
   → DONE. This is the cheap recurring path — a healthy queue costs one
   query and one post.
3. ELSE set `batch_target = min(brief.batch_cap, high_water_mark - available)`.
   Generation proceeds sized to the actual gap.

## Phase 2: Recon — read the REAL surfaces

Evidence comes from reads performed THIS run, never from memory or training
priors. The brief names the surfaces; this phase reads them.

**A read performed THIS run can still be a read of the past — assert the clone
before reading it.** Phase 4 re-probes every citation from a different CONTEXT,
but against the SAME working tree, so a stale checkout makes generator and
verifier read identical stale bytes and *agree*. Independence of context is not
independence of data source, and a stale citation looks better-evidenced than
average precisely because it was checked. This is also how the brief's Known
Ground Truths acquire false premises: Phase 6 refreshes them from this run's
verification pass, so a stale read becomes a recorded "verified" fact the next
run inherits.

Bash: `py -3 core/scripts/product-repo-freshness.py --repo <each repo this lens will read> --json`

Read `cannot_check` FIRST: `false` means the check ran, `true` means it could
not — which is NOT an all-clear (guard-1084; the tool is advisory-silent, so
silence and non-execution look alike). Then for every record with `behind > 0`:
`git -C <repo> pull --ff-only` and re-run the check before reading a single
line. Where a repo cannot be pulled, every citation drawn from it MUST carry
`(clone behind N)` inside its EVIDENCE text, and may not by itself justify
`priority: HIGH`.

Staleness is BOX-DEPENDENT — measure it, never assume it, and never inherit
another box's reading. Two boxes in one fleet, measured a day apart: one had
32% of its checkouts behind (worst lag 10 commits), the other 94% (worst lag
57) — a 3x difference in hit rate and 5.7x in worst lag, same repos, same
remotes. So "the fleet is fine, someone checked" is not a finding about YOUR
clone, and a box that rarely generates evidence can be the stalest one. The
stale set on the low box still included the repos of the highest-priority
product lane, so a small percentage is not a safe percentage either.

- **Standard mode**: pick ONE generation lens by rotation —
  `lens_index = recurring_goal.achievedCount % 6` (derives rotation from the
  recurring goal's own cycle counter; no extra state file). Recon only the
  surfaces that lens needs. Inline reads (Grep/Glob/Read); the Agent tool MAY
  be used for 1-3 bounded read-only scouts when a surface is large.
- **--ultra mode**: parallel reader agents per surface (repos, live-app
  journey walk, metric stores, existing-goal corpus), then per-lens
  generators. Follow the Workflow tool's size guidance; verification agents
  (Phase 4) are part of the same run.

Recon always exports the dedup corpus: every open goal in the brief's lanes
(`asp|goal-id|status|priority|title` per line) — generators and verifiers
both consume it.

## Phase 3: Generate — the six lenses

Each lens is a different question asked of the same domain. A standard run
works one lens deeply; an --ultra run staffs all six. Candidates from every
lens obey the same Candidate Contract (below).

| # | Lens | The question | Evidence source |
|---|---|---|---|
| L1 | **Metric-driven** | Which measured quality axes are weakest, and what work moves each? | The brief's metric store. Verify axis NAMES against the rubric FILE — never from memory (a prior run filed goals against a mislabeled axis). |
| L2 | **Journey-driven** | For each persona × journey stage (arrive → first value → habit → share → pay), what does a real user actually hit? | Walk the LIVE surface this run — click the app, run the client, read the rendered page. |
| L3 | **Code-reality** | Three sweeps: BUILT-BUT-INVISIBLE (backend capability with no user-facing expression — highest leverage, it is paid-for value nobody sees), DEFINED-BUT-UNREACHABLE (config/features with no path for any user to trigger), WIRED-BUT-BROKEN (integrations failing silently). | Read the code. Cite file:line. Grep before claiming absence. |
| L4 | **Revenue-path** | Walk the money end-to-end: see price → choose → pay → receive → renew/upgrade. Every broken or missing link is a candidate. | The brief's billing/account surfaces. |
| L5 | **Distribution** | How does a delighted user bring the next user? (share hooks, public artifacts, attribution credits, referral surfaces) | Journey recon + surface reads. |
| L6 | **Critic** | After L1-L5: which persona has zero candidates? Which journey stage? Which standing directive? Which lane? File gap-fillers or explicitly record "gap acknowledged, not filed". | The candidate set itself + the brief. |

## The Candidate Contract (every candidate, every lens)

- **title**: verb-led, ≤120 chars, names the user-visible change. Banned
  verbs: improve, enhance, polish, explore (unfalsifiable).
- **description** sections, pipe-separated:
  `STORY:` As <named persona>, I want <capability> so that <outcome>.
  `PERSONA+STAGE:` persona + journey stage.
  `EVIDENCE:` dated, falsifiable: file:line citation, metric datum, or a
  live-surface observation from THIS run. Negative claims ("no X exists")
  MUST carry their search trail ("searched A, B, C — absent").
  `SCOPE:` bounded (one loop iteration); larger work is marked
  `needs-decompose` and says so in scope.
  `ACCEPTANCE:` 2-5 checks observable AT VERIFICATION TIME (a session shows
  X; page renders Y; test T passes; metric Z read from store). Criteria
  requiring data that will not exist until after launch are staged
  ("query runs against synthetic data pre-launch; real cohorts post-launch").
  `ROUTING:` intended agent + one-line rationale, respecting the brief's
  routing pins.
- **priority**: HIGH only when the work gates a ship date, revenue, or a
  catastrophic metric; else MEDIUM.
- **lane**: an aspiration ID from the brief, or `new-lane` ONLY when nothing
  fits (see Phase 5.4).

## Phase 4: VERIFY BEFORE FILING (the gauntlet — never skip)

Measured baseline (2026-08-12): ~50% of raw candidates survive end-to-end.
If >90% of a batch survives this phase, the verification was too soft —
re-run it harder rather than filing.

1. **Story-level package dedup**: two candidates telling the same story are
   duplicates even with different titles (title-level dedup measurably
   missed one). Compare STORY + EVIDENCE overlap; merge or drop.
2. **Queue dedup**: against the exported open-goal corpus. Prefer dropping
   or sharpening over planning to override the filing gate.
3. **Adversarial evidence verification** — the soul of this skill. For every
   candidate, a context OTHER than the one that generated it re-probes:
   - every file:line citation (open the file; does the content support the
     claim? record actual line on drift);
   - **the CLONE that citation was read from** — re-assert it at verify time
     (`py -3 core/scripts/product-repo-freshness.py --repo <repo> --json`), not
     just at Phase 2, because a partner may have pushed mid-run. `behind > 0`
     caps the verdict at PARTIAL until the repo is pulled and the citation
     re-probed: this re-probe reads the same tree the generator did, so its
     agreement is evidence about the tree, never about origin. Note a symbol
     grep cannot substitute — a renamed-body refactor keeps the call site and
     swaps the implementation (`_ssh_cmd` wrapping `_ssm_run`), so "the symbol
     is still there" answers yes while meaning the opposite;
   - the 1-3 load-bearing negative claims (search for X anyway; name what
     was searched);
   - **the kill question: does what this goal builds ALREADY EXIST?** (5 of
     6 false premises in the baseline run were already-built capabilities);
   - counts and numbers by MEASURING, not by reading docs (a doc undercounted
     an action registry two separate ways in the baseline run).
   Standard mode: the executing agent re-probes with fresh reads AFTER
   generation, treating its own candidates as hostile claims. --ultra mode:
   independent verifier agents partitioned by surface, then a synthesis
   verdict. Verdicts: VERIFIED / PARTIAL (minor drift, premise intact) /
   WRONG (kill or rewrite) / UNVERIFIABLE (kill).
4. **Metric-name check**: every axis/metric named in a candidate matched
   against the rubric file.
5. **Acceptance-verifiability check**: every criterion observable at
   verification time or explicitly staged.

Kill or fix WRONG candidates here. Convert already-exists candidates into
validation goals when the story still matters ("verify the existing X is
user-visible; tune only if a session shows it is not") — that preserves the
product insight with true evidence.

## Phase 5: File

1. Payload per goal (see `goal-schemas.md` for field semantics):
   `title, description, priority, status: pending, category` (from brief),
   `participants: ["agent"]`, `intended_agent` (from ROUTING),
   `work_class` (brief's target class), `origin_signal` — user-invoked run:
   `user_directive`; recurring firing: `recurring_cadence:<recurring-goal-id>`,
   `verification: {outcomes: [ACCEPTANCE items], checks: []}`.
2. File sequentially, fail-soft per goal:
   Bash: `bash core/scripts/aspirations-add-goal.sh <asp-id>` with JSON on
   stdin. Capture each allocated goal ID from the response.
3. Duplication-gate refusals: INSPECT each (read the cited existing goal).
   Genuine dup → drop. False positive → refile with
   `--override-duplication "<specific inspected difference>"`. Never
   `--override-all`.
4. **New aspiration** (rare): only when survivors fit no lane AND
   consolidate-before-expand rule 3 is satisfied (existing-lane health
   checked, justification recorded). `aspirations-add.sh` requires the
   `goals` key — pass `"goals": []` and file goals separately. Flag the new
   lane for strategic-focus consideration in the Phase 6 post; do NOT edit
   strategic focus from this skill.
5. **Holdback**: file at most `batch_target`; hold the remainder as a
   tier-2 list in the announcement artifact for the next cycle.
6. Read-back verification: `aspirations-query.sh --goal-field id <gid>
   --full` on a sample (the plain query is a truncated projection — always
   `--full` for content checks).

## Phase 6: Announce + Refresh the Brief

1. Persist the run package (candidates, verdicts, filed IDs, tier-2 holdback)
   to `agents/<agent>/temp/goal-generation-<date>.json`.
2. Board announcement — coordination channel, type `status`, tags
   `product,generation`: counts (raw → verified → filed), lanes touched,
   goal-ID ranges, corrections/kills worth knowing, tier-2 count, any
   new-lane flag. Sidecar posts follow guard-409: the announcement is its
   own Bash call, `|| true`, never chained to a state write.
3. **Brief refresh** (the hook slot's feedback path): update the brief's
   Known Ground Truths with facts verified this run (correct counts, axis
   names, already-exists findings) and its lane table with new lanes. Use
   Edit on `world/conventions/goal-generation-brief.md`; append a ledger
   record to `world/conventions/convention-changes.jsonl`
   (`type: "edit", target: "goal-generation-brief", source: "generation-run"`).
   Stale ground truths in the brief are how the NEXT run inherits this
   run's mistakes.

## Verification (for the recurring goal's verify phase)

- Filed goal IDs exist via `--full` read-back (or the supply-governor
  skip decision is recorded in the board tick).
- Board post ID captured.
- Run package written to the agent temp store.

## Anti-patterns

- Filing first, verifying later — the exact defect this skill exists to fix.
- Title-level-only dedup (story-level is the bar).
- Evidence written from memory, a summary, or a doc's claim about the code
  instead of a read of the code (docs undercounted twice in the baseline).
- Skipping the already-exists kill question.
- "Improve/enhance/polish" titles.
- Acceptance criteria needing post-launch data, unstaged.
- Filing past the supply governor's batch target (flooding).
- Passing `--ultra` from a recurring firing (Workflow needs user opt-in).
- Editing strategic focus from this skill (flag it; the owner decides).
- A >90% verification survival rate accepted without re-verification.

## Return Protocol

See `.claude/rules/return-protocol.md`. This is a sub-skill: terminate with
a Bash tool call handing control back, e.g.
`Bash: echo "generate-domain-goals complete — return to caller"`. Never end
on a text summary.
