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
# BEFORE any deletion, then runs THREE guarded DELETION lanes (g-115-2948), NONE
# using a per-file `rm` on an interpolated path — plus a report-only Lane 0 that
# deletes nothing (listed last below because it was added last, g-115-3397):
#   Lane 1 — purge ephemera FILES: `find … -maxdepth 1 -type f (ephemera globs)
#     -mmin +120 -delete` (leaves drained/ untouched; the 120-min age guard skips
#     an actively-written suite.log from an in-flight run).
#   Lane 2 — GC stale drained/ FILES: `-mtime +30` (the drained/ dir itself is
#     preserved; --drained-age-days overrides the 30-day default).
#   Lane 3 — remove abandoned stray subdirs: any dir under temp/ that is NOT
#     drained/ and is untouched past --age-min, via a bounded `find "$stray" -delete`.
#     EXCEPTION (g-115-2962): a stray dir carrying a top-level RECEIPT.* or a
#     .archive-marker sentinel is an archive-before-delete recovery layer and is
#     PRESERVED (reported on stderr, excluded from .stray_purged) — never
#     destroyed as a drain side-effect (archive-before-delete.md). The receipt
#     match is extension- and case-INSENSITIVE as of g-115-3397: it required
#     `RECEIPT.md` exactly until 2026-08-08, a name zero producers write
#     (_seed_engine.py writes RECEIPT.json, history_vacuum_archive.py writes
#     lowercase receipt.json), so the guard was unreachable by every archive the
#     framework creates. SSOT predicate: `_has_archive_receipt`.
#   Lane 0 — REPORT-ONLY, deletes nothing (g-115-3397). Hidden dotfiles directly
#     under temp/ are matched by NO lane: Phase 1 below enumerates temp/*.md and
#     temp/*.json (globs that cannot match a leading dot) and Lane 1 exempts
#     `! -name '.*'`, so a dotfile is never drained, never purged, and never
#     counted by the temp-pressure metric — permanent invisible residue, and the
#     originating case was a 221-byte .launch-payload.json holding an api_key and
#     three other secrets. This lane makes it VISIBLE without adding a way to
#     destroy live state: purging is the wrong correction because the exemption
#     protects the git-tracked .gitkeep, and the live population is working
#     cadence state. Allowlist: .gitkeep, .archive-marker, .drain-watermark
#     (the Phase 2.5 marker — managed, not residue).
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
    three in the Phase 4 report). ALSO surface .unmanaged_dotfiles (Lane 0) and
    .unmanaged_dotfile_names when the count is non-zero — those files were NOT
    touched and will still be there next drain, so an unreported non-zero count
    is the invisible-residue defect returning. It is identical under --dry-run
    (Lane 0 never deletes), so do NOT read a zero there as "nothing to see".
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

3b. BEFORE committing to a DISCARD verdict, run the scripted encode probe.
   DISCARD is the one branch whose wrong call is silent and effectively
   irreversible — the doc lands in temp/drained/ and nothing re-examines it —
   and it was the only branch resting entirely on LLM judgement (g-115-3089).

   Bash: py -3 core/scripts/drain-encode-probe.py <file> --json

   The probe infers the artifact's TYPE from its field shape, then probes the
   store for the EFFECT the payload would have had — not merely for something
   with a matching name. Act on the verdict:

   | verdict | meaning | required action |
   |---|---|---|
   | `absent`  | the payload's effect is NOT in the store — unapplied work | **DISCARD IS BLOCKED.** Verify store-wide first (below), THEN encode or leave undrained. Never archive. |
   | `encoded` | the effect is present in the store | DISCARD is safe to proceed |
   | `unknown` | shape unrecognised / store unreadable / effect not observable | fall back to LLM judgement, exactly as before |

   `unknown` is the common case for query-output captures and command scratch,
   which is correct — the probe narrows the judgement call, it does not replace
   it. Only `absent` constrains you, and only in the safe direction.

   `absent` IS NOT AN INSTRUCTION TO ENCODE — IT IS A PROMPT TO CHECK, AND THE
   TWO DIRECTIONS FAIL DIFFERENTLY. It fails SAFE for discard (over-retaining
   costs disk) and UNSAFE for encoding (a duplicate is noisy; a re-entered
   REFUTED claim is corrosive). The wording above led with encode until
   2026-08-13 and that ordering is what nearly landed one. Measured that day
   (bravo, `hostname` cc-05, `uname -r` 6.8.0-137-generic, g-001-343) on the 5
   absent artifacts of type `guardrail` + `reasoning_bank` — the probe's OWN
   native shapes, where its predicate is best tested and where NEITHER open
   blocker (g-115-5372 `.json`-metric half, g-115-5979 `trace_md` half) applies:
   **3 of 5 were ALREADY PRESENT** in `guardrails.jsonl`; **1 was genuinely
   absent and WRONG** (`rb26` claimed a recurring-starvation coverage gap that
   `rb-7403` already records as investigated and PHANTOM — different scales);
   1 was absent and new. Actionable as stated on 1 of 5. So over-reporting is
   NOT confined to the two owned shapes, and a fix scoped to them leaves the
   class alive everywhere else.

   So on `absent`, before encoding: run a STORE-WIDE existence probe (not a
   category-scoped one — `guardrails-read.sh --active`, `reasoning-bank-read.sh
   --active`; a bare call errors `at least one filter is required`), assert a
   POSITIVE CONTROL that the corpus actually loaded, then check the result for
   semantic overlap AND contradiction. The control is not optional ceremony:
   the first store-wide probe of that measurement returned rc=1/empty and
   rendered as "CONFIRMED ABSENT store-wide" — searching an empty blob always
   misses, so an unreadable store manufactures the exact verdict that licenses
   the write (the `learning-routing-audit` class in CLAUDE.md, where an empty
   id-set silently licensed 17,466 nullings). Encoded: `rb-7698`.

   FAIL-OPEN: the probe never emits `absent` from an internal error (every
   failure path returns `unknown`) and always exits 0. A probe bug therefore
   restores today's behaviour rather than wedging the drain lane — arming an
   inert checker would invert the harm rather than fix it. Do NOT gate a drain
   on the probe's exit code; read the verdict.

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

2.5. ADVANCE THE THIRD-CLASS WATERMARK — full pass only, never --dry-run,
   never --file (encode-before-delete gate, 2026-08-21), AND ONLY WHEN THE
   DRY-RUN PROVES IT LICENSES NOTHING YOU DID NOT CLASSIFY (guard-4864,
   2026-08-22). THE DEFAULT IS NOT TO STAMP.
   `would_purge` > 0 means Lane 1 is condemning THIRD-CLASS suffixes
   (.jsonl/.note/.patch/.desc/.eml/.tsv/.remote/.local-preserve-*) — and
   Phase 1's census is `ls temp/*.md temp/*.json`, so your pass did NOT
   enumerate a single one of them. Stamp ONLY when it is 0 AFTER the stamp,
   or when you have just extended the census to cover those suffixes and
   classified each file individually.

   ⛔ READ `would_purge` AFTER STAMPING, NOT BEFORE. A pre-stamp read is taken
   under the OLD watermark, and stamping is what CHANGES the value — so it
   predicts nothing. This step said "Run this check FIRST" until 2026-08-23,
   one line above its own measurement that stamping moves it `0 -> 31`.
   Measured 2026-08-23 (zeta, cc-02): pre-stamp **0** (gate PASSES, stamp
   licensed), post-stamp **3** — three board captures no census ever saw.
   # Rationale (WHY the gate is a transaction, not a pre-check): core/config/rationale/third-class-watermark-gate.md

   RUN IT AS A THREE-STEP TRANSACTION, saving the prior value FIRST:
   Bash: source core/scripts/_paths.sh && cp "$AGENT_DIR/temp/.drain-watermark" "$AGENT_DIR/temp/wm-prior.raw" 2>/dev/null
   Bash: <the stamp command below>
   Bash: bash core/scripts/temp-drain-purge.sh --dry-run   # read `would_purge` NOW
   IF > 0: RETRACT by RESTORING the prior (`cp "$AGENT_DIR/temp/wm-prior.raw"
   "$AGENT_DIR/temp/.drain-watermark"`; `rm -f` only when there was no prior —
   it drops `watermark_source` to absent and discards a still-valid older,
   narrower license). Re-run the dry-run to CONFIRM it returned to 0, and say
   in the Phase 4 report which files it would have condemned. Retraction is
   always safe: it removes a LICENSE, never data.
   Measured 2026-08-22 (alpha, hostname cc-04, reducer): stamping flipped
   `watermark_source` absent->file and took `would_purge` 0 -> 31, condemning
   `experience.jsonl.local-preserve-20260818`,
   `vanished-goals-recovery-2026-08-20.jsonl`, `experience.jsonl.remote` and an
   unapplied `npc-hours-fix.patch` — recovery layers, i.e. an
   archive-before-delete violation produced by following this step literally.
   `rm -f "$AGENT_DIR/temp/.drain-watermark"` retracted it and returned
   would_purge to 0; retraction is always safe because it removes a LICENSE,
   never data. A same-day reducer pass had already refused this stamp for the
   same stated reason and recorded it only in an outcome_note, where the next
   executor of this step never read it — which is why the correction is HERE
   (guard-1984: a guardrail cannot outvote the instrument it guards).
   Bash: source core/scripts/_paths.sh && [ -n "$AGENT_DIR" ] && (date -d '-60 min' +%Y-%m-%dT%H:%M:%S 2>/dev/null || date +%Y-%m-%dT%H:%M:%S) > "$AGENT_DIR/temp/.drain-watermark" || echo "REFUSED: AGENT_DIR unresolved — watermark NOT advanced"
   The source + non-empty guard are MANDATORY (guard-687/guard-968: a bare
   $AGENT_DIR in an unsourced Bash step collapses "$AGENT_DIR/temp" to
   "/temp" — the same empty-var class the Phase 1.5 helper note above
   documents). The stamp is BACK-DATED 60 minutes (fresh-eyes fix,
   2026-08-21): the drain ENUMERATED at Phase 1, minutes before this step
   runs, so a completion-time stamp would falsely license purging files
   created inside that window that no drain ever saw. Back-dating puts the
   claim on the safe side for any drain shorter than 60 min; files
   classified but younger than the margin merely stay exempt until the next
   drain (fail-exempt in both directions). The `date -d` fallback keeps the
   step alive on a non-GNU date (stamps un-back-dated — old behavior, never
   broken). Both writer and consumer run under the settings-enforced TZ=UTC
   env, so the naive stamp compares on one clock (guard-982; rb-3741's
   TZ-split hazard applies to long-lived daemons, not these hook-env runs).
   The watermark asserts "a completed drain pass enumerated everything
   present at least 60 minutes before this drain finished" — it is what
   licenses temp-drain-purge.sh Lane 1 to mechanically delete THIRD-CLASS
   files (odd suffixes neither drainable nor enumerated ephemera) older
   than it. READ THAT ASSERTION AGAINST PHASE 1 BEFORE YOU MAKE IT: the
   census globs `*.md` and `*.json` ONLY, so "enumerated everything present"
   is FALSE for exactly the third-class suffixes the stamp condemns. The two
   halves of this step were written against different populations and the
   gate at the top is what reconciles them. A --file invocation classifies ONLY its target, and a --dry-run
   classifies nothing, so writing it there would falsely license purging
   files no drain ever saw. The marker is a MANAGED
   dotfile: allowlisted in Lane 0's unmanaged-dotfile report and exempt from
   every purge lane (dotfile exclusion). Rationale + predicate:
   _purge_find_predicate header in core/scripts/temp-drain-purge.sh;
   temp-store.md § The third-class watermark.

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
