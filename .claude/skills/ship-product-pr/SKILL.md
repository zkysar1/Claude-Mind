---
name: ship-product-pr
forged: true
forged_by: alpha
forged_date: "2026-07-15"
forged_from: gap-011
description: "Ships a product-repo (Ayoai/ZDS) code change as a reviewed GitHub pull request end-to-end via world/scripts/product-pr-flow.sh: creates a feature branch, commits with attribution, runs the repo's full-suite pre-push build gate, pushes, opens the PR with gh, and watches CI checks until green (the checks-gate) before declaring done — optionally merging and fast-forwarding main. Use whenever the agent has product-repo changes staged and needs to open a PR, or the user or a goal says 'open a PR', 'create a pull request', 'raise a PR', 'ship the fix as a PR', or 'branch, commit, push, PR'. MUST use this skill for product-repo PRs — never hand-run the branch/commit/push/gh-pr/checks-watch steps ad hoc, and never mark a change done on a red or still-running pipeline. Not for direct-to-main pushes (that path is post-execution.md Step 2)."
user-invocable: false
triggers: [open a PR, create a pull request, raise a PR, ship the fix as a PR, branch commit push PR, open a pull request for, product-repo PR flow, pull request flow]
tools_used: [Bash]
companion_scripts: [world/scripts/product-pr-flow.sh]
conventions: [post-execution]
minimum_mode: assistant
revision_id: "skill-forge-ship-product-pr-g115-2241"
previous_revision_id: null
---

# /ship-product-pr — Product-Repo Pull-Request Flow

Ship a change to a product repo (under `AGENT_WRITE_PATH` — the Ayoai / ZDS
estates) as a REVIEWED pull request, not a direct push to main. One call to the
companion script runs the whole ~8-step ceremony with the CI checks-gate baked
in.

This is the PR path. The direct-to-main path is
`world/conventions/post-execution.md` Step 2 — use that when the repo's
convention is trunk-based commits; use THIS when the change warrants review + a
CI gate before it lands.

## Restricted Operations (MANDATORY)

MUST use `world/scripts/product-pr-flow.sh` for the branch → commit → build →
push → PR → checks-watch → merge ceremony. NEVER hand-run the individual git/gh
commands ad hoc — the script enforces the two disciplines that are easy to skip
by hand:

1. **Pre-push build gate** — the repo's full test suite runs BEFORE the push; a
   red suite STOPS the flow (run-full-suite-after-deep-code / post-execution.md
   Step 2b.1).
2. **Checks-gate** — `gh pr checks --watch` blocks until CI concludes; the skill
   never reports "done" on a red or still-running pipeline.

## Preconditions

- The working tree of `--repo` contains ONLY the intended change (stage-clean
  per implementation-discipline — the script runs `git add -A`).
- `gh` is authenticated (`gh auth status` — the script aborts if not).
- This is a PRODUCT-repo change (an `AGENT_WRITE_PATH` sibling repo), NOT the
  Mind framework repo.

## Procedure

Invoke the companion script with the change already made in the working tree.
Resolve `world/` via `_paths.sh` first (the raw `world/...` prefix is not
auto-resolved for Bash args — see `.claude/rules/path-resolution.md`):

    source core/scripts/_paths.sh
    bash "$WORLD_PATH/scripts/product-pr-flow.sh" \
      --repo <abs-path-to-product-repo> \
      --branch-slug <fix|feat|chore>/<short-slug> \
      --title "<PR title>" \
      --body @<path-to-body.md>          # or --body "<inline text>"
      [--base main] [--merge] [--no-build] [--build-cmd "<cmd>"]

### Parameters

| Flag | Required | Purpose |
|------|----------|---------|
| `--repo <path>` | yes | Absolute path to the product repo |
| `--branch-slug <slug>` | yes | Feature-branch name (e.g. `fix/warm-pool-self-heal`) |
| `--title <t>` | yes | PR title (also the commit subject) |
| `--body <text\|@file>` | no | PR body; `@file` reads from a file. Defaults to the title |
| `--base <branch>` | no | PR base branch (default `main`) |
| `--merge` | no | Squash-merge + delete branch + pull `main` when checks are green (default: stop at green checks for a human merge) |
| `--no-build` | no | Skip the pre-push build gate (docs-only PRs only) |
| `--build-cmd "<cmd>"` | no | Override the auto-detected build command |
| `--commit-body <text>` | no | Extra commit-message body above the trailer |
| `--checks-timeout <sec>` | no | Cap on the checks-watch (default 1800) |

The script auto-detects the build command (`gradlew` → `./gradlew test
--no-daemon`; `package.json` → `npm test`; pytest markers → `python3 -m pytest
-q`). When a repo needs special setup — the Ayoai lambda repos need
`PYTHONPATH=/opt/GitHub/Ayoai/ayoai-lambda-common/src` — pass `--build-cmd`.

## Output contract

The script prints ONE JSON line on stdout:

    {"ok":true,"branch":"...","pr_url":"...","pr_number":"14","checks_state":"green","build_state":"passed","committed":true,"merged":false}

- `checks_state`: `green` | `red` | `none` (no CI) | `timeout` | `unknown`
- On any failure it prints `{"ok":false,"stage":"...","error":"..."}` and exits 1.

Parse `ok`; only claim the PR is shipped when `ok=true` AND `checks_state` is
`green` (or `none` for a repo with no CI). A `red` or `timeout` checks_state is
NOT done — surface it and either fix or leave the PR open for review.

## Error handling

| Stage | Failure | Action |
|-------|---------|--------|
| validate | not a git repo / gh not authed | abort (fix auth, re-invoke) |
| build | suite red | STOP — do NOT push; fix the failure, re-invoke |
| push | push rejected | abort; check remote / branch protection |
| pr-create | gh pr create failed | abort; a branch with an open PR is reused automatically |
| checks | red / timeout | do NOT merge; surface state; leave PR open |

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not
text. The terminal action is the Bash call that runs
`world/scripts/product-pr-flow.sh` (and, if invoked mid-loop, the Bash echo
returning control to the orchestrator). Never end with a text summary of the PR.
