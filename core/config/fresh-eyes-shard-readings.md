# Fresh-Eyes Shard-Ordering Readings

Dated per-shard readings of the fresh-eyes series file, extracted from
`.claude/skills/fresh-eyes-review/SKILL.md` on 2026-08-24 (hot-path size gate,
g-115-6690 — that skill is 76 KB against a 65,536 B injection ceiling, so its
tail was already arriving TRUNCATED and every dated reading added to it made
the truncation worse).

**The skill keeps the METHOD. This file keeps the EVIDENCE. Append new readings
HERE, never there** — the same split as `core/config/felt-sense-readings.md`,
`core/config/run-full-suite-baselines.md` and
`core/config/strategic-scan-readings.md`.

The standing imperative, which stays in the skill: there is NO fleet-wide "top"
or "tail" ordering. Both "read the TOP" and "read the TAIL" have each been wrong
for some agent at some date. Derive N as the MAX over section headings, with a
case-INSENSITIVE exclusion of forward-reference headings. A per-shard ordering
claim is a DATED observation about ONE file, and shards get restructured without
announcement (guard-3487).

---

⚠ THE SHARDS HAVE DIVERGED — THERE IS NO FLEET-WIDE "TOP" OR "TAIL". This
line read "Read its TOP entry" until 2026-08-12, and guard-3312's action_hint
still asserted the shards are "newest-FIRST". Measured that day (bravo, cc-05,
all five shards): alpha IS newest-first (N=66 at line 129, archived rows at
1031) — so the old instruction was right for the agent who wrote it — while
bravo (N=18..N=41) and foxtrot (N=38..N=42) are OLDEST-first with the newest
point at the TAIL, and echo/zeta are index/rollup splits in a third shape
entirely. So `sed '1,140p'` returns alpha's NEWEST row and bravo's OLDEST
rows, silently, and both look like a series. Note the bravo-specific
"read the TAIL" correction recorded at N=40 is the same mistake mirrored —
do not adopt it fleet-wide either.
⚠ RE-MEASURED 2026-08-24 (foxtrot, `hostname` LAPTOP-3IOFCNEO, `uname -r`
6.18.33.2-microsoft-standard-WSL2). The 2026-08-12 block above is preserved as the
dated record it is; this is what the SAME probe reads twelve days later, and one
shard has changed SHAPE:
  bravo    — 23 `## N=` entries, N=59..N=81 ascending in file order: OLDEST-first,
             newest at TAIL. UNCHANGED from 2026-08-12.
  foxtrot  — 19 entries, file-FIRST is `N=66`, file-LAST is `N=65`, MAX is `N=72`:
             **NEITHER top nor tail.** A front block carries N=66..N=72 and an
             older block N=54..N=65 follows it. The 2026-08-12 "foxtrot is
             OLDEST-first" reading is NO LONGER TRUE, and following it costs an
             off-by-SEVEN: the tail returns `## N=65` with a `HANDOFF to N=66`
             beneath it, so the pass numbers itself 66 and overwrites the
             successor slot of six existing entries. That happened ON this pass
             and was caught only by positive-controlling the probe's answer (72)
             against the tail's (65) — the probe was right and the shortcut wrong.
  alpha/echo/zeta — ZERO `## N=` headings; they carry their indices in the other
             shapes this block already documents. So an ordering classifier that
             diffs file-order against sorted-order compares two EMPTY lists, finds
             them equal, and reports a confident "OLDEST-first" for all three.
             That vacuous pass is guard-1922's shape — measured this pass, the
             classifier did exactly that. Do not read it as a measurement.
STANDING LESSON, now with a second independent confirmation: a per-shard ordering
claim is a DATED observation about ONE file, and shards get restructured without
announcement. guard-3487 is why bravo is reported above as re-measured-unchanged
rather than assumed to have moved with foxtrot — a defect found on your own shard
may describe only your shard. The MAX-over-headings probe below is the only form
that survived both measurements; both "read the TOP" and "read the TAIL" have now
each been wrong for some agent at some date.

⚠ ADDITIVE RE-READ 2026-08-24 (foxtrot, `hostname` LAPTOP-3IOFCNEO, `uname -r`
6.18.33.2-microsoft-standard-WSL2), N=74. The 2026-08-24 block above is preserved
as the dated record it is (guard-683); this updates only what MOVED, and scopes the
claim to the one shard actually re-measured (guard-3487):
  foxtrot  — `MAX is N=72` above is now **STALE BY ONE**: the probe read **73** at
             N=74's Phase 2.0a, and this pass appends **74**. The BLOCK BOUNDARY did
             NOT move — the front block simply ABSORBED N=73 (now N=66..N=74), and
             the older block N=54..N=65 still follows it. So the SHAPE finding above
             holds exactly as written; only its MAX figure ages, and it ages by one
             per fire. A future pass should expect to bump this number, not to
             re-derive the layout.
  bravo / alpha / echo / zeta — NOT re-measured this pass. Their rows above stand
             as their own dated observations; do not infer they moved with foxtrot
             (that inference is precisely what guard-3487 forbids).
STANDING NOTE FOR THIS FILE: a `MAX is N=<k>` figure is a MOVING value recorded in a
FROZEN block — guard-2516's shape. The durable content here is the SHAPE (which end
of the file the newest entry sits at, and whether the shard is multi-block); the MAX
is a timestamp in disguise. Read the shape from this file; read the MAX from the
probe, every fire.

- **2026-08-28, bravo/cc-05 (Linux 6.8.0-137-generic), N=101.** Three-branch probe returned **100** on BOTH the read-time and the write-time run (g-115-8055), authoritative `backend-cat.sh` copy 61,310 B, byte-identical to the `$WORLD_PATH` mirror. Positive-controlled against the ROWS, not the shard-index table (guard-2421): `grep -n 'N=' | tail` showed `## N=100 — 2026-08-28T18:13` as the last section heading, agreeing with the probe. Shard layout: **oldest-first, newest-LAST** — the section tail is the newest point, so append at EOF. Post-append size **69,124 B**, which at the head banner's measured 2.579 B/token is ~26.8k tokens and therefore **already past the ~25k Read cap**; the inherited "distill advisory ~N=107" is stale and the fold is due at N=102 (recorded in that shard's HANDOFF (g)).

### 2026-08-31 — echo shard, read by fresh-eyes N=117 (echo, cc-03, Linux 6.8.0-137-generic)

Three-branch probe returned **116**; positive-controlled against the rows themselves
(`grep -n 'N=' | tail -8`), which showed `| **N=116** 2026-08-31 17:03` as the last table row.
Layout: **oldest-first, table rows** (`| **N=NN** DATE TIME | ... |`), carve stubs interleaved
in the same table, plus prose sections for N=110/111/112 BELOW the table. Heading-only greps
still see nothing on this shard — branch 3 (first `N=` token per table row) does all the work,
as the skill records.

Two corrections a successor should carry:

- **The shard-index cell was stale again.** It reads `104–` while N=116/N=117 sit in the tail.
  Third recorded instance of the same hand-maintained-cell defect. Anchor on the ROWS.
- **N=116's own row miscounted the live points** ("N=113/114/115/116 = 4"). N=113 had been
  carved by that same pass, so the table held 3. A row's self-reported survivor count is not
  an anchor either — count the `| **N=` rows.

Authoritative read used throughout (`backend-cat.sh cat`), re-run at write time per g-115-8055:
MAX_N was still 116 at write time, so N=117 was allocated with no peer collision. Node
56,127 B pre-carve → 52,798 B after carving N=114 + N=115 and appending N=117.

### zeta shard, N=129 (2026-09-02, cc-02)

Probe returned MAX **128** at read time and **128** again at write time (g-115-8055
re-probe) — no peer allocated in the gap, so N=129 was safe. Positive-controlled
FROM THE ROWS, not from the shard-index table: this shard is a newest-LAST table,
so branch 3 (`^\|` + first `N=` per row) is the branch that carries the answer;
the heading branch tops out at N=34 on stale section headers and would have
allocated N=35 over 94 live rows. Anyone reading "MAX SECTION HEADING" literally
on this shard gets a wrong-but-well-formed N.

Shard size: 279,756 B at read time, **290,719 B** after the N=129 row. That is
**zero growth in the 21.6h between fires** — N=128's "+9.1 kB in 3.4h = 2.7 kB/h,
2.8x rate increase" measured N=127's OWN ROW divided by a shrinking Δt, and its
per-hour extrapolation predicted +58 kB over this interval against an actual 0 B.
Shard growth is per-FIRE. Quote row bytes, never kB/h.

Write-path note that cost a round trip: the row was first appended with a Python
file-append, which is NOT Write/Edit/MultiEdit, so `owncloud-push-on-write` never
fired and the row was LOCAL-ONLY — authoritative read-back showed 279,756 B and
`N=129 present: 0` against a 289,485 B mirror. Recovered by re-doing it through
the Edit tool plus `owncloud-flush.sh` (pushed=5), then verifying
authoritative == mirror == 290,719 B. After any non-tool write to a governed
root, re-read authoritatively and diff the byte count.

### zeta shard, N=130 (2026-09-02, cc-02)

Probe returned MAX **129** at read time and **129** at write time (g-115-8055
re-probe) — no peer in the gap; N=130 allocated. Positive-controlled FROM THE
ROWS: the last three `| ... N=127 / N=128 / N=129` rows, in date order, on a
newest-LAST table (branch 3 carries the answer, as N=129 recorded).

Shard **291,808 B** at read time — **+1,089 B over N=129's post-row 290,719 B
with NO row added in between** (a post-write clause plus a front-matter
refresh). So a file delta between fires is not "growth per fire" either; quote
the ROW bytes, and expect the file to move a little without a row.

Two measurement notes from this fire, kept here rather than in the row:

1. A date-only `completed_date` (`2026-09-02`, no time) on `g-369-104` drops the
   record out of every sub-day interval count — my first "lane closes since
   N=129" read **1** where the records show **2**, and the missing one was the
   lane close I made myself. When carrying Rule 26's flow half, count by stable
   identity (guard-2828) and check the stamps for date-only values before
   trusting an interval count.
2. `msg-20260810-125438-alpha-5078` (tags self-md + zeta, unread 23d, OUTSIDE
   the 2.3b self_evolution/self-drift tag filter) named a dead guard-013 pointer
   at self.md L278. `grep` returns zero guard-013/014 matches in
   `agents/zeta/self.md` today, so it was marked read with that reason. A
   directed self-md finding can sit outside the tag filter indefinitely; sweep
   the `self-md` tag once per fire as a cheap second net.

The Read tool refuses this shard WITHOUT an explicit `limit` once the file is
past 256 KB ("File content (285KB) exceeds maximum allowed size") — pass
`offset` AND `limit` (one line is enough for the row region) or read-before-edit
cannot be satisfied at all.

Measured after the push: authoritative == mirror == 295916 B; the N=130 row is **4,108 B**
(the row itself says "~3.6 kB" — written before it was measured, the same
self-report-changes-the-size gap N=129 recorded; the bound held at ~4 kB, 2.7x
smaller than N=129, and the ledger, not the row, carries the true figure).

### foxtrot shard, N=97 (2026-09-02, foxtrot-laptop, WSL2 6.18.33.2)

Probe returned MAX **96** at read time and **96** at write-time re-probe (g-115-8055) — no peer in the gap (foxtrot is this shard's only writer by design); N=97 allocated. Positive-controlled FROM THE ROWS: the `## N=96 — 2026-09-02T15:47:35` heading is the newest section on a newest-LAST shard, and its table's N=92..N=96 columns agree.

Per-branch readings on this shard: branch 1 (section headings, HANDOFF headings excluded by `-vi`) = 96 and CARRIES the answer; branch 2 (`| **N=` bold rows) = none; branch 3 (first `N=` per table row) = 92 — the comparison tables put the OLDEST column first, so a row-only probe would allocate N=93 here and collide with an existing section. Keeping all three branches and taking the max across them is what makes the foxtrot shard safe; the "first N= per row" rule is harmless only because branch 1 outranks it.

Shard 299,375 B / 4,482 lines authoritative at the write-time re-probe. The mirror was NOT compared before the write: the comparison command doubled the `world/` prefix (`$WORLD_PATH/world/...`), `cmp` returned rc=2 on the missing path, and the loop printed `auth!=mirror` — a wrong-path negative that reads exactly like a real divergence (the guard-2298 shape on a byte comparison; judge a cmp by its rc, 2 is 'could not compare', not 'different'). Measured AFTER the write with the correct path: the N=97 row is **7,685 B**, the mirror 307,618 B / 4,614 lines (the Edit-tool PostToolUse hook re-formatted the file, so the file delta 8,243 B is not the row), and authoritative == mirror after the push.

### foxtrot shard, N=98 (2026-09-03, foxtrot-laptop, WSL2 6.18.33.2)

Probe returned MAX **97** at read time (01:39) and **97** at the write-time re-probe (01:43:18, g-115-8055) — no peer in the gap (foxtrot is this shard's only writer by design); N=98 allocated. Positive-controlled FROM THE ROWS: the `## N=97 — 2026-09-02T19:04:51` heading at line 4485 is the max section heading, and the N=98 section was appended after its `### HANDOFF to N=98` block (line 4614 was the file's last line).

Per-branch readings on this shard: branch 1 (section headings, HANDOFF headings excluded by `-vi`) = 97 and CARRIES the answer; branch 2 (`| **N=` bold rows) = none; branch 3 (first `N=` per table row) = 93 — the comparison tables' first column, which would have collided with the existing N=94 block had it been taken alone.

Shard 307,618 B / 4,614 lines authoritative at the write-time re-probe; the local mirror measured the same 307,618 B in the same call (no doubled `world/` prefix this time — `$WORLD_PATH/knowledge/tree/...`).
