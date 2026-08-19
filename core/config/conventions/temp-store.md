# Temp Store Convention

Canonical temp store for agent working documents pending drain to the
knowledge tree. The single permitted write target for transient working
files that used to scatter across `reports/` and ad-hoc locations.

- **Path**: `agents/<agent>/temp/`
- **Lifecycle**: between-step staging — preserved across iterations,
  compactions, and recovery; drained to the knowledge tree by the
  `/drain-temp` skill (Phase 5 of the file-model normalization).
- **Durability**: own-cloud S3-synced (git-ignored). The owncloud sweep
  pushes `temp/` to S3 like other governed agent state (it is not in
  `_EXCLUDE_DIRS`), and `pull_temp` — folded into `pull_continuity`, run at
  `/start` — resumes it on a machine-move via a prefix listing + the same
  no-clobber freshness gate as session continuity. So temp/ working docs
  survive a cross-machine agent move without waiting on a git round-trip.
  ALL of `temp/` is gitignored (g-115-1765) — working docs, the `drained/`
  audit trail, pure ephemera (`.log`/`.txt`), and any ad-hoc scripts/subdirs.
  Durability is the S3 sync above, not git: `temp/` is a transient staging area
  (everything drains to the tree or is discarded), so it does not belong on the
  shared git surface — and cross-agent temp peeking is an anti-pattern anyway
  (see 'Searching temp/'). Only `.gitkeep` is tracked, to preserve the dir on a
  fresh clone. Unlike `session/`/`sessions/`, the ignore is now a portable
  committed `.gitignore` rule (it previously lived only in a machine-local
  `.git/info/exclude`, which did not travel to fresh boxes — the g-115-1765 bug).
- **Purge reclaims the store of record — since g-115-6196 (`bba547e57`), and NOT
  before.** The S3 sync above has a consequence that is easy to miss and was
  live for a long time: because `temp/` is a real sync root, a local `find
  -delete` reclaims *nothing*. Every purged file remained an object in the
  authoritative store, so a purge was **not durable** (any `owncloud-pull.sh
  --with-temp`, fresh clone, or transplant restored it) and the store was never
  reclaimed. Measured at the time: one agent's local temp went to 31 files / 60 MB
  while its S3 prefix held 23,125 objects / 3.33 GB. `temp-drain-purge.sh` now
  pipes its exact deleted set into `owncloud_sync.py --purge-propagate`
  (`temp-drain-purge.sh:694`), which `delete_object()`s each key. Three
  properties worth knowing before relying on it:
  (a) it is **ownership-gated** — a non-owner box refuses every key, so the S3
  half of a purge is an owner-box act, which is also where residue accumulates;
  (b) it is **fail-soft** — propagation failure degrades to the pre-fix
  local-only behavior and is recorded in the report's `backend_propagation`
  field rather than raised, so read that field instead of assuming;
  (c) it is **not retroactive** — the historical backlog is untouched by design
  and is owned separately by g-115-6229, so a still-growing prefix count is
  expected and is not evidence the fix failed.
  Verified on the owner box by HEAD on a specific key (alpha, `hostname` cc-04,
  2026-08-15): present after push → **still present after a local `rm`** (the
  defect, reproduced) → absent after propagate; with `--dry-run` correctly
  reporting `would_delete` without deleting, and out-of-scope / other-agent
  paths refused.

---

## Why this exists

The knowledge retrieval pipeline searches a fixed set of stores (knowledge
tree, reasoning bank, guardrails, pattern signatures, experience, beliefs,
experiential index). A working document written ONLY to a slush directory
(historically `agents/<agent>/reports/`) is in none of them — it is
invisible to `/prime` and `retrieve.sh` forever. A second, disconnected
retrieval surface is not a style problem; it is lost knowledge.

`temp/` resolves this by being explicitly a STAGING area, not an archive:
every file in `temp/` either drains into the knowledge tree (the one
long-term retrieval surface) or is discarded. Nothing in `temp/` is meant
to live there permanently.

## temp/ vs session/scratch/

| Property | `temp/` | `session/scratch/` |
|---|---|---|
| Path | `agents/<agent>/temp/` | `agents/<agent>/session/scratch/` |
| Scope | Agent-wide (not per-session) | Per-session |
| Lifetime | Preserved until drained by `/drain-temp` | Wiped on `/start --recover` and recovery-gate auto-recovery |
| Recovery | preserve (drain is the only deletion path) | clear (`session-manifest.yaml recovery_action: clear`) |
| Content | Working documents with reuse value that DRAIN to the tree: analyses, briefings, audits, design docs, snapshots | IO buffers with no reuse value: probe dumps, JSON staging, one-shot work files |
| Drain target | Knowledge tree / reasoning bank / experience | Nowhere — ephemeral by definition |
| Git | Gitignored — all of `temp/` except `.gitkeep` (g-115-1765); durability via S3 sync | Gitignored (`**/session/`) |
| Cross-machine | Synced to S3 — survives an agent moving boxes | `sync_tier: machine_local` — **never leaves this box**; an agent that moves machines loses it outright |

**Decision rule**: if the content might be worth encoding into the
knowledge tree later, write to `temp/`. If it is a throwaway IO buffer
consumed within the current step, write to `session/scratch/`.

**Two traps in the `Lifetime` and `Cross-machine` rows above** (g-335-941):

1. `session/` (SINGULAR) is not `sessions/` (PLURAL). The 24h mtime TTL people
   remember belongs to `cleanup-stale-bindings.sh`, which sweeps
   `agents/<name>/sessions/<SID>/` — the per-session dirs. `session/scratch/`
   is swept by a different mechanism entirely. Misreading the two costs you the
   wrong mental model of when your file dies.
2. Scratch's clear is an **EVENT, not a timer** — every `/start --recover` and
   every recovery-gate auto-recovery. There is no deadline you can plan around,
   which is sharper than a TTL, not softer.

### Do not cite a `session/scratch/` path from a durable record

A goal description, tree node, reasoning-bank entry, or convention that names a
`session/scratch/` path is a durable pointer into a target that will be cleared
without warning. It WORKS at write time — the file is right there — and the
failure is silent and delayed. Write the content to `temp/` and cite that, or
inline the content into the record.

**This is a narrow rule, and the narrowness is measured** (echo, hostname
`cc-03`, `uname -r` 6.8.0-136-generic, 2026-08-06, own-cloud, 1,435 durable
surfaces — world tree + world conventions + world aspirations/reasoning-bank/
pipeline/guardrails/pattern-signatures + all 5 agent aspiration queues):

| predicate | distinct refs |
|---|---|
| `agents/*/session/` anywhere | 65 |
| …naming an at-risk `session-manifest.yaml` entry (`recovery_action: clear` ∪ `sync_tier: machine_local`) | 26 |
| …under `session/scratch/` — **the actual defect class** | **1** |
| `agents/*/temp/` (the sanctioned store), for contrast | 358 |

**Do NOT widen this into a gate on "durable record cites an ephemeral path."**
25 of those 26 at-risk citations are *evidence citations* — a record naming a
session file as the PROVENANCE of a measurement it already transcribes
(`Source: agents/<agent>/session/precheck-drops.jsonl, 92 precheck-end
records`). That is the exact discipline `guard-1476` mandates, so the obvious
gate would fire ~25 false positives on compliance with another guardrail and
train agents to drop their provenance stamps. The single scratch-shaped hit was
`g-335-941`'s own narrative quoting an already-remediated incident from another
deployment. The distinction that matters is **evidence citation vs content
pointer**, not durable-vs-ephemeral.

## File naming

Flat directory, timestamped filenames for uniqueness:

```
temp/<type>-<YYYY-MM-DDTHH-MM-SS>.md
temp/<type>-<YYYY-MM-DDTHH-MM-SS>.json
```

Examples: `temp/design-notes-2026-06-02T14-30-00.md`,
`temp/diag-<subsystem>-2026-06-02T14-30-00.md`,
`temp/audit-<topic>-2026-06-02.md`.

Cadence-ritual briefings (`fresh-eyes-*.md`, `fresh-eyes-program-*.md`,
`fresh-eyes-tree-*.md`, `felt-sense-*.md`) are the EXCEPTION: they self-encode
their durable value at creation (their own Phase 5.6 / lane sweeps write to
tree/RB/guardrails/Self), so they are archival-by-design and land straight in
`temp/drained/` — they never enter the drain queue as already-encoded slush
(g-115-1838). Do not treat a `temp/drained/fresh-eyes-*.md` as an undrained
working doc.

(NOT temp/: completion reports are the single `COMPLETION-REPORT.md` pointer at
the agent root — git history is their archive; phase-cost telemetry and the
completion delta-baseline (`last-outcome-snapshot.yaml`) are operational state
under `session/`. temp/ holds only working docs that DRAIN to the knowledge tree.)

Flat directory with ONE structural exception: `drained/` (below). No
other subdirectories, no goal-specific nesting, no ad-hoc scripts.

## drained/ subdirectory

```
temp/drained/
```

When `/drain-temp` (Phase 5) processes a file — extracting its value into
the knowledge tree, reasoning bank, or experience archive — it moves the
file to `temp/drained/` with its original name, leaving an audit trail of
what was drained and when. `temp/drained/` contents older than 30 days
carry zero retrieval value (their knowledge is in the tree). As of
g-115-2948 this GC is **automated** — `temp-drain-purge.sh` **Lane 2**
(`gc_drained_archive`) prunes `drained/` files older than `--drained-age-days`
(default 30) on every run, via the same guarded `find -maxdepth 1 -type f
-mtime +N -delete` pattern (the `drained/` dir itself is always preserved).
No separate maintenance goal is needed.

**A citation protects an artifact in `drained/` too (g-306-102).** Lane 2
exempts any file whose basename is cited by a durable record, the same
exemption Lane 1 gained in g-306-111 and keyed the same way. Before this,
citation-protection was a property of WHICH DIRECTORY a file sat in rather
than of the artifact: draining a cited doc into `drained/` stripped its
protection and made it age-deletable with no reference check at all. Two
consequences worth knowing:

- When the cited set cannot be determined, Lane 2 is **skipped entirely**
  (deletes nothing, warns on stderr) rather than falling back. Lane 1 can
  degrade to the pre-inversion allow-list because that is strictly
  no-worse-than-before; Lane 2 has no allow-list to fall back to, so its only
  no-worse option is to delete nothing. A zero `drained_gc_would_purge` under
  `citation_lookup=="failed"` is a **not-run, not a clean run**.
- Lane 2 emits per-file basenames as `drained_gc_files`, so the exemption is
  verifiable from outside — `durability-property-check.py cited-temp-not-purged`
  now covers Lanes 1+2 rather than Lane 1 only. Lane 3 stays count-only by
  nature: it deletes DIRS, and the cited set is keyed on file basenames.

The "no other subdirectories" rule
above is likewise enforced by **Lane 3** (`cleanup_stray_dirs`): any dir
DIRECTLY under `temp/` that is NOT `drained/` and is untouched past the
`--age-min` guard (default 120 min) is removed via a per-dir-bounded guarded
`find "$stray" -delete` — sweeping abandoned scratch subdirs (e.g. a leftover
`gNNN-session/`) that neither the ephemera purge (`-type f`) nor the `.md`/
`.json` drain ever reach.

## Everything that is not a content-bearing `.md`/`.json` — purged, not drained

temp/ holds THREE file classes. One drains; the other two are purged. As of
g-306-111 Lane 1 is **purge-by-default with exemptions**, not an allow-list of
extensions — so the third class is bounded BY THE PREDICATE rather than by a
list that goes stale on the next goal (see § The third class, below):

| Class | Extensions | Carries knowledge? | `/drain-temp` action |
|---|---|---|---|
| Drainable working docs | `.md`, `.json` (with content) | Yes — analyses, briefings, designs | **Exempt from purge.** Encode to tree/RB/experience, then move to `drained/` |
| Pure ephemera | `.log`, `.txt`, `.py`, `.sh`, `.err`, `.raw`, `.out`, `.bak` — **common examples, NOT a closed list** (these 8 WERE the whole lane pre-g-306-111; they are now merely the frequent members of the row below) | No — test-suite output, tool dumps, one-off scratch scripts, raw command-output dumps, backup copies | **Purge (delete)** in Phase 1.5, once older than a 120-min age guard |
| Empty scratch | ANY name **except dotfiles**, 0 bytes (`-empty`) | No — nothing was ever written | **Purge (delete)** in Phase 1.5, once older than the 120-min age guard |
| **Third class** (the complement) | everything else — `.jsonl`, `.yaml`, `.xml`, `.tsv`, `.gz`, `.eml`, `.sha256`, and any suffix a goal invents | Sometimes | **Purge (delete)** past the same 120-min guard, UNLESS cited by a durable record (§ The third class (a)(1)). Cited-but-unwrapped files survive so they can be promoted into a receipted dir. |

> ⚠ **Anything that is not a content-bearing `.md`/`.json` is purged.**
> Since g-306-111 the predicate in `core/scripts/temp-drain-purge.sh`
> (`_purge_find_predicate`) is an INVERSION: it matches every depth-1 file
> EXCEPT (i) dotfiles, (ii) `.md`/`.json` **with content** — 0-byte ones still
> purge, since nothing was written to drain — and (iii) basenames cited by a
> durable record. So `.py`, `.sh`, `.raw`, `.out`, `.bak`, 0-byte files, AND
> every third-class suffix (`.jsonl`, `.yaml`, `.tsv`, `.gz`, one-offs a goal
> invents) are all purged. A one-off helper script, a raw command-output dump
> (`selector.raw`, `probe.out`), a `.bak`, a `.jsonl` scratch dump, or an empty
> scratch file left in
> `temp/` **will be deleted** once it is >120 min old. That is the intended
> behaviour (these are ephemera), but do not leave a script or dump you want to
> keep here — promote a script to `core/scripts/`/`world/scripts/`, and encode a
> dump's value (or move it out) before it ages past the guard. **Dotfiles (names
> starting with `.`) are EXCLUDED from both lanes** (`! -name '.*'`): temp/'s
> tracked 0-byte `.gitkeep` (and any dotfile marker) is protected from the
> `-empty` lane — else the drain would delete the only git-tracked file in temp/
> and iteration-commit would commit the deletion, breaking the fresh-clone dir
> guarantee above (g-115-2947 fresh-eyes catch). This table and the `find_expr`
> glob in `temp-drain-purge.sh` MUST be updated together.

**Raw command-output dumps** (redirecting `goal-selector.sh`, `retrieve.sh`, a
`/tree` summary, or any script's stdout to a file for inspection) are IO buffers
with no reuse value — per the `temp/ vs session/scratch/` table above they
belong in `session/scratch/`, not `temp/`. When convenience lands one in `temp/`,
name it with a `.raw` or `.out` extension (`selector.raw`, NOT `selector.json`)
so Phase 1.5 **purges** it, rather than Phase 1 enumerating a bare `.json` as a
drainable working doc and Phase 3 archiving megabytes of valueless scratch into
`drained/`. The extension is the stable purge marker; a bare-named `.json` dump
is treated as a working doc and drained. (g-115-2947)

**The same rule applies to one-shot command INPUT, and that direction is the one
that actually accumulates.** Everything above says "output" — dumps, stdout,
"for inspection" — so a JSON body written to be piped INTO a script
(`cat goal.json | aspirations-add-goal.sh`) reads as a legitimate `.json` working
doc, and the rule above then converts it into permanent residue. An input payload
is an IO buffer with no reuse value on exactly the same grounds as an output dump:
once the command has run, the store holds the effect and the buffer holds nothing.
Name it `.raw`/`.in`, or write it to `session/scratch/`.

Measured 2026-08-16 (echo, cc-03, g-001-84): 153 of 209 root files in one temp/
were one-shot command IO — 84 outputs, correctly `.raw`-named and purgeable, and
69 inputs named `.json`, which had accumulated over 18 days. The input half is
STRUCTURALLY STUCK, and that is why this is a convention change and not a naming
preference: wrong extension to purge (Phase 1.5 skips `.json`), and Phase 2 cannot
discard them either, because `drain-encode-probe.py` reads a goal payload whose
goal was never filed as `absent` — 69 of 69 — and `absent` BLOCKS discard by
design. Verified across four surfaces (both live queues, both archives) that the
goals genuinely do not exist, and the cause is deliberate refusal, not loss:
echo's `goal-duplication-gate` blocked 719 filings in the same window. So the
payloads are refused-draft residue that the drain lane must never encode and
cannot archive — a file class with no exit. Correct naming at write time is the
only place this is cheap to fix. (g-001-84)

Pure ephemera lands in temp/ legitimately — the framework's own guidance
redirects test-suite output here (`.claude/rules/run-full-suite-after-deep-code.md`
writes `agents/<agent>/temp/suite.log`), and one-shot tool dumps (`leak-check.txt`)
follow the same path. These files have nothing to encode, so `/drain-temp`
DELETES them rather than archiving to `drained/`: all of `temp/` (including
`drained/`) is gitignored (g-115-1765), so archiving untracked ephemera into
`drained/` would only relocate slush between two ignored paths. Deletion loses
no history — there is none to lose (nothing under `temp/` is git-tracked). The
gitignore is now a portable committed `.gitignore` rule; it previously lived
ONLY in a machine-local `.git/info/exclude`, which did not travel to fresh
boxes — so temp/ committed there every iteration until g-115-1765 moved the
ignore into the shared `.gitignore`.

Both classes feed the aspirations-precheck temp-pressure signal
(`core/scripts/precheck-eval.py` `cmd_temp_pressure`): `count` (docs) +
`ephemera_count` (.log/.txt) = `pressure_count`, which drives the warn / drain
thresholds. Before g-115-1727 the metric AND the drain glob both saw only
`.md`/`.json`, so ephemera-only accumulation was invisible to both and grew
unbounded — the exact slush-directory failure mode this convention exists to
prevent, for the one file class the drain missed.

The 120-min purge age guard protects an actively-written `suite.log` from an
in-flight run (the daemon-safe full suite is ~32 min); a just-completed log is
purged on the next drain cycle. The temp-pressure metric applies NO age guard —
it counts all ephemera so a recent slush still triggers the drain that will
later purge it.

### The purge MUST go through the guarded helper — never a hand-rolled `rm`

The Phase 1.5 purge MUST call `core/scripts/temp-drain-purge.sh` — the canonical
GUARDED purge path. Do NOT hand-roll an `rm` (or reconstruct the find/rm inline)
on a temp-dir variable. The helper asserts the temp dir is set + non-empty,
absolute, strictly under `PROJECT_ROOT`, and `basename=='temp'` BEFORE any
deletion, then deletes via `find … -maxdepth 1 -type f (ephemera globs) -mmin
+120 -delete` — never a per-file `rm` on an interpolated path (and `-maxdepth 1`
leaves `drained/` untouched).

WHY (g-115-1876): a hand-rolled `rm -f "$TEMP_DIR/$f"` where `$TEMP_DIR` resolves
empty becomes an `rm` on a root-relative path, which Claude Code flags as a
dangerous-rm and PROMPTS for confirmation EVEN under
`--dangerously-skip-permissions` (the fleet launch mode). An autonomous agent
cannot answer its own dialog, so the loop looks alive (`agent-state=RUNNING`)
while hanging at zero progress until a human intervenes — an agent hung 46+ min
this way (observed 2026-07-09, cc-05). The guarded helper eliminates the whole
hand-rolled-rm class: there is exactly one purge path, and it fails loud
(non-zero exit, deletes nothing) rather than ever issuing a dangerous rm.
Regression-guarded by `core/scripts/tests/test_temp_drain_purge.sh` (8 guard
cases + a dry-run smoke + purge-lane behavior tests that assert the two lanes
— ephemera extensions and 0-byte empties — purge while content-docs, fresh
files, and `drained/` contents are excluded, run against the SSOT
`_purge_find_predicate` function).

**General rule (applies beyond temp):** any framework guidance that has an agent
construct an `rm` on a variable path MUST guard the variable (set + non-empty +
expected-shape) first, or route through a helper that does. An unguarded `rm` on
a possibly-empty variable is an agent-hang hazard, not just a data-loss hazard —
the dialog it triggers cannot be answered by an autonomous agent.

## The third class: promotion threshold + durable home (D2 decision)

Both lanes above are **allow-lists**. Drain matches `.md`/`.json`; purge matches
eight named extensions plus 0-byte. Everything else is the COMPLEMENT of two
enumerated sets, so the third class is unbounded *by construction* — not by
oversight. An extension list can never close it, because its members are
invented per-goal: measured on `cc-02` 2026-07-31, 70 third-class files carried
**21 distinct suffixes**, 8 of them one-offs a single goal made up (`.premutation`,
`.pre2`, `.mutated`, `.mine`, `.bak-preiam-cutover`, `.12`, `.test`, `.patch`).
Enumerating those would have been a fresh list, stale on the next goal.

### (a) The load-bearing threshold

An artifact is **load-bearing** iff BOTH hold:

1. **Cited** — at least one durable record (tree node, RB entry, guardrail,
   experience, convention, goal) references it by path. `core/scripts/temp-citation-ratchet.py`
   already computes these `(record, path)` pairs; no new counter is needed.
2. **Un-inlinable** — its content cannot be folded into the citing record.

Threshold is **≥1 citation, not ≥2.** This is forced by measurement, not chosen
for leniency: `artifact-reference-integrity.md` (D3) measured inbound-reference
counts for *moving* artifacts at **median 1, max 4**, with 283 of 305 carrying
exactly one. So a threshold at 2 retains 22 of 305 (7%) and a threshold at 3
retains 4 (1.3%) — either discards the overwhelming majority of genuinely-cited
artifacts to buy a distinction the data does not support. Above 1 there is
almost no distribution left to threshold on, which is why the citation test is
a **boolean** (cited at all?) rather than a tunable count.

**Age is explicitly rejected as the gate** (guard-2071: an age-gated evictor
cannot bound a rate-driven store). Measured accrual here is **6.1 files/day**
(43 of the 70 arrived within 7 days; 0 exceeded 30 days). Any age-only gate with
window *W* leaves ~`6.1 × W` files resident — **~184 at a 30-day window**. The
bound must come from the PREDICATE (what is eligible to persist), never from the
window. The current apparent boundedness — 0 files older than 30d despite no
matching purge lane — comes from **ad-hoc manual gardening by whichever agent
notices**, which is not a mechanism and must not be cited as one.

Everything textual folds. Per D3, FOLDING — dissolve the pointer, inline the
detail — is the sanctioned remedy, and `.jsonl`/`.yaml`/`.xml`/`.tsv` (50 of the
70 measured) all fold into a citing node. Folding is the DEFAULT; promotion is
the exception.

### (b) The durable home — and why it is not a new directory

**No new store, and no new top-level entry.** D3 already decided the neighbouring
question with "no new node type, no new store, no schema change," and the L1
path-resolution hook refuses a new top-level entry under any governed root
regardless (`.claude/rules/path-resolution.md`) — so inventing `world/artifacts/`
is not available even if it were desirable.

The home is a **receipted directory**: `agents/<agent>/temp/<slug>/` containing
the artifact plus a top-level `RECEIPT.*`. This is not a new mechanism — Lane 3
(`cleanup_stray_dirs`) **already** preserves exactly this shape, skipping any
stray dir carrying a top-level `RECEIPT.*` or `.archive-marker`
(g-115-2962/guard-1377).
Promotion is therefore "wrap the file in a receipted dir," not "build a home."

The receipt's EXTENSION and CASE do not matter, and that was not always true:
until g-115-3397 (2026-08-08) both this paragraph and the Lane-3 reader named
`RECEIPT.md` exactly — a filename **no** producer in the tree writes
(`_seed_engine.py` → `RECEIPT.json`; `history_vacuum_archive.py` → lowercase
`receipt.json`), so the preservation this section promises was unreachable by
every archive the framework creates. `_has_archive_receipt` is now the SSOT
predicate and matches `RECEIPT` / `RECEIPT.*` case-insensitively at top level
only. Write whichever extension suits the payload; do NOT rely on a nested
receipt or a `*receipt*`-ish filename — neither is preserved, deliberately.

The `RECEIPT.*` is what keeps this from repeating the `reports/` freeze. That
directory was frozen (see § Migration) because it became *a second, disconnected
retrieval surface* — invisible to `/prime` and `retrieve.sh`, so its contents
were lost knowledge regardless of which folder held them. The failure was never
the location; it was that **nothing pointed in**. A receipted dir is sanctioned
ONLY while criterion (1) holds: a durable record cites it, so it is reachable
THROUGH the retrieval surface rather than being a parallel one. An uncited
receipted dir is the reports/ defect wearing a marker, and the receipt must name
its citing record.

### What this measures out to, and the residual defect

On `cc-02` 2026-07-31, of 70 third-class files exactly **one** was genuinely
un-inlinable (`g-335-258-archive.tar.gz`; the only other binary was `ruff` inside
a stray vendored virtualenv, a Lane 3 stray-dir case, not a file case). That one
file is **cited by nothing** — grep across the agent tree, `core/config`,
`.claude`, `world/`, and `meta/` returned zero references. So the population
actually needing a durable home here is **zero**, and the correct action is to
build nothing.

Do not read that as universal. The class composition is **deployment-dependent**,
and that is measured, not speculative. A downstream deployment censused its own
temp/ root on 2026-07-30 (`g-029-87`, recorded in `core/scripts/precheck-eval.py`)
and found 26 files where the pressure metric reported 7 — a 3.7x undercount whose
missing 19 were **6 `.pdf`, 4 `.yaml`, 4 `.docx`, 2 `.jsonl`, 1 `.ps1`, and 1
extensionless**. That deployment's un-inlinable set is genuinely non-empty
(`.pdf`/`.docx` fold into nothing), where this box's is a single uncited tarball.
Same class, opposite population — which is exactly why the rule is written on
FOLDABILITY rather than on any one box's extension census.

That goal reached the same verdict from the metrics side and is worth quoting,
because it is independent confirmation rather than an echo: unclassified files are
"not drain-drainable and not purgeable, so counting them toward the drain threshold
would fire drain goals that cannot drain them. **Visibility is the fix; changing
threshold semantics is not.**" `unclassified_count` is that visibility. This
decision supplies the half it deliberately left open — what to DO with the files
once seen.

Note the asymmetry that makes the one measured artifact interesting: it is an
archive-before-delete archive — precisely what guard-1377's preservation exists
to protect — and the preservation could not see it, because `cleanup_stray_dirs`
matches `-type d` only. A durable artifact is protected if it is a DIRECTORY and
unmanaged if it is a FILE.

**The residual defect was retention, not addressing.** D3 reached that same
sentence from the reference-integrity side; this decision reached it independently
from the lifecycle side. That work — inverting Lane 1's predicate so the
complement is purgeable-by-default with the load-bearing set exempted — **landed
in g-306-111** (`_purge_find_predicate`, plus the class table above).

Three things measured during that change are worth carrying forward, because
none was predicted by the decision above:

1. **Cited paths are not all literal.** 4 of 64 live cited paths are WILDCARDS
   (`…/temp/g-335-531-*`, `…/temp/mergeback-*.json`) — durable records cite a
   FAMILY, not a file. The exemption honors them: escaping to a literal would
   match nothing and delete the very family the citation names. The one form
   that is refused is a pattern matching ANY filename (`*`), which a single
   citation could otherwise use to exempt everything and silently revert the
   inversion. That case warns on stderr and is dropped.
2. **The lane fails CLOSED, not open.** When the cited set cannot be determined
   (unmounted world, missing script), `temp-citation-ratchet.py --cited-paths`
   exits 2 rather than printing an empty list, and Lane 1 degrades to the
   pre-inversion allow-list — never to purge-everything. "Unknown" and "nothing
   is cited" must not render identically when the consumer DELETES on the
   answer. The run reports which happened via `citation_lookup` in its JSON.
3. **Measured blast radius on the first box (`cc-03`, 2026-07-31):** 252 →
   264 would-purge, i.e. **13 files newly exposed** (9 `.jsonl`, 1 `.mjs`,
   1 `.yaml`, 1 `.tsv` — all raw IO dumps), and **1 file newly PROTECTED**
   (`g-335-531-residue-classify.py`, saved by a cited-family wildcard that the
   old allow-list would have deleted as a `.py`). The inversion is therefore
   not purely more-aggressive: it also stops orphaning cited evidence.

## Searching temp/

"Search through temp and find that thing" needs no dedicated tool: temp/ is a
flat directory of text files on the local disk, so the agent's own `Grep` /
`Glob` over `agents/<agent>/temp/` IS the search surface. On a multi-machine
setup the continuity pull (above) materializes the agent's temp/ locally, so the
same local `Grep` works on any machine.

There is deliberately NO `temp-search` daemon endpoint: a second search path
would violate single-source-of-truth (two ways to find the same file) for zero
gain over `Grep`, and cross-agent temp peeking is an anti-pattern — agents
coordinate through `world/board/`, not by reading each other's temp/ (see
`core/config/conventions/coordination.md`). Each agent searches its OWN temp/.

## The agent-dir write-surface allowlist

The canonical map of where an agent may write under its own dir, enforced
at write time by the Phase-4 hard gate
(`core/scripts/path-resolution-hook.py`, PreToolUse[Write|Edit|MultiEdit]).

| What you are writing | Write to |
|---|---|
| Reusable domain knowledge, lessons, patterns | Knowledge tree (`world/knowledge/tree/`) — the only retrieval surface |
| Experience traces (narratives of what happened) | `experience/` |
| Daily journal entries | `journal/` |
| Session state (signals, working memory, handoff) | `session/` (registered in `session-manifest.yaml`) |
| Per-session IO buffers / probe dumps | `session/scratch/` |
| Per-session binding metadata | `sessions/<SID>/` |
| Operational telemetry / diagnostic time-series (phase-costs) and delta baselines (`last-outcome-snapshot.yaml`) | `session/` — machine-local, never knowledge, regenerable |
| Completion-report dashboard (latest pointer) | `COMPLETION-REPORT.md` at the agent root (git history is its archive) |
| **Analyses, briefings, audits, design docs, snapshots** (working docs that DRAIN to the tree) | **`temp/`** — the home for the working docs that used to scatter into `reports/` |
| Agent identity, config, aspiration queue | The registered top-level agent files only (`self.md`, `*.jsonl`, `*.yaml`, …) |

**Permitted top-level directories** under `agents/<agent>/`:
`session`, `sessions`, `journal`, `experience`, `.history`, `temp`.

Anything else — `reports/`, a newly-invented directory, or a stray
top-level file not on the registered list — is denied by the Phase-4 gate
with a redirect here. The gate is an ALLOWLIST: it permits the canonical
locations and denies everything else, rather than blacklisting specific
names. This is deliberate — it teaches that there is a place for each kind
of output, and inventing a new location is the error to avoid.

## Gate behavior (Phase 4)

The allowlist gate lives in `core/scripts/path-resolution-hook.py`
(PreToolUse[Write|Edit|MultiEdit]). For a write under the bound agent dir:

1. The first path segment under `agents/<agent>/` is extracted.
2. If it is one of the permitted directories — ALLOW.
3. If the target is one of the registered top-level agent files — ALLOW.
4. Otherwise — DENY with an educational message listing the routing table
   and redirecting to `temp/`.

`reports` is not named in the deny logic; it is denied because it is not on
the allowlist — the same mechanism that denies any future invented
directory.

## Migration (reports/ → temp/ → removed)

`agents/<agent>/reports/` no longer exists. It was the legacy slush pile for
working docs (analyses, fresh-eyes reviews, phase plans); the file-model
normalization replaced it with `temp/` (a staging area that DRAINS to the
knowledge tree) plus this allowlist gate. reports/ was briefly retained as a
FROZEN git-tracked archive — then **removed entirely** (user-directed,
2026-06-02): all 665 legacy files across the 6 agents were `git rm`'d and the
directories deleted. **Git history is the archive** — every removed file stays
recoverable via `git log` / `git show`, so nothing is lost.

Writers were repointed in the earlier Phase 3 BEFORE the gate activated —
briefings (`fresh-eyes-*`, `felt-sense-*`) write to `temp/`; phase-cost
telemetry and the completion delta-baseline write to `session/`; the
timestamped completion-report archive was dropped (the `COMPLETION-REPORT.md`
pointer's git history is its archive). So no live writer targets reports/.

**Why removed, not kept frozen (2026-06-02 override).** The earlier Phase-6
frozen-archive compromise had already established that the legacy corpus was
overwhelmingly already-captured or stale — goal-closure artifacts whose learning
was encoded into the tree at closure, or superseded proposals. The ~26 tree-node
citations that previously resolved to on-disk reports files have been folded: the
dangling pointers were removed and any essential detail inlined into the node,
with the source marked git-archived. With the corpus encoded-or-stale and the
citations folded, the on-disk archive no longer earned its keep; removing it
eliminates a second, disconnected retrieval surface entirely. git history
preserves the bytes — `git show <rev>:agents/<agent>/reports/<file>` recovers any
one of them.

The Phase-4 allowlist gate still DENIES `reports/` (it is not on the permitted
list), which prevents the directory from being silently recreated. There is no
agent-side override — a genuinely needed write goes to `temp/` (working docs) or
`session/` (operational state). A bulk historical recovery, if ever needed, uses
`git checkout`/`git show` of the removed paths.

## Cross-references

- `core/config/conventions/learning-routing.md` — the full "where does this
  learning go?" decision tree across all stores
- `core/config/session-manifest.yaml` — `session/` file tiers (`temp/` is
  outside `session/` and is not manifest-governed)
- `core/config/conventions/session-state.md` — two-tier session layout
- `.claude/rules/path-resolution.md` — L1 path governance and cruft prevention
