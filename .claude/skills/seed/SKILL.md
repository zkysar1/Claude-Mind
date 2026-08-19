---
# domain-leak-exempt: this skill's pseudocode includes verification-grep examples that
# enumerate the EXACT sentinel patterns (Ayoai-Mind, ZakNoCloud, Zachary, AyoAI,
# AYOAI_, X-Ayoai-) the framework wants to find at the destination post-transplant.
# Treating them as patterns to transform would cause double-rename + scanner-self-leak
# (see fresh-eyes review 2026-05-19). file_replace / inline_edit ARE still applied.
name: seed
description: "Manages the framework SEED: build the seed manifest, PLANT (publish) the domain-stripped framework into a destination repo, verify, and diff. The seed is domain-free — planting it grows a NEW, empty environment (e.g. claude-mind) with no agents, no world, no learned state. To relocate a LIVING mind WITH its agents, world, meta and identity onto another machine, that is a TRANSPLANT — use the separate /transplant skill instead. Use when the user says 'seed', 'plant the seed', 'publish the framework', 'publish to claude-mind', 'copy framework to', 'create a seed', 'verify the seed', or 'diff against destination'. Sub-commands: create, plant, verify, diff."
user-invocable: true
triggers:
  - "/seed"
  - "plant the seed"
  - "publish the framework"
  - "create a seed"
  - "publish to claude-mind"
  - "copy framework to"
  - "verify the seed"
  - "diff against destination"
parameters:
  - name: sub-command
    description: "create | plant DEST | verify DEST | diff DEST"
    required: true
  - name: destination
    description: "Absolute path to destination repo (required for plant/verify/diff)"
    required: false
  - name: flags
    description: "--dry-run, --diff, --backup/--no-backup, --force, --manifest <path>, --fresh-git, --commit, --clean-cruft/--no-clean-cruft, --include-bench"
    required: false
tools_used: [Bash, Read, Glob, Grep, Write, Edit]
conventions: [session-state, external-paths]
minimum_mode: assistant
applies_to: any
forged: false
---

# /seed — Framework Seed Management

Manages the framework seed: creates the seed manifest, transplants framework files
to a destination repo (applying domain → generic transformations at copy time),
verifies destination integrity, and diffs source against destination.

The source repo (your working AyoAI deployment, or any domain deployment) stays
UNTOUCHED. Cleanup happens at copy time via transformation rules defined in
`core/config/seed-manifest.yaml`. This separates "your live working environment"
from "the publishable framework abstraction."

> **Naming (2026-06-03):** this skill's copy verb was renamed `transplant` → **`plant`**. A *seed* is domain-free — planting it grows a NEW, empty environment (e.g. claude-mind) with no agents, no world, no learned state. The word **transplant** is now reserved for relocating a *living* mind (its agents + world + meta + identity) to another machine — see the `/transplant` skill.

## Mode Gate

This skill requires `assistant` or `autonomous` mode (creates new files at source
for manifest/skill maintenance; copies framework files to destination).

```
Bash: session-mode-get.sh
IF mode is "reader":
    Bash: echo "Skill requires assistant mode. Run /start --mode assistant to enable."
    STOP.
```

## Step 0: Load Conventions

`Bash: load-conventions.sh session-state external-paths`

Read only the paths returned (files not yet in context). If output is empty,
all conventions already loaded — proceed.

## Sub-Commands

```
/seed create [--manifest <path>] [--dry-run]
/seed plant <destination> [flags]
/seed verify <destination>
/seed diff <destination>
```

### `/seed create` — Build or Update Seed Manifest

For the initial seed, the manifest is hand-curated and committed at
`core/config/seed-manifest.yaml` (Phase 1.5a of publication-cleanup-plan.md).
This sub-command is reserved for MAINTENANCE — when new framework files are
added to source, `/seed create` scans for them and proposes manifest updates.

Step 1: Read existing manifest at `core/config/seed-manifest.yaml` (or `--manifest <path>`).
Step 2: Scan source repo for files added/removed since `manifest.updated` date.
Step 3: Propose include/exclude updates for changed files:
  - New SKILL.md → propose addition to `include[".claude/skills/"]`
  - New convention → propose addition to `include["core/config/conventions/"]`
  - Renamed forged skill → propose update to `exclude_children`
Step 4: If `--dry-run`, print proposed changes; else write updated manifest with
        bumped `updated` date.
Step 5: Report.

(Initial manifest is committed by hand in Phase 1.5; auto-update logic
is a follow-up implementation.)

### `/seed plant <destination>` — Copy Framework to Destination

The primary use case. Copies framework files per manifest from source to
destination, applying transformations at copy time.

**Step 0**: Parse args and flags.
  - Positional arg: destination path (required)
  - Flags: `--dry-run`, `--diff`, `--backup`/`--no-backup` (default ON),
    `--force`, `--manifest <path>`, `--fresh-git`, `--commit`,
    `--clean-cruft`/`--no-clean-cruft` (default ON), `--include-bench`

**Step 1**: Read manifest.
  `Bash: cat core/config/seed-manifest.yaml`
  (or path from `--manifest` flag)

**Step 2**: Verify manifest version compatibility.
  Skill has hardcoded `SKILL_VERSION = 1`.
  If `manifest.min_skill_version > SKILL_VERSION` or
     `manifest.max_skill_version < SKILL_VERSION`: REFUSE with upgrade
     instructions.

**Step 3**: Pre-flight checks.
  a. **Destination exists or can be created.**
     If missing, prompt: "Destination `<path>` does not exist. Create? [y/N]"
     (skip prompt if `--force`)
  b. **Destination git status.** If `.git/` present:
     `Bash: git -C <dest> status --porcelain`
     If non-empty: REFUSE (warn with `--force`).
  c. **Destination session state.** If `<dest>/agents/*/session/agent-state`
     contains "RUNNING" OR `<dest>/.active-agent-*` present:
     REFUSE: "An agent is running at destination. /stop it first." NO OVERRIDE.
  d. **Domain dirs at destination.** If `<dest>/agents/`, `<dest>/world/`, or
     `<dest>/meta/` exist: INFO ("will NOT be touched").
  e. **Source cleanliness.** `Bash: git status --porcelain` in source.
     If non-empty: WARN ("source has uncommitted changes — seed will include
     uncommitted modifications"). Proceed.

**Step 4**: Build copy plan.
  `Bash: py -3 core/scripts/_seed_engine.py build-plan --manifest <path>`
  Returns JSON: `{files: [...], transformations: [...], excludes: [...]}`
  Each file annotated with applicable transformations
  (file_replace > inline_edit > global_regex > word_list_strip).

**Step 5**: Dry-run preview (mandatory unless `--force`).
  Print:
  - "N files to copy"
  - "K transformations to apply (broken down by type)"
  - "M files to backup at destination"
  - List of pending_template files, BY NAME. These are NOT skipped: a
    `pending_template: true` file_replace is DROPPED and the file falls through
    to the ordinary inline_edit + global_regex + word_list_strip chain
    (`_seed_transforms.py:470`). The WARN says which files took that fallback so
    a dangling template pointer cannot read as "this file does not promote"
    (g-115-3474 — that misreading is exactly what a count-only WARN produced).
  If `--dry-run`: STOP here. Print exit message "Dry run complete. Re-run
  without --dry-run to apply."

**Step 6**: User confirmation gate (skip if `--force` or `--dry-run`).
  Print summary, ask: "Proceed with transplant to `<dest>`? [y/N]"

**Step 7**: Backup destination (default ON; `--no-backup` to skip).
  `Bash: py -3 core/scripts/_seed_engine.py backup --dest <dest> --manifest <path>`
  Copies manifest-included framework files at destination to
  `<dest>/.seed-backup-<ISO-timestamp>/`.

**Step 8**: Staged copy.
  `Bash: py -3 core/scripts/_seed_engine.py copy-staged --source <src> --dest <dest> --manifest <path>`
  - Copies files to `<dest>/.seed-staging/`
  - Applies transformations to each staged file (via `_seed_transforms.py`)
  - On any failure: deletes staging dir, reports which file failed with rollback message

**Step 9**: Atomic swap.
  `Bash: py -3 core/scripts/_seed_engine.py swap --dest <dest>`
  - Moves files from `<dest>/.seed-staging/` to `<dest>/`
  - Removes staging dir

**Step 10**: Clean cruft (default ON; `--no-clean-cruft` to skip).
  `Bash: py -3 core/scripts/_seed_engine.py clean-cruft --dest <dest> --manifest <path>`
  For each pattern in `manifest.cruft_patterns`, removes matching files at destination.

**Step 11**: Handle git.
  - If `--fresh-git`: `Bash: rm -rf <dest>/.git && git -C <dest> init`
  - Else: preserve existing `.git/`.
  - If `--commit`: `Bash: git -C <dest> add -A && git -C <dest> commit -m "Seed transplant from <source> at <ISO-timestamp>" -m "size-budget-override: framework seed plant — hot-path files track the upstream versions"` (the second `-m` is the g-115-6470 hot-path size-budget trailer: a plant may legitimately grow the destination's hot-path files to the upstream sizes, and its `commit-msg` hook refuses growth without it)

**Step 12**: Run post-copy actions.
  For each entry in `manifest.post_copy_actions`:
  - `type: command` → execute via Bash at `run_at` location (source or destination)
  - `type: message` → print to user
  - On failure: respect `on_failure` (warn or fail)

**Step 13**: Run verification.
  Invoke `/seed verify <destination>` (sub-command below).

**Step 14**: Report.
  Print structured PASS/FAIL/WARN summary with per-step detail.
  Final Bash echo (return-protocol terminal action).

### `/seed verify <destination>` — Post-Transplant Smoke Test

Runs 8-check verification on an existing destination. No copy.

**Step 1**: Manifest completeness.
  `Bash: py -3 core/scripts/_seed_engine.py verify-completeness --dest <dest> --manifest <path>`
  For each include entry, confirm file/dir exists at destination. PASS/FAIL per entry.

**Step 2**: Domain leakage.
  `Bash: ls <dest>/agents <dest>/world <dest>/meta 2>/dev/null`
  Confirm these dirs do NOT exist (unless pre-existing — info-only in that case).
  Confirm no `exclude_always` paths were accidentally copied.

**Step 3**: Cruft absence.
  For each `cruft_patterns` entry, confirm no match at destination.

**Step 4**: Git state report.
  `Bash: git -C <dest> status --porcelain && git -C <dest> branch --show-current && git -C <dest> log --oneline -1 && git -C <dest> remote -v`
  Report: branch, clean/dirty, last commit, remote (if any).
  If `--fresh-git` was used: confirm only 1 commit (the init).
  Report-only for THIS verb, but not universally: `seed-verify.sh --expect-commit`
  (passed by the plant + promote paths, never by this standalone verb) makes a
  dirty tree a hard FAIL instead. Post-plant a dirty destination IS a failed
  plant, and reporting it leniently there is what let an empty promotion open a
  PR under a PROMOTED banner (g-115-3481). Keep the two modes distinct — do not
  "simplify" this step back to lenient-everywhere.

**Step 5**: Sample integrity (SHA-256).
  For 5-10 key files (`CLAUDE.md`, `core/scripts/_paths.sh`, `.claude/settings.json`,
  `core/config/tree.yaml`, `mind_api/src/server.py`):
  - Compute SHA-256 at source AND at destination
  - Apply manifest transformations to source content first (so comparison is post-transform)
  - Report match/mismatch per file

**Step 6**: Framework bootability.
  `Bash: bash <dest>/core/scripts/check-prerequisites.sh`
  Confirms Python 3.10+, PyYAML, bash 4+ at destination.

**Step 7**: Path self-reference scan.
  `Bash: grep -rn "Ayoai-Mind\|ZakNoCloud\|Zachary" <dest>/.claude/settings.json <dest>/CLAUDE.md <dest>/README.md`
  Any hit → WARN.

**Step 8**: Report (structured PASS/FAIL/WARN summary).
  Output: counts + per-item detail. Final Bash echo.

### `/seed diff <destination>` — Compare Source vs Destination

**Step 1**: Read manifest.

**Step 2**: For each include entry, diff source vs destination (after applying
            transformations conceptually):
  `Bash: py -3 core/scripts/_seed_engine.py diff --source <src> --dest <dest> --manifest <path>`

**Step 3**: Report:
  - New files at destination (not in source manifest)
  - Modified files (post-transform-aware diff)
  - Deleted files (in source manifest, missing at destination)
  - Identical files (count only)

## Constraints

- NEVER copy (plant) `.env.local`, `agents/`, `world/`, `meta/` (enforced by `exclude_always`)
- NEVER overwrite a running agent session at destination (Step 3c, NO override)
- Always backup by default (`--backup` ON)
- Always clean cruft by default (`--clean-cruft` ON)
- Manifest is hand-curated, not auto-generated for production use
- Source repo is NOT cleaned — transformations apply at copy time only
- Skill version must match manifest's `min_skill_version`/`max_skill_version` range

## Implementation

The skill body is implemented in:

- `core/scripts/seed-transplant.sh` — top-level orchestrator for `/seed plant` (filename keeps `transplant` to avoid churning the CLAUDE.md AGENTS_PARENT_DIR audit table + tests; calls helpers below)
- `core/scripts/seed-create.sh` — manifest create/update sub-command
- `core/scripts/seed-verify.sh` — post-transplant verification
- `core/scripts/seed-diff.sh` — source-vs-destination diff
- `core/scripts/_seed_engine.py` — manifest parsing, build-plan, staging, swap, backup, cruft-clean
- `core/scripts/_seed_transforms.py` — transformation engine: global_regex, inline_edit, file_replace, word_list_strip
- `core/scripts/seed-path-self-reference-scan.sh` — Step 7 verification helper
- `core/scripts/skill-tree-restructure.py` — post-copy action A1 (regenerate _tree.yaml without forged skills)

Implementation lives in Phase 1.5b (after this spec is reviewed). The implementations
must NOT modify source content — they only READ source and WRITE destination.

## Chaining

- **Called by**: User (`/seed <sub-command>`)
- **Calls**: `bash core/scripts/seed-*.sh` wrappers + their Python helpers
- **Does NOT call**: `init-mind.sh`, `init-world.sh`, `init-agent.sh` — those run on `/start` at destination
- **Does NOT modify**: source repo content (read-only on source side)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
Terminal action for each sub-command:
- `/seed create`: final Bash echo summarizing manifest changes
- `/seed plant`: final Bash echo summarizing transplant outcome + verification result
- `/seed verify`: final Bash echo with PASS/FAIL/WARN summary
- `/seed diff`: final Bash echo with diff summary

NEVER end a sub-command with text output. The terminal tool call IS the report.
