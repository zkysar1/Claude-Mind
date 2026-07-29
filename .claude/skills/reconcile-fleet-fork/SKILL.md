---
name: reconcile-fleet-fork
forged: true
forged_by: zeta
forged_date: "2026-07-12"
forged_from: gap-15
description: "Reconciles accumulated two-sided git divergence between a push-deferred box and origin/main — the fleet fork-window integration: pre-merge safety rails, merge, per-file evidence resolution, semantic-seam suite verification, daemon restart, board summary. Use whenever iteration-push reports MERGE CONFLICT or defers its fetch-merge on consecutive closes, whenever a goal says 'integrate origin/main', 'reconcile origin divergence', 'merge origin into local', or 'catch up with origin', or whenever the repo is both ahead AND behind origin (two-sided rev-list divergence). MUST use core/scripts/fleet-reconcile-preflight.sh for the preflight probe — never ad-hoc git status/rev-list checks."
user-invocable: false
minimum_mode: assistant
tools_used: [Bash, Read, Edit, Grep]
companion_scripts: [core/scripts/fleet-reconcile-preflight.sh]
triggers:
  - "integrate origin/main"
  - "reconcile origin divergence"
  - "merge origin into local"
  - "catch up with origin"
  - "iteration-push MERGE CONFLICT"
  - "fetch-merge deferred"
  - "behind origin"
---

# /reconcile-fleet-fork — Fleet Fork-Window Reconciliation

Integrates accumulated `origin/main` divergence into a push-deferred box.
Wraps the sig-29 playbook: the knowledge lives in the tree node
`world/knowledge/tree/system/fleet-git-divergence-reconciliation.md` (read it
in Phase 0 — it carries the Verified Values of prior reconciles and the
per-file evidence rules); the lessons live in rb-3161 and sig-29.

Two prior executions calibrate expectations: g-115-2013 (590 behind / 63
ahead, 31 conflicts, 2 semantic seams found by the suite AFTER merging) and
g-115-2022 (7 commits, 3 conflicts, suite green FIRST run because factual
disagreements were fact-checked DURING resolution). The second shape is the
target.

## Core doctrine (from sig-29 / rb-3161)

- **Auto-merge success is NOT semantic coherence.** The only real failures
  come from files that merge CLEAN but cross contracts between parallel
  implementations. The FULL test suite is the semantic-conflict detector.
- **Provenance-check before adapting a failing test**:
  `git show origin/main:<test>` — if origin never had it, it is yours to
  adapt; if upstream owns it, change the RESOLUTION instead.
- **Fact-check factual disagreements DURING resolution** (rb-245-for-merges):
  when the two sides disagree on a FACT about a writer/behavior, re-read the
  writer in-turn — never pick a side by loyalty.
- **Resolve per-file by EVIDENCE**: superset analysis, attribution tests for
  bulk-commit-only touches, three-way base diffs.
- **Constitutional anchors take the origin side untouched** — never hand-edit
  `.claude/settings.local.json` or `settings-structural-validator.{py,sh}`
  during a merge (hard rule; the preflight reports anchor deltas).

## Restricted Operations

MUST use `core/scripts/fleet-reconcile-preflight.sh` for the preflight probe
— never ad-hoc `git status` / `git rev-list` one-offs. The script is the
read-only single source for divergence counts, working-tree churn,
untracked-collision detection (the merge-abort class), anchor deltas, and the
conflict preview. Mutations stay with this skill's phases so every
destructive step passes its rails.

## Phase 0: Context

1. `Bash: bash core/scripts/fleet-reconcile-preflight.sh` — capture JSON.
   IF `reconcile_needed` is false: report "no divergence — nothing to
   reconcile" and stop (terminal Bash echo).
2. Read the playbook node:
   `Bash: source core/scripts/_paths.sh; cat "$WORLD_DIR/knowledge/tree/system/fleet-git-divergence-reconciliation.md"`
3. `Bash: bash core/scripts/retrieve.sh --category "fleet-git-divergence-reconciliation semantic seams provenance merge conflict" --depth shallow`
   — surfaces guardrails/rb added since the last run. (Category tokens match
   the node KEY + doctrine words — the generic "fleet fork reconciliation
   merge divergence" phrasing under-surfaced sig-29/rb-3161 at invocation 1.)
   sig-29 itself STILL does not surface through retrieve (0 pattern_signatures
   at invocations 1 AND 2 — its stored category/tags don't token-match); read
   it directly: `Bash: bash core/scripts/pattern-signatures-read.sh --id sig-29`
   (fail-open if the flag form differs — the doctrine above already embeds
   sig-29's lesson).
4. `Bash: git log HEAD..origin/main --oneline` — see WHAT is incoming (whose
   goals, which subsystems) before touching anything; pairs with the preflight
   conflict preview to set resolution expectations (added after invocation 1).

## Phase 1: Safety Rails (from preflight `safety_rails_needed`)

1. Pre-merge branch: `Bash: git branch pre-merge-$(date +%Y%m%d-%H%M) HEAD`
2. IF `working_tree_churn > 0`: commit the churn first (a merge over a dirty
   tree entangles resolution diffs with unrelated edits):
   `Bash: git add -A && git commit -m "chore: pre-reconcile working-tree churn"`
   (or route through `iteration-commit.sh` when a goal context is active).
3. IF `untracked_collisions` non-empty: archive-before-delete — tarball the
   listed files to `agents/<agent>/temp/reconcile-collisions-<date>.tar.gz`,
   verify the tarball lists them (`tar -tzf`), THEN remove the originals.
   `git merge` aborts if these stay in place.

## Phase 2: Merge

1. `Bash: git merge origin/main` (no --no-commit needed; conflicts pause it).
2. IF the merge auto-completes with zero conflicts: skip to Phase 4 — the
   suite is still MANDATORY (semantic seams merge clean).
3. Enumerate conflicts: `Bash: git status --porcelain=v1 | grep -E '^(UU|AA|DU|UD|AU|UA|DD)'`

## Phase 3: Per-File Evidence Resolution

For EACH conflicted file, in this order:

1. **Anchor file?** (preflight `anchor_deltas` or the 3 anchor paths) →
   `git checkout --theirs <path>` (origin side untouched). Never edit.
2. **Establish what each side changed**: three-way base diff —
   `git diff <merge_base> HEAD -- <path>` vs `git diff <merge_base> origin/main -- <path>`.
3. **Attribution test**: if the only local touch is a bulk/over-inclusion
   commit (check `git log <merge_base>..HEAD -- <path>`), take THEIRS.
4. **Superset analysis**: if one side's implementation contains the other's
   behavior, take the superset; retire the losing parallel implementation in
   the SAME close (script + its test) and enumerate what invariant coverage
   the retired tests uniquely guarded — re-home that coverage.
5. **Factual disagreement?** If the two sides assert different FACTS about a
   writer or behavior (e.g., "this store is read-modify" vs "append-only"),
   STOP and re-read the writer in-turn before resolving. The side matching
   the code wins; fix the other side's artifacts (tests, comments) in the
   same pass.
6. **Tests that conflict or will fail**: provenance-check first —
   `git show origin/main:<test-path>`. Origin never had it → adapt the test
   to the adopted implementation. Origin owns it → change the resolution.
7. **Duplicate-definition sweep**: when adopting upstream's canonical
   placement of code you previously grafted elsewhere, grep-count the moved
   symbol names (expect exactly 1 definition each) before staging.
8. Stage each resolved file; when all resolved: `git commit` (merge commit).

## Phase 4: Semantic Verification (MANDATORY — even on clean merges)

1. Run the daemon-safe full suite with the storage pin (guard-955/guard-672 —
   the pin is mandatory on own-cloud boxes; tmp-world tests collide with
   production S3 keys without it):
   ```
   STORAGE_BACKEND=local PYTHONUNBUFFERED=1 python3 -u -m pytest core/scripts/tests \
     -m "not daemon_integration" -q > agents/<agent>/temp/reconcile-suite.log 2>&1; \
     echo "SUITE_EXIT=$?" >> agents/<agent>/temp/reconcile-suite.log
   ```
   Use `python3` (or `py -3` on Windows) — NEVER bare `python`: the shim PATH
   injection is hook-dependent and did not land on a backgrounded call at
   invocation 2 (SUITE_EXIT=127 "python: command not found"; the suite never
   ran and the sentinel caught it).
   Read the log's tail for the SUITE_EXIT sentinel — do not trust waiter
   exit codes or empty task stdout (~32min runtime is normal; collection is
   silent >50s). SUITE_EXIT=127 means the interpreter was not found — the
   suite did NOT run; that is not a test result.
2. FOR EACH failure: provenance-check the test (Phase 3.6 rule), classify as
   semantic seam (contract crossing between parallel implementations) vs
   pre-existing failure (`git stash` + re-run on pre-merge branch when
   unclear), fix at the correct side, re-run the affected subset, then the
   full suite to green.

## Phase 5: Finalize

1. IF the merge touched daemon code (`bash core/scripts/mind-api-code-changed.sh
   "HEAD~1"` exits 0): restart EXPLICITLY — merge commits do NOT fire the
   post-commit daemon-restart hook (`git merge` invokes post-merge, and
   `core/githooks/` has no post-merge hook; observed live at invocation 3:
   daemon sha stayed at the pre-merge commit until a manual restart).
   `Bash: bash core/scripts/mind-api-start.sh --restart`, then verify the
   served sha MATCHES the merge commit:
   `curl -sf --max-time 2 http://127.0.0.1:$(cat mind_api/state/daemon.port)/v1/admin/health`
   → `git_head_sha` == `git rev-parse --short HEAD`. A 200 with a STALE sha
   is the rb-915 dead-code state — liveness alone is not the check.
   `/v1/admin/health` is the canonical route (rt_is_up, `_runtime.sh:144`);
   bare `/health` 404s (probed at invocation 2).
2. `Bash: bash core/scripts/fleet-reconcile-preflight.sh --no-fetch` —
   confirm `behind: 0` and `anchor_deltas: []` post-merge.
3. Push stays policy-deferred on read-only boxes — do NOT push unless the
   box's git policy allows it; `iteration-push` handles the eligible case.

## Phase 6: Record

1. Board summary (coordination channel): divergence numbers, conflict count,
   resolution split (OURS/THEIRS/UNION), suite result, notable seams:
   `echo "<summary>" | Bash: bash core/scripts/board-post.sh --channel coordination --type status --tags "fleet-reconcile,<goal-id>"`
2. Append a Verified Values row for this reconcile to the tree node
   (`fleet-git-divergence-reconciliation.md`) via Edit — divergence, conflict
   count, merge sha, suite result, anything the playbook should learn.
3. sig-29 outcome: judged at the goal's Phase 6 spark
   (`pattern-signatures-record-outcome.sh sig-29 CONFIRMED|CORRECTED`) — note
   the verdict evidence in the goal summary so the spark can record it.

## Error Handling

- `git merge` aborts on untracked files origin tracks → Phase 1.3 was
  skipped; run it, then retry the merge.
- Suite hang → `faulthandler_timeout=600` in pytest.ini aborts with a stack;
  read the dump, do not kill blindly.
- Merge gone wrong beyond repair → `git merge --abort`, or reset to the
  Phase 1 pre-merge branch (`git reset --hard pre-merge-<stamp>`); the
  tarball from Phase 1.3 restores any removed untracked files.
- Anchor conflict that cannot take origin side cleanly → STOP; route to the
  user (constitutional anchor changes need a user-authorized path).

## Input/Output Contract

- Input: optional goal context (goal-id for board tags + commit ceremony).
  No arguments required — the preflight discovers everything.
- Output: merged HEAD (behind=0), green suite log at
  `agents/<agent>/temp/reconcile-suite.log`, board post, updated tree node.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the Phase 6 `board-post.sh` call (or the Phase 0
"no divergence" Bash echo on the early exit). Never end with a text summary.
