---
name: drain-temp
description: "Reviews every item in the agent's temp/ staging area (scripts, logs, folders and docs alike) and gives each one a recorded decision before anything is deleted. Takes the census from temp-decisions.sh pending, decides obvious junk in bulk (aged empty files and finished-run logs, still one recorded row each), then routes every other item to its durable home: knowledge to the knowledge tree, lessons to the reasoning bank or guardrails, a reusable script to world/scripts or core/scripts, data worth keeping to a receipted archive, junk to discard. Records each decision and its reason in temp/.temp-decisions.jsonl, moves each temp copy out once its content is home, then runs temp-drain-purge.sh, which deletes only reviewed discards and logs every deletion. Use when the user says \"drain temp\", \"review temp\", \"clear out temp\", or when aspirations-precheck flags temp_drain_needed or temp_drain_stalled. Pass --dry-run to see the census and the proposed decisions without recording, moving or deleting anything."
user-invocable: true
triggers:
  - "/drain-temp"
  - "drain temp"
  - "review temp"
  - "encode everything in temp"
parameters:
  - name: dry-run
    description: "Show the census and the proposed decisions; record, move and delete nothing"
    required: false
  - name: file
    description: "Review one named item (a file or a folder) instead of the whole store (e.g. --file design-2026-06-02.md)"
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

# /drain-temp — Temp Review

`agents/<agent>/temp/` is a staging area. Every item in it — a doc, a script, a
log, a folder — gets a review and a recorded decision, and nothing is deleted
until a review has decided it (user directive, 2026-10-05). This skill is that
review: it lists what needs one, decides obvious junk in bulk, routes everything
else to its durable home, records every decision with its reason, and lets the
guarded purge delete only what was decided to be junk.

**Hybrid skill**: the user invokes it ("drain temp", "review temp"); the
aspirations loop invokes it when `aspirations-precheck` flags `temp_drain_needed`
or `temp_drain_stalled`. Writes to the tree / reasoning bank / guardrails /
scripts folders — requires assistant or autonomous mode.

## Why this exists

An item left in `temp/` is in none of the retrieval stores — invisible to
`/prime` and `retrieve.sh` — and a script left there is in no scripts folder, so
nobody finds it to run again. The review is how that value becomes durable and
findable, and the decision log is how anyone can later see what happened to
each item and why. See `core/config/conventions/temp-store.md`.

## How the pieces fit

| Piece | Role |
|---|---|
| `core/scripts/temp-decisions.sh` | Owns the decision log `temp/.temp-decisions.jsonl` (append-only, machine-local). `pending` lists what needs a review, `bulk-junk` decides obvious junk, `decide` records decisions, `show` reads back what was decided and deleted. |
| `core/scripts/temp-drain-purge.sh` | The only deletion path. Deletes only items whose decision in force is `discard`, re-checks its guards at the delete itself, logs every deletion to the same log, and asks the shared store to delete its copy too (only the box that owns the agent can; its `backend_propagation` field says what happened). |
| `aspirations-precheck` temp-pressure | Counts the same population `pending` lists, and files the review goal once it crosses the drain threshold. |

Three rules hold the design together:

1. **Decide, never delete.** No `rm`, `find -delete` or hand-rolled cleanup in
   `temp/`. A discard is a recorded decision; the purge executes it. A step that
   deletes directly would bypass the purge, and its guards would never run
   (guard-6001: when an instruction and a sweep both delete, the instruction's
   exceptions are the only ones that hold).
2. **Do the work, then record, then move.** A decision row claims something is
   true — an encode landed, a script is in its new home, an archive has its
   receipt. Record it only once that is true, and move the temp copy out only
   after it is recorded: an interruption then leaves the item pending with its
   decision visible, never moved without a record.
3. **The log is the authority, and it is bound to content.** A decision applies
   only while the item still matches what was reviewed (a file's content; a
   folder's file paths, sizes and times). An item edited after its review comes
   back as pending, and a `keep` comes back after 30 days.

## Phase 0: Load Routing Context

```
Bash: load-conventions.sh temp-store learning-routing
→ Read the returned paths not already in context. learning-routing.md is the
  "where does this go?" decision tree Phase 3 routes by; temp-store.md is the
  lifecycle this skill runs.
```

## Phase 1: Census

```
Bash: bash core/scripts/temp-decisions.sh pending --summary
→ JSON summary of the bound agent's temp/:
  pending           items that need a decision; by_class splits them
                    (doc / script / run-output / empty / dir / other)
  fresh             in flight — touched within 120 min (a folder: its newest
                    entry at any depth). Not reviewed, not counted, never
                    purged; they come back once they stop changing
  kept              a `keep` still in force
  awaiting_purge    a `discard` the next purge will execute
  receipted_dirs    folders carrying a top-level RECEIPT.* or .archive-marker:
                    archives, outside the review, never purged
  tracked_skipped   git-tracked files (changed through git, not here)
  bad_names         names no decision can be recorded for (a control
                    character); rename those by hand
  ledger_bad_lines  unparseable log lines (reported, ignored)

IF pending == 0: report "nothing awaiting review" and go to Phase 5 step 2 —
   the purge still executes discards recorded earlier.
IF --file <name>: review just that item — skip Phase 2; in Phase 3 read only
   its entry (`pending` lists it unless it is in flight or already decided;
   add --include-fresh to see in-flight items).
```

Every command here resolves `temp/` from the bound agent (`$MIND_AGENT` via
`_paths.sh`). Never hardcode an agent name and never derive the path from world/
or meta/ (`.claude/rules/path-resolution.md`).

Hidden dotfiles are outside the review on purpose. The framework's own markers
(`.gitkeep`, `.archive-marker`, the decision log and its lock) are managed
state; any other dotfile is reported by the purge as `unmanaged_dotfiles`
(Lane 0) for a person to look at. A dotfile can hold credentials — the case that
added Lane 0 was a launch payload carrying an API key — so open one only to learn
what it is, and never quote it.

## Phase 2: Decide Obvious Junk in Bulk

```
SKIP under --file.

Bash: bash core/scripts/temp-decisions.sh bulk-junk --dry-run
→ the items it WOULD decide: aged 0-byte files, and aged .log / .out / .err /
  .raw files (run output). Never a folder, a dotfile, a cited or git-tracked
  file, or an item that already has a decision in force.

Read that list BY NAME before recording it. A suffix is evidence, not proof
(guard-6368: a name is not what a thing does) — a `.log` holding a curated
measurement, or the only copy of a result, is not junk. Review each such item
first (Phase 3) and record its own decision (Phase 4: keep, archive or encode);
bulk-junk skips an item that already has a decision, so the exception holds.

Then, unless --dry-run:
Bash: bash core/scripts/temp-decisions.sh bulk-junk
→ {"decided": N, "skipped": {...}, "items": [...]}: one row per item, recorded
  `by: bulk-junk` with a `why` naming the rule. rc=3 = the cited or tracked set
  could not be read, so it decided NOTHING — retry it, never work around it.
```

## Phase 3: Review Each Item

```
Bash: bash core/scripts/temp-decisions.sh pending --limit 40
→ one entry per item, oldest first:
    [class] name  size  age  why_pending, flags...
        its first lines (a folder: its first entries)

why_pending:
  undecided               never reviewed
  changed-since-decision  edited after its review — review it again
  keep-expired            a keep older than 30 days — is it still in use?
  decided-not-executed    an encode / promote / archive whose move never
                          happened — finish it (Phase 5) or decide again
  discard-blocked: <why>  a discard the purge would now refuse (cited since,
                          or a folder that gained git work) — decide again
flags:
  CITED by <path>         a durable record points at it: it cannot be
                          discarded — encode or fold it, archive it, or keep it
  same as <x>             identical bytes elsewhere: another pending item,
                          drained/<name> (an already-drained copy that came
                          back, rb-3498), or a script's durable home
  git {...}               a folder holding a repo: unpushed / dirty work

Large backlog: review in batches (--limit N, or --class C). The pressure count
falls as each batch is recorded, and the next precheck files the review again
while the rest still crosses the threshold. Two measured shortcuts for a big
doc backlog: read the store ONCE and compare every doc against it (rb-8850),
and run the 3b probe across all docs first, then work through each verdict
bucket (rb-9032) — each item still gets its own decision and its own why.
```

For each item, oldest first:

```
1. Read it — the whole file; for a folder, list it and open what matters.

2. Retrieve before deciding (retrieve-before-deciding.md) — TWO queries:
   Bash: bash core/scripts/retrieve.sh --category "<one-line summary of its topic>" --depth shallow
   Bash: bash core/scripts/retrieve.sh --category "<the item's name>" --depth shallow
   The first finds the destination and any encoding that already exists. The
   second finds guardrails and lessons that name the item itself: a temp file
   can be the last surviving copy of store records, and when it is, a
   guardrail says so by its file name.

3. Decide by what the item IS, never by its suffix (table below).
```

| What it is | Decision | `where` | How (Phase 4) |
|---|---|---|---|
| Reusable knowledge, facts, patterns | `encode` | the tree node key | `/tree add`, or Edit the node step 2 surfaced |
| A time-anchored lesson ("this failed because X") | `encode` | `rb-NNN` | `reasoning-bank-add.sh` |
| A rule the agent must obey | `encode` | `guard-NNN` | `guardrails-add.sh` |
| The narrative of a goal or session | `encode` | `exp-...` | the experience archive |
| A stable value (path, endpoint, id) | `encode` | `world/conventions/<kind>.md` | Edit the locator file (encode-stable-facts.md) |
| Unapplied work: a goal never filed, a note that never reached its goal | `encode` | the goal id | file or apply it — only after 3b's store-wide check |
| A script or tool you will run again | `promote` | `world/scripts/<name>` or `core/scripts/<name>` | copy it home, then prove it runs there |
| Data worth keeping that no record can absorb | `archive` | `temp/<slug>/` | a receipted folder |
| Still in use by open work | `keep` | — | `why` names that work (a goal id); it comes back in 30 days |
| Already encoded, superseded, a duplicate, or junk | `discard` | — | `why` names the evidence |

```
   One item can feed several stores (a tree node AND a guardrail): put the
   primary destination in `where` and name the others in `why`.

   DISCARD is a first-class outcome — do not force junk into the tree. Every
   `why` names its evidence: "identical to drained/<name>", "encoded as rb-NNN",
   "one-off probe; its result is recorded in g-NNN-NN".

   SCRIPTS AND TOOLS — ask what the thing is before choosing a folder:
   - A script that will run again goes to world/scripts/ when it serves this
     world's domain. It goes to core/scripts/ only when it is framework code,
     and then it must be domain-free (domain-leak-check.sh), it needs a test
     under core/scripts/tests/, and it is a framework change like any other:
     commit it, and it travels by promotion. When unsure: world/scripts/.
   - A "tool" that is not code (a recipe, checklist, how-to) goes to the
     knowledge tree, or becomes a forged skill when it is a multi-step
     procedure invoked by name. Never a scripts folder.
   - A one-off script whose result is already recorded: discard.
   - `same as world/scripts/<name> (identical)`: discard — it is already home.
     `(differs)`: compare the two and keep the better one at home.

   CAUTIONS — each is a measured loss:
   - A goal-filing payload (title / description / priority keys): never
     discard it on the probe verdict alone, and never file it blind — its work
     may have shipped under another title (guard-5720). Check the queues first.
   - An outcome-note .md is not proof the note reached its goal (guard-6976):
     read the goal record. Missing there → apply it (encode, where = goal id).
   - A .md opening with front matter that carries `parent:` and `node_type:`
     is a KNOWLEDGE NODE (guard-4595): find the live node by key and diff
     against it plus its children. Discard loses it if it is not live; encode
     writes a duplicate.
   - A snapshot or export of a store (records in the store's own shape) can be
     the last copy of records the store has since lost: compare it with the
     store before any discard.
   - A raw export of customer or account data: discard it — the purge also
     deletes the shared-store copy, which a hand rm never reaches
     (guard-6870) — or archive it outside temp/ if a record truly needs it.
     Never encode it into drained/, which keeps it 30 more days (guard-7074).
   - A folder holding a git repo with unpushed or dirty work: push or archive
     that work first — the discard is refused until then.
   - A loose RECEIPT.* at temp/ root belongs beside the archive it describes
     (guard-6152): verify it against that archive, copy it there, cmp, and
     only then discard the temp copy (why: where the copy now lives).

3b. BEFORE deciding DISCARD for a doc (.md / .json), run the scripted encode
   probe. DISCARD is the one decision whose wrong call is silent and
   irreversible — the purge deletes the item and nothing re-examines it — and
   it was the only one resting entirely on LLM judgement (g-115-3089).

   Bash: py -3 core/scripts/drain-encode-probe.py <file> --json

   The probe infers the artifact's TYPE from its field shape, then probes the
   store for the EFFECT the payload would have had — not merely for something
   with a matching name. Act on the verdict:

   | verdict | meaning | required action |
   |---|---|---|
   | `absent`  | the payload's effect is NOT in the store | **No discard yet.** Run the store-wide check below; then encode it, or discard it with the evidence that it was refused, superseded or wrong |
   | `encoded` | the effect is present in the store | discard is safe; cite the probe's evidence in `why` |
   | `unknown` | shape unrecognised / store unreadable / effect not observable | your judgement decides — read the item |

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

4. Under --dry-run: record nothing. List each proposed decision with its
   `where`, then go to Phase 5 step 2 (the purge's own --dry-run).
```

## Phase 4: Act, Then Record

```
SKIP under --dry-run.

For each item, do its work first (rule 2), then record it:

  discard, keep  nothing to do first.
  encode         write the encoding (the How column). For a tree node, update
                 last_updated + last_update_trigger (knowledge-freshness.md).
                 Unapplied work: file the goal (aspirations-add-goal.sh), or
                 apply the note (aspirations-update-goal.sh REPLACES the
                 field, so read it first — guard-5228). READ IT BACK.
  promote        create the script at its new home with the Write tool (it
                 resolves world/ to the configured path and pushes the file
                 to the shared store), keeping its name unless the folder's
                 naming says otherwise. Make it executable if it is run
                 directly, then RUN it from there: its --help, a dry run, or
                 at least a syntax check (bash -n / py_compile). A core/scripts/
                 script also needs its test under core/scripts/tests/.
  archive        write temp/<slug>/RECEIPT.md: what the item is, why it is
                 kept, and the durable record that cites it. An archive no
                 record points to is the old reports/ slush wearing a marker
                 (temp-store.md § D2): name the citing record, or do not
                 archive.

Record in batches, with a quoted heredoc (stdin-json-inputs.md):
  Bash: bash core/scripts/temp-decisions.sh decide <<'EOF'
  [{"item": "probe-output.txt", "decision": "discard", "why": "one-off probe; its result is recorded in g-NNN-NN"},
   {"item": "design-notes.md", "decision": "encode", "where": "<node-key>", "why": "design decisions encoded into the node"}]
  EOF
  A batch over ~6 KB will not fit on one command line: Write the JSON array to
  agents/<agent>/sessions/<SID>/scratch/decisions.json and redirect it in
  (`decide < <that path>`).
→ {"recorded": N}, rc=0.
  rc=1: something in the batch is invalid and NOTHING was recorded. `errors`
  names each record and why — a missing why or where, a dotfile, a
  git-tracked file, or a discard the purge would refuse (cited, a receipted
  folder, a folder with git work). Fix those records and resend the batch.
  rc=3: git could not say which files it tracks; nothing recorded — retry.
```

## Phase 5: Move, Then Purge

```
1. Move the temp copy of each recorded encode and promote into drained/ (an
   audit copy for 30 days; the purge's Lane 2 clears it after that), and each
   archive into its folder. Never a bare mv: it silently overwrites a
   same-named file already in drained/ (guard-1032).

   Bash: export MIND_AGENT=<agent>; source core/scripts/_paths.sh && [ -n "$AGENT_DIR" ] && T="$AGENT_DIR/temp" && mkdir -p "$T/drained" && { mv -n "$T/<name>" "$T/drained/<name>"; if [ -e "$T/<name>" ]; then echo "NOT MOVED: <name>"; else echo "moved: <name>"; fi; }

   (An archive: mv -n "$T/<name>" "$T/<slug>/<name>" behind the same
   guards.) The export and the non-empty check are not ceremony: with the
   agent unset, _paths.sh resolves ANOTHER agent's dir (guard-687). The
   existence test, not mv's exit status, says whether it moved: `mv -n`
   reports a skipped move differently across versions.

   NOT MOVED means drained/<name> already exists. Compare the two:
     identical (cmp -s) → record `discard` for the temp copy, why "identical
                          to drained/<name>"; the purge removes it.
     different          → move it under a dated name instead:
                          drained/<stem>-<YYYYMMDDTHHMMSS><suffix>.

   An archive whose citing record names the old path: edit the record to name
   temp/<slug>/<name>, or fold the content into it, so the citation does not
   dangle.

2. See what the purge will delete BEFORE it deletes (guard-3109):
   Bash: bash core/scripts/temp-drain-purge.sh --dry-run
   → would_purge + files (Lane 1, files) and stray_would_purge + stray_dirs
     (Lane 3, folders) must be exactly the discards recorded and still in
     force. Anything you did not expect there: STOP and find out why first.
     drained_gc_files (Lane 2): drained/ copies older than 30 days.
     age_skipped / stray_age_skipped: decided items touched within 120 min,
     skipped this run and deleted by a later one.
     decisions_lookup or citation_lookup "failed": Lanes 1 and 3 delete
     nothing this run, so their zeros are unmeasured, not clean.

3. Execute (skip under --dry-run):
   Bash: bash core/scripts/temp-drain-purge.sh
   → purged + files, stray_purged + stray_dirs, drained_gc_purged,
     stray_preserved_git_dirs, deletions_logged + deletion_log ("failed" =
     deletions the log does not show; the stderr WARN names them), and
     backend_propagation: whether the shared-store copies were deleted too —
     read it, never assume. unmanaged_dotfiles + unmanaged_dotfile_names
     (Lane 0) read the same with or without --dry-run and were NOT touched:
     name them in the report, or they stay invisible.
```

The purge is the only deletion path, and that is load-bearing. A hand-rolled
`rm -f "$TEMP_DIR/$f"` whose variable resolves empty becomes an `rm` on a
root-relative path, which Claude Code flags as a dangerous rm and PROMPTS for
confirmation even under --dangerously-skip-permissions. An autonomous agent
cannot answer its own dialog, so the loop looks alive (state=RUNNING) while it
hangs at zero progress until a human taps a button — an agent hung 46+ min this
way (g-115-1876). The helper asserts the temp dir is set, absolute, strictly
under the project root and named `temp` before any deletion, and deletes with a
bounded `find` per decided path — never an `rm` on an interpolated path.

## Phase 6: Report, Re-read, Journal

```
1. Bash: bash core/scripts/temp-decisions.sh show --since 3h --limit 0
   → every decision and deletion this pass recorded (widen --since if the pass
     ran longer).

2. Emit:
   ═══ TEMP REVIEW ═══════════════════════════════
   Decided: {N} item(s)
     encode   {n}  {item} -> {where} ...
     promote  {n}  {item} -> {where} ...
     archive  {n}  {item} -> {where} ...
     keep     {n}  {item} ({why}) ...
     discard  {n}  ({b} of them by bulk-junk)
   Deleted: {purged} file(s), {stray_purged} folder(s), {drained_gc_purged} from drained/
   Still pending: {pending}    In flight: {fresh}
   Unmanaged dotfiles: {names}                       (omit when 0)
   ═══════════════════════════════════════════════

3. End on a fresh read, not on the opening list — temp/ is written to while
   the review runs (guard-5718):
   Bash: bash core/scripts/temp-decisions.sh pending --summary
   Items that appeared during the pass are in flight and wait for the next
   one. Anything else still pending is unfinished: say so in the report.

4. IF anything was decided or deleted, and a goal invoked this review:
   Bash: bash core/scripts/journal-append.sh --goal <that goal id> --outcome-class routine --summary "temp review: <N> decided, <M> deleted, <K> still pending"
   The decision log stays on this box; the journal is the cross-box record of
   what the review did. A direct user request has no goal to attribute: the
   decision log and the report above are its record.

5. A loop-invoked review (temp_drain_needed / temp_drain_stalled) is
   satisfied once pending is below the drain threshold — the next precheck
   recomputes it.
```

## Invocation Rules

- Never delete anything in `temp/` directly — no `rm`, no `find -delete`.
  Record a discard; `temp-drain-purge.sh` executes it.
- Never review `temp/drained/` — it holds audit copies that the purge's Lane 2
  clears after 30 days.
- Never hardcode an agent name — operate on the bound agent (`$MIND_AGENT`).
- Every decision carries a `why` naming its evidence; encode, promote and
  archive also name `where`.
- `--dry-run` has no side effects (no record, no move, no delete) — safe in any
  mode.
- Retrieve before each decision (Phase 3 step 2, both queries).
- DISCARD is valid — not every item deserves a home.

## Chaining

- **Called by**: the user ("drain temp", "review temp"); the `/aspirations` loop
  when `aspirations-precheck` emits `temp_drain_needed` or `temp_drain_stalled`.
- **Calls**: `temp-decisions.sh`, `temp-drain-purge.sh`, `retrieve.sh`,
  `drain-encode-probe.py`, `/tree add`, `reasoning-bank-add.sh`,
  `guardrails-add.sh`, the experience archive path, `aspirations-add-goal.sh` /
  `aspirations-update-goal.sh` (unapplied work), `journal-append.sh`.
- **Does NOT call**: `/start`, `/stop`, `/aspirations`.

## Return Protocol

See `.claude/rules/return-protocol.md` — the last action MUST be a tool call, not
text. The terminal action is the Phase 6 `journal-append.sh` call — or, when no
goal invoked the review, under `--dry-run`, or with nothing to review, a final
`Bash: echo` handing control back. When invoked from the loop, never end with a
text summary — control returns to the orchestrator.
