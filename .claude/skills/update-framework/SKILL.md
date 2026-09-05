---
name: update-framework
description: 'Update THIS deployment to the newest framework release from the staging Mind, knowledge-first: reads pull-promotion.md, detects whether this checkout is git-fed (framework v* tags reachable in this repo) or a transplant (uses framework-pull.sh), then merges or adopts the newest release tag, verifies, and records world/installed-release.yaml. Use whenever the user says update yourself, update your framework, pull the latest framework, pull fresh from the staging Mind, adopt the newest release, self-update, or when recurring goal g-002-02 (Framework self-update check) fires. Never web-search or pip install for this; the framework arrives over git from the staging repo named in promotion-cycle.md.'
user-invocable: true
triggers:
  - "/update-framework"
  - "update framework"
  - "pull latest framework"
  - "self-update"
tools_used: [Bash, Read]
conventions: [pull-promotion]
minimum_mode: assistant
revision_id: "skill-bootstrap-update-framework-2515a0"
previous_revision_id: null
---

# /update-framework — Adopt the newest framework release

**Hybrid skill**: user-invocable (chat: "update yourself", "pull the latest
framework") AND agent-callable (recurring goal g-002-02 fires it in autonomous
mode). Valid in assistant and autonomous mode.

The framework does not come from the web. It comes from the staging Mind over
git (`.claude/rules/promotion-cycle.md`). Do NOT web-search, do NOT pip install,
do NOT hand-copy files, do NOT ask the user to fetch anything. There are two
deployment shapes and this one skill handles both — detect, then act.

## Step 0: Load the convention

Bash: `bash core/scripts/load-conventions.sh pull-promotion` — Read the path it
returns (skip if already in context). C1–C7 and addenda a–h are the WHY; the
steps below are the HOW. When they disagree, the convention wins.

## Step 1: Refuse at the dev origin (fail-closed)

Bash: `bash core/scripts/env-read.sh value ENVIRONMENT_ID` (subcommand is
`value`, not `get`).

- `ayoai-mind` → STOP. The dev origin never pulls from staging
  (`promotion-cycle.md`). Say so and end with the Return Protocol.
- empty, rc≠0, or no `core/config/environments/<id>.yaml` → STOP. A Mind that
  cannot identify itself does not adopt a framework release (§e).
- any other registered id → continue.

## Step 2: Detect the shape (one command)

Bash: `git tag --list 'v*' --sort=v:refname | tail -1`

- **non-empty** → **GIT-FED**: this checkout carries the framework's release
  tags; `git remote get-url origin` names the staging repo (C6). Go to Step 3A.
- **empty** → **TRANSPLANT**: framework files were planted (the plant carries no
  tags, C3); there is no framework history here. Go to Step 3B.

State the shape and the evidence (the tag, or its absence) before proceeding.

## Step 3A: Git-fed — merge the newest release tag in place

1. Bash: `git status --porcelain -- core .claude mind_api CLAUDE.md`. Any line
   is an uncommitted LOCAL framework change. If this deployment's
   `core/config/environments/<id>.yaml` carries `framework_origin:` (§g) the
   change is stray: `git checkout HEAD -- <path>`. Otherwise commit it first so
   the merge can reconcile it (C2: a pull is a reconcile). Re-run until clean.
2. Bash: `git fetch origin --tags`
3. `newest` = `git tag --list 'v*' --sort=v:refname | tail -1`.
   `installed` = `installed_tag` from `world/installed-release.yaml` if it
   exists, else `git describe --tags --abbrev=0 HEAD`. If `origin/main`
   carries a higher `__version__` (`git show origin/main:mind_api/src/__init__.py`)
   than `newest`, the push hop was left unfinished: post that on the
   coordination board and still adopt the TAG, never HEAD (C3).
4. `newest` == `installed` → nothing to adopt. Skip to Step 5 and close
   ROUTINE naming both tags — a no-op run is the expected steady state.
5. Disjointness (C5): the dirty set and the incoming set must not intersect.
   Bash: `comm -12 <(git status --porcelain | awk '{print $2}' | sort) <(git diff --name-only HEAD.."$newest" | sort)`
   Non-empty → STOP; resolve or stash first, never adopt over it.
6. Bash: `git merge --no-edit "$newest"` — a fast-forward when there are no
   local commits, a real merge when there are. On CONFLICT: `git merge --abort`,
   STOP, file an Unblock naming the conflicting paths. NEVER `--force`, NEVER
   `reset --hard` — the tree holds uncommitted store writes.
7. Daemon (addendum d): Bash:
   `git diff --name-only "$installed".."$newest" -- core/config | grep -q . && bash core/scripts/mind-api-start.sh --restart`
   The `post-merge` hook already recycles for daemon CODE when
   `git config core.hooksPath` is `core/githooks`; if it is not, restart
   unconditionally — redundant is harmless, stale is not.
8. Bash: `bash core/scripts/framework-pull.sh --record-installed "$newest"`
   (the one writer for `world/installed-release.yaml` on this shape).
   Continue to Step 4.

## Step 3B: Transplant — run the executor, never re-derive it

1. Bash: `bash core/scripts/framework-pull.sh` — the PLAN (default; copies
   nothing). If it prints `No source repo resolved`, this box has no local
   clone of staging: clone the staging repo (C6 names it) beside this repo as
   `../claude-mind`, or set `FRAMEWORK_SOURCE_REPO=<path>` in
   `agents/<agent>/local-paths.conf`, then re-run. Do not web-search for it.
2. Read the plan.
   - rc=0 (clear) → Bash: `bash core/scripts/framework-pull.sh --adopt`. It
     copies, verifies in a worktree pinned at the adopt commit (C4), rolls back
     on red, records `world/installed-release.yaml`, and recycles the daemon.
   - rc=2 (blocked) → Bash: `bash core/scripts/promotion-plan-triage.sh`.
     Record a decision per flagged file in `world/promotion-decisions.yaml`
     (§a: keep-prod-ahead / back-port-filed / KERNEL-escalate). Never adopt
     past an unresolved exit 2 (C2).
   - rc=3 (rolled back) → read the report, file an Unblock with the failing
     half named. The installed tag is unchanged and that is a safe state.
3. Continue to Step 4.

## Step 4: Verify and surface the seed-delta

- **Git-fed only** (the transplant adopt already verified): Bash:
  `bash core/scripts/run-full-suite.sh`. Read the `VERDICT:` line first, never
  the totals (`.claude/rules/run-full-suite-after-deep-code.md`; on a box with
  a live daemon do not pin a worktree — guard-5866). On a clean verdict, Bash:
  `bash core/scripts/framework-pull.sh --record-installed "$newest" --verified`.
  A red verdict is a finding, not a reason to reset: file it, leave the merge.
- **Seed-delta (§b)**: Bash:
  `git diff "$installed".."$newest" -- core/config/world-aspirations-initial.jsonl`
  (transplant: run it in the source clone). Each NEW record is filed in this
  Mind's own aspirations via `aspirations-add-goal.sh`, or declined with a
  reason stated in the report. Silence is not a decision.
- Bash: `grep __version__ mind_api/src/__init__.py` — say the version.

## Step 5: Report and return

One short paragraph: shape detected, `installed` → `newest` (or already
newest), verdict, daemon recycled or not, seed-delta count, anything filed.
Then the Return Protocol.

## Chaining

- **Called by**: user directly (`/update-framework`, or `/respond` routing an
  "update yourself" message); the agent via recurring goal g-002-02 in
  autonomous mode
- **Calls**: framework scripts only — `load-conventions.sh`, `env-read.sh`,
  `framework-pull.sh`, `promotion-plan-triage.sh`, `run-full-suite.sh`,
  `mind-api-start.sh`, `aspirations-add-goal.sh`; no other skills
- **Modifies**: the framework checkout (git merge or adopt),
  `world/installed-release.yaml`, `world/promotion-decisions.yaml`, this Mind's
  aspirations (seed-delta filings)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not
text. The terminal action is the Bash that records the installed release, or on
a no-op run a Bash `echo` naming both tags. Never end on the Step 5 summary.
