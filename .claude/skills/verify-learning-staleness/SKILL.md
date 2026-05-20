---
name: verify-learning-staleness
description: "Scans verify-learning/SKILL.md for stale Check:/Bash: assertions whose targets (paths, phase headers, grep patterns) no longer exist in the codebase. Four lanes: L1 path references, L2 Phase/Step references inside SKILL.md targets, L3 grep-target patterns, L3b `Grep Phase X for `pattern`` variant. Skips negative assertions (\"MUST NOT exist\", \"test ! -f\", \"absent\") and external paths (meta/, world/). Use whenever the agent suspects verification-checklist drift — after major SKILL.md refactors, after extracting inline pseudocode into scripts, after renaming phases, OR via the recurring goal under asp-115 (g-115-219) that fires weekly. Sister to felt-sense-checkin Phase 5b (which calls the same scanner at 75-goal cadence) — this skill is the on-demand entry point."
user-invocable: false
triggers: [verify-learning-staleness, staleness-scan, check-drift, audit-verify-learning, stale-checks, verify-learning-audit]
tools_used: [Bash]
companion_scripts: [core/scripts/verify-learning-staleness.py]
conventions: []
minimum_mode: assistant
revision_id: "skill-bootstrap-verify-learning-staleness-1a0562"
previous_revision_id: null
---

# /verify-learning-staleness — Verify-Learning Staleness Scanner

Detect stale Check: and Bash: assertions in `verify-learning/SKILL.md`
whose underlying targets (paths, phase headers, grep patterns) have been
moved, renamed, or extracted out of the codebase without the check being
updated.

## Why This Exists

`verify-learning/SKILL.md` accumulates ~2000 verification assertions
over time. Each assertion references a concrete artifact: a script
path, a SKILL.md phase number, a grep pattern in a named file. When
refactors happen — extracting inline pseudocode into Python, renumbering
phases, deleting retired scripts — the verification check goes stale
silently. Future `/verify-learning` runs report PASS for stale checks
because the assertion can't even be evaluated against current code,
producing false confidence.

This skill is the structural complement: it scans every assertion and
reports which ones reference artifacts the codebase no longer contains.

## Companion Script

- `core/scripts/verify-learning-staleness.py` — the canonical scanner.
  Modes:
  - Default (no flag) — JSON to stdout, exit 0 if clean / exit 1 if stale.
  - `--text` — human summary on stdout, JSON to stderr.
  - `--skill-md <path>` — override the target file (default: framework path).

## Procedure

1. `Bash: py -3 core/scripts/verify-learning-staleness.py --text` —
   run the scanner. Output lists each stale finding as
   `[L<lane>] line N: <stale_ref>` with detail explaining why the
   target couldn't be resolved.

2. For each finding, decide the disposition:
   - **Relocated** — the underlying contract still exists, but moved to
     a different file/phase/script. Update the Check: text to reference
     the new location.
   - **Retired** — the contract was removed entirely. Delete the Check:
     line.
   - **Future-tense** — the check was added speculatively for upcoming
     work. Leave alone if work is genuinely imminent, otherwise convert
     to a comment or delete.

3. After applying fixes, re-run the scanner — `stale_count` should
   ratchet down (or stay constant only with documented future-tense
   exemptions).

## Lane Reference

| Lane | What it detects |
|---|---|
| `L1_path` | Framework path mentioned in a Check:/Bash: line that doesn't exist. Excludes `meta/` and `world/` (external paths per local-paths.conf). |
| `L2_phase` | `Phase X` or `Step X` referenced next to a SKILL.md path, where that phase header doesn't appear in the named file. |
| `L3_grep` | `Grep <FILE> for \`pattern\`` where pattern produces 0 matches in FILE. |
| `L3b_grep_phase` | `Grep Phase X for \`pattern\`` idiom — pattern absent from the SKILL.md mentioned earlier in the same line. |

## Negative-Assertion Guard

Checks that EXPECT artifacts to be missing (e.g., `test ! -f X`,
`X does NOT exist`, `Phase X absent`, `MUST NOT contain Y`) are
exempt from staleness reporting. The negative-phrase list lives in
`_NEGATIVE_PHRASES` in the scanner script — extend there if a new
negative idiom appears.

## Expected Output

Clean state:
```
verify-learning-staleness: scanned <N> assertions, found 0 stale
```

Drift state:
```
verify-learning-staleness: scanned <N> assertions, found <M> stale
  [L2_phase] line 162: Phase 0.5.0a in .claude/skills/aspirations-precheck/SKILL.md
    Phase '0.5.0a' not found in ... — phase/step was renumbered, removed, or extracted
  [L1_path] line 3892: core/scripts/validate-loop-digest.sh
    path 'core/scripts/validate-loop-digest.sh' does not exist
  ...
```

## Failure Modes

- **Scanner returns 0 stale but verify-learning still drifts** — the
  scanner regex didn't catch the pattern. File an Idea goal to extend
  detection (e.g., add a new lane or loosen `_PATH_RE`).
- **Scanner returns false positives** — likely a new negative idiom not
  in `_NEGATIVE_PHRASES`. Add the phrase, re-run.
- **Scanner can't find a referenced grep target** — an L3 finding for
  `grep target 'X' could not be resolved`. Either the basename collides
  with multiple files (rare, scanner takes first match) or the file
  truly doesn't exist (true positive).

## Chaining

- **Called by**:
  - The aspirations loop via recurring goal `g-115-219` under
    `asp-115 Recurring Infrastructure Monitoring` (interval 168h /
    weekly). Pulls drift detection into the regular hygiene rotation.
  - `felt-sense-checkin` Phase 5b at the 75-completed-goals cadence.
  - The agent on-demand whenever it suspects drift — e.g., right after
    editing `verify-learning/SKILL.md`, after a SKILL.md extraction
    refactor, or when a `/verify-learning` run reports suspicious 100%
    PASS.
- **Calls**: `core/scripts/verify-learning-staleness.py`.
- **Does NOT call**: `/verify-learning` itself (that's a much heavier
  diagnostic — this skill is the structural pre-check, not a replacement).
- **Modifies**: nothing. This skill is read-only. Stale-check repair is
  done via the standard Edit tool against `verify-learning/SKILL.md`
  after the scan reports findings.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool
call, not text. The terminal Bash call is the scanner invocation;
never end the skill with a text summary paragraph.
