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
