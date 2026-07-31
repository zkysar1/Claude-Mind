---
name: drain-temp
description: "Drains the agent's temp/ working-doc store into the knowledge tree. Walks every undrained file under agents/{agent}/temp/ (analyses, briefings, audits, design docs, snapshots), classifies each against the learning-routing decision tree, encodes its reusable value into the right store (knowledge tree / reasoning bank / guardrails / experience), then moves the file to temp/drained/ as an audit trail. Also PURGES stale pure-ephemera files (.log/.txt/.py/.sh/.err/.raw/.out/.bak test-suite output, tool dumps, one-shot scratch scripts, raw command-output dumps, backups) plus 0-byte empties of any name that carry no knowledge — deleting them rather than encoding (Phase 1.5, 120-min age guard). Use when the user says \"drain temp\", \"encode everything in temp\", \"clear out temp\", or when the aspirations-precheck temp-pressure check flags temp_drain_needed (>= drain threshold of undrained docs + ephemera). Pass --dry-run to list what WOULD drain or purge without encoding, moving, or deleting."
user-invocable: true
triggers:
  - "/drain-temp"
  - "drain temp"
  - "encode everything in temp"
parameters:
  - name: dry-run
    description: "List undrained files + proposed routing without encoding or moving anything"
    required: false
  - name: file
    description: "Drain a single named temp/ file instead of the whole store (e.g. --file design-2026-06-02.md)"
    required: false
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [temp-store, learning-routing, tree-retrieval, reasoning-guardrails, experience]
minimum_mode: assistant
revision_id: "skill-bootstrap-drain-temp-f10501"
previous_revision_id: null
---

# /drain-temp — Temp Store Drain Engine

Encodes the value out of `agents/<agent>/temp/` into the one long-term retrieval
surface (the knowledge tree + supplementary stores), then archives each processed
file to `temp/drained/`. This is the mechanism that makes `temp/` a STAGING area
rather than a slush directory: every working doc either drains into knowledge or
is discarded — nothing lives in `temp/` permanently.

**Hybrid skill**: the user invokes it ("drain temp", "encode everything in temp");
the aspirations loop invokes it when `aspirations-precheck` flags
`temp_drain_needed` (temp/ accumulated past the drain threshold). Writes to the
tree / reasoning bank / guardrails — requires assistant or autonomous mode.

## Why this exists

A working document written to `temp/` is in none of the retrieval stores until it
is drained — invisible to `/prime` and `retrieve.sh`. Draining is how that knowledge
becomes durable and findable. See `core/config/conventions/temp-store.md`.

## Phase 0: Load Routing Context

```
Bash: load-conventions.sh temp-store learning-routing
→ Read the returned paths not already in context. learning-routing.md carries the
  "where does this learning go?" decision tree used in Phase 2 classification.
```

## Phase 1: Enumerate Undrained Files

```
1. Resolve the bound agent's temp dir via the canonical path helper (never
   hardcode an agent name — _paths.sh exports AGENT_DIR for $MIND_AGENT):
   Bash: source core/scripts/_paths.sh; TEMP_DIR="$AGENT_DIR/temp"; echo "$TEMP_DIR"
2. List undrained working docs — files DIRECTLY under temp/ (NOT under drained/):
   Bash: ls -1 "$TEMP_DIR"/*.md "$TEMP_DIR"/*.json 2>/dev/null
   (drained/ is the archive subdir — never re-drain it; the glob above does not
   descend into it.)
3. IF no drainable files (.md/.json): set docs_count=0 and skip Phase 2-3, but
   STILL run Phase 1.5 — pure ephemera (.log/.txt) may need purging even when no
   docs remain (the g-115-1727 case: 7 ephemera survived a full doc-drain).
   Report "temp/ is clean" + DONE only if Phase 1.5 ALSO purges nothing.
4. IF --file <name> given: restrict the list to that one file under "$TEMP_DIR"
   and SKIP Phase 1.5 (single-file drain is a targeted op, not a full sweep).
5. Sort oldest-first (timestamped filenames sort lexically = chronologically).
```

`$AGENT_DIR` resolves to `agents/<bound-agent>/` via `_paths.sh` (`agent_dir()` is
the documented helper). Never hardcode an agent name and never derive the path
from world/ or meta/ (see `.claude/rules/path-resolution.md`).

## Phase 1.5: Purge Pure Ephemera (.log/.txt/.py/.sh/.err/.raw/.out/.bak + 0-byte empties)

temp/ also collects PURE-EPHEMERA files that carry no knowledge — test-suite
output (`suite-*.log`), tool dumps (`leak-check.txt`), one-shot scratch
scripts (`build-*.py`, `orphan-*.py`, `restart-poller.sh`, `gs.err`), raw
command-output dumps (`selector.raw`, `probe.out` — stdout redirects), backup
copies (`*.bak`), and 0-byte empties left by an interrupted redirect. These are
NOT drainable working docs: the framework's own guidance writes them here (see
`.claude/rules/run-full-suite-after-deep-code.md` — "redirect to
`agents/<agent>/temp/suite.log`"), but they have nothing to encode. Left alone
they accumulate indefinitely — the slush-directory failure mode for a file class
Phase 1's `.md`/`.json` glob deliberately never touches (g-115-1727, g-115-2947).

Raw command-output dumps ideally go in `session/scratch/` (their proper
ephemeral home); when convenience lands one in `temp/`, give it a `.raw`/`.out`
extension so THIS phase purges it — a bare-named `.json` dump is instead
enumerated by Phase 1 as a drainable working doc and archived to `drained/`,
bloating the audit trail with valueless scratch (see
`core/config/conventions/temp-store.md` → "Raw command-output dumps").

Purge them — DELETE, not archive. `agents/*/temp/` (including `drained/`) is
gitignored (guard-872), so archiving ephemera to `drained/` would only relocate
untracked slush; deletion is correct and loses no history (there is none to
lose). Discard-only: no encode, no `drained/` move.

```
SKIP this phase entirely when invoked with --file (targeted single-doc drain).

# Purge stale ephemera via the CANONICAL GUARDED helper — NEVER hand-roll an
# `rm` (or the find/rm inline) here. `temp-drain-purge.sh` asserts the temp dir
# is set + non-empty, absolute, strictly under PROJECT_ROOT, and basename=='temp'
# BEFORE any deletion, then runs THREE guarded lanes (g-115-2948), NONE using a
# per-file `rm` on an interpolated path:
#   Lane 1 — purge ephemera FILES: `find … -maxdepth 1 -type f (ephemera globs)
#     -mmin +120 -delete` (leaves drained/ untouched; the 120-min age guard skips
#     an actively-written suite.log from an in-flight run).
#   Lane 2 — GC stale drained/ FILES: `-mtime +30` (the drained/ dir itself is
#     preserved; --drained-age-days overrides the 30-day default).
#   Lane 3 — remove abandoned stray subdirs: any dir under temp/ that is NOT
#     drained/ and is untouched past --age-min, via a bounded `find "$stray" -delete`.
#     EXCEPTION (g-115-2962): a stray dir carrying a top-level RECEIPT.md or a
#     .archive-marker sentinel is an archive-before-delete recovery layer and is
#     PRESERVED (reported on stderr, excluded from .stray_purged) — never
#     destroyed as a drain side-effect (archive-before-delete.md).
#
# WHY the helper and NOT an inline rm: hand-rolling an unguarded
# `rm -f "$TEMP_DIR/$f"` here — when $TEMP_DIR resolves empty — becomes an `rm`
# on a root-relative path, which Claude Code flags as a dangerous-rm and PROMPTS
# for confirmation EVEN under --dangerously-skip-permissions. An autonomous agent
# cannot answer its own dialog, so the loop looks alive (state=RUNNING) but hangs
# at zero progress until a human taps a button — an agent hung 46+ min this way
# (g-115-1876). Do NOT reconstruct the find/rm inline; call the helper.
Bash: bash core/scripts/temp-drain-purge.sh          # add --dry-run when the drain is --dry-run
  → parse the JSON: purged_count = .purged; ephemera names = .files; drained GC
    count = .drained_gc_purged; stray-dir count = .stray_purged (surface all
    three in the Phase 4 report).
IF --dry-run: invoke with --dry-run (all *_purged=0; .would_purge /
    .drained_gc_would_purge / .stray_would_purge list what WOULD go); DELETE NOTHING.
IF every count is 0 (.purged, .would_purge, .drained_gc_purged, .drained_gc_would_purge,
    .stray_purged, .stray_would_purge): nothing to purge/GC/clean; continue.
```

Ephemera NEWER than the age guard are left in place (a running suite's log); the
temp-pressure metric still counts them (no age guard on the count — see
`core/scripts/precheck-eval.py` `cmd_temp_pressure`), so they resurface for the
next drain once stale.

## Phase 2: Classify and Encode Each File

For each undrained file (oldest first):

```
1. Read the file (full content).

2. Retrieve before encoding (retrieve-before-deciding.md): run
   Bash: retrieve.sh --category "<one-line summary of the doc's topic>" --depth shallow
   to (a) find the target tree node and (b) detect content already encoded
   (avoid duplicate encoding).

3. Classify the content against learning-routing.md and route to ONE primary store
   (a single doc MAY also produce a secondary encoding — e.g. tree node + guardrail):

   | Content shape | Store | How |
   |---|---|---|
   | Reusable domain knowledge / facts / patterns | Knowledge tree | /tree add (or Edit an existing node surfaced by step 2) |
   | Time-anchored lesson / "this failed because X" | Reasoning bank | reasoning-bank-add.sh |
   | A behavioral rule / user correction the agent must obey | Guardrails | guardrails-add.sh |
   | Narrative of what happened during a goal/session | Experience | experience archive (experience-add path) |
   | Stable operational value (path, endpoint, ID) | world/conventions/<kind>.md | Edit the locator convention (encode-stable-facts.md) |
   | Already fully encoded (step 2 found it) OR pure ephemera / superseded scratch | DISCARD | no encoding — just archive in Phase 3 |

   DISCARD is a first-class outcome — do not force-encode junk into the tree.
   Record WHY (already-encoded / superseded / ephemeral) for the Phase 4 report.

4. Knowledge reconciliation: after a tree encoding, update the node's
   last_updated + last_update_trigger (knowledge-freshness.md).

5. In --dry-run: do NOT encode. Record the proposed store + target only.
```

## Phase 3: Archive the Drained File

```
For each file processed in Phase 2 (NOT in --dry-run):
  Bash: mkdir -p "$TEMP_DIR/drained" && git mv "$TEMP_DIR/<file>" "$TEMP_DIR/drained/<file>" \
        2>/dev/null || mv "$TEMP_DIR/<file>" "$TEMP_DIR/drained/<file>"
  (Shell mv/git mv bypasses the Write/Edit allowlist gate by construction — the
   gate is a PreToolUse[Write|Edit] hook, not a filesystem lock. drained/ is the
   sanctioned archive subdir; git mv keeps history when the file is tracked.)
```

In `--dry-run`, skip this phase entirely.

## Phase 4: Report + Journal

```
1. Emit a summary:
   ═══ TEMP DRAIN ════════════════════════════════
   Drained: {N} file(s)
     {file} -> {store}:{target}   (or DISCARD: {reason})
     ...
   Purged (ephemera + 0-byte empties): {purged_count} file(s)   (omit line if 0)
     {ephemera-file names, from Phase 1.5}
   Discarded: {M}    Remaining undrained: {0 unless --file}
   ═══════════════════════════════════════════════

2. IF anything was encoded (not dry-run): append a journal entry recording the
   drain (journal-append.sh) so the audit trail names what moved from temp/ to
   knowledge this session.

3. IF this was invoked by the loop on temp_drain_needed: the drain goal is
   satisfied once temp/ is below threshold — the next precheck recomputes the count.
```

## Invocation Rules

- Never re-drain files already under `temp/drained/`.
- Never hardcode an agent name — operate on the bound agent (`$MIND_AGENT`).
- DISCARD is valid — encoding is not mandatory for every file; already-encoded
  and ephemeral content is archived without a duplicate tree write.
- `--dry-run` has no side effects (no encode, no move) — safe in any mode.
- Respect retrieve-before-deciding: retrieve before each encode to route correctly
  and avoid duplicates.

## Chaining

- **Called by**: the user ("drain temp"); `/aspirations` loop when
  `aspirations-precheck` emits `temp_drain_needed`.
- **Calls**: `retrieve.sh`, `/tree add`, `reasoning-bank-add.sh`,
  `guardrails-add.sh`, the experience archive path, `journal-append.sh`.
- **Does NOT call**: `/start`, `/stop`, `/aspirations`.

## Return Protocol

See `.claude/rules/return-protocol.md` — the last action MUST be a tool call, not
text. The terminal action is the Phase 4 `journal-append.sh` call (or, in
`--dry-run` / nothing-to-drain, a final `Bash: echo` handing control back). When
invoked from the loop, never end with a text summary — control returns to the
orchestrator.
