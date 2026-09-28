# Rationale: consolidate Step 9 phase-cost handoff path

Cited by: `.claude/skills/aspirations-consolidate/SKILL.md` Step 9 (Write
Continuation Handoff), the `last-phase-cost.json` writer and reader lines.

## The defect (g-115-9954, measured 2026-09-14 foxtrot; re-measured 2026-09-26)

Step 9 redirected `phase-cost-report.sh --write-report` to
`"$MIND_AGENT/session/last-phase-cost.json"` and read the same path back.
With `AGENTS_PARENT_DIR=agents` (the current layout) `$MIND_AGENT` expands to
the bare agent name, so the target is `PROJECT_ROOT/<name>/session/` — a dir
that does not exist. Consequences, measured:

1. **The redirect fails and bash never executes the command** — a failed
   output redirect means `phase-cost-report.sh` never runs at session end on
   any current-layout box (positive control: `touch MARKER > no-such-dir/f`
   leaves no MARKER). So BOTH `last-phase-cost.json` AND the persisted report
   under `agents/<name>/session/phase-costs/` are lost — the persisted report
   is written by the script itself, which never got the chance to run.
2. The `|| echo '{}'` fallback wrote to the identical bad path, so it failed
   too. The reader then found no file and printed `skipped (no markers)` —
   the exact benign-looking symptom the two-step layout was introduced to
   fix, persisting for a different reason.
3. Invisible to the existing audits: the four audit greps in
   `core/config/conventions/agent-dir-resolution.md` scan only `core/scripts/`
   and `mind_api/`, never `.claude/skills/` or `core/config/`, and none matches
   the bare agent-name-relative shell form.

## The fix (2026-09-27, g-115-9954)

- Both lines now `source core/scripts/_paths.sh` and resolve the path via
  `$(agent_dir "$MIND_AGENT")/session/last-phase-cost.json`, per CLAUDE.md's
  "never write PROJECT_ROOT/<agent> or $PROJECT_ROOT/$AGENT directly". Writer
  and reader carry the same expression and changed together.
- The reader keeps the inline-env-var prefix style (`VAR="..." py -3 -c`)
  that iteration-close.sh uses throughout (`GID="$GOAL_ID" ... python3 -c` at
  e.g. L1055/L1106/L1779) — robust against MSYS pipe issues. The old comment
  citation `iteration-close.sh:114` was STALE: that line is a `set -euo
  pipefail`-era comment block; there is no phase-cost call in iteration-close.sh
  at all (phase markers are emitted by `_emit_marker` near L4864 instead).
  The stale citation and the stale `session-manifest.yaml` writer pointer
  (`SKILL.md:591`) were corrected in the same change.
- `agent-dir-resolution.md`'s audit list grew two greps (now "ALL SIX"):
  `(^|[^A-Za-z0-9_/])\$\{?MIND_AGENT\}?/` (bash form) and
  `MIND_AGENT'\)+'/` (python form), both over `.claude/skills/ core/config/
  core/scripts/ mind_api/src/` — the scopes the original four never scanned.

## Verification (g-115-9954, this box)

- Ran both Step 9 lines verbatim from PROJECT_ROOT with only `MIND_AGENT`
  set: `last-phase-cost.json` (2173 B) landed under
  `agents/alpha/session/`, a real persisted report was written
  (`phase-costs/20260927T065632.json`), and the reader printed a real
  `written_to` path plus `5 phases, 5 pairs` — not `none`/`skipped`.
- Both audit greps return 0 sites across the four scopes.
