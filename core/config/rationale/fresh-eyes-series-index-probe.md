# Rationale: fresh-eyes-review Phase 2.0 Series-Index (N) Probe

Referenced from `.claude/skills/fresh-eyes-review/SKILL.md` Phase 2.0. Why the
N-allocation probe has three branches, why each filter is case-insensitive and
position-anchored, why it reads the authoritative store rather than the mirror,
and why it must be re-run at WRITE time. Every defect below returns a
wrong-but-well-formed N, which reads as plausible rather than as an error.

## Why there is no fleet-wide "top" or "tail"

Measured twice, 12 days apart, all five shards: `sed '1,140p'` returns one agent's
NEWEST row and another's OLDEST, and both look like a series. One shard is now
NEITHER — an off-by-SEVEN that overwrote six successors' slots — and an ordering
classifier passes VACUOUSLY on the shards with zero `## N=` headings (guard-1922).
Yours may describe only yours (guard-3487). "Read the TOP" and "read the TAIL"
have EACH been wrong for some agent at some date. Dated readings, verbatim:
`core/config/fresh-eyes-shard-readings.md`.

## Why the forward-reference exclusion must be case-insensitive

It was `-v` until 2026-08-16. Fleet agents write the pre-registration heading in
more than one casing: bravo writes `### HANDOFF to N=k` in caps, which the
case-SENSITIVE `Handoff to N=` filter does not match, so the forward reference
leaks into the MAX and the probe returns the NEXT pass's number. Measured that day
on bravo's shard, same file, same run, only the flag differing: `-v` -> **57**,
`-vi` -> **56**, against a true newest entry of `## N=56`. The failure is silent
and off-by-one in the direction that makes the briefing overwrite its own
successor's slot.

This is the SAME case lesson Phase 2.3b's (a-pre) block measured at length ("MATCH
THESE CASE-INSENSITIVELY... 287 lowercase vs 40 capital") — learned there, never
propagated to this sibling filter in the same file. When you fix a casing
assumption in one filter, grep the file for the others. guard-2653 is about WHAT
to exclude; this is about the MATCH surviving how five different agents actually
type it.

## Why three branches, and why NOT a single union regex

Heading-only was wrong for echo by THREE on 2026-08-17 (returned 15 against a true
71) because echo writes rows as TABLE ROWS (`| **N=68** …`), which no heading grep
can see at any casing.

Do NOT "simplify" this to a single union regex: the obvious form
`^(#{1,4} |\| \*\*)N=[0-9]+` requires `N=` IMMEDIATELY after the prefix and so
returns EMPTY for alpha and zeta, whose headings carry other text first. Measured
that day across all five shards, both forms — the union regressed **2 of 5 to
nothing** while the max-of-both regressed none.

VERIFIED against ground truth 2026-08-18 (zeta, `hostname` cc-02, `uname -r`
6.8.0-137-generic), all five shards, old two-branch vs new three-branch, same run:
alpha 83→83 · bravo 61→61 · echo 73→73 · foxtrot 57→57 · **zeta 34→79**. Zero
regressions, zero over-matches, and zeta corrected by **45**.

## Why the third branch takes the FIRST `N=` per row

A row's own index appears FIRST (in its date cell or at the head of its verdict
cell); any other `N=` later in the same row is prose referring to a DIFFERENT
point — including a FORWARD one. Measured 2026-08-18 (zeta, cc-02): a
max-within-row variant read echo as **74** when echo's newest row is **73**,
because echo's own N=73 row names N=74 in its body. guard-2653's `handoff to N=`
filter does not catch that phrasing and cannot be widened to (the forms are
unbounded prose) — position is the reliable discriminator, not wording.

## Why a wrong diagnosis kept this unfixable for a day

The prior note here read "zeta has 0 table rows, so its shape is a third one
neither branch reads" and left the probe alone. **zeta has 111 table rows.** Its
index is an INLINE token inside a cell — `| 2026-08-17 21:01 | … | **N=79 — THE
N=77 RECOVERY…** |`, and in older rows `… ** N=74, fresh-eyes, …` — so the cell
does not START with `N=`, which is the only thing branch 2 can see.

"No rows" pointed at a missing shape (nothing to do); "rows whose N= is not at the
cell head" points at a missing MATCH (one awk). A correct diagnosis was available
from `grep -cE '^\|'`, one command, and the note stood for a day. The 34 it
returned is a stray prose token, which is why it reads as plausible rather than as
an error — a wrong-but-well-formed N is the dangerous shape.

## Why the shard-index table is not a positive control

It is a hand-maintained prose cell with no writer and no check. echo's read
`| *(this node)* | 68– | (empty — next row lands here) |` for three consecutive
fires while N=68/69/70 sat in the tail directly below it — so using it as the
fallback anchor is precisely what produced the off-by-three. The trustworthy
anchor is the ROWS THEMSELVES. A wrong index is embarrassing; the real cost is the
wrong PRIOR POINT it carries into Decision Rule 11 — a wrong drift score and
therefore a wrong verdict.

## Why N is allocated at WRITE time (g-115-8055)

This line read the local mirror until 2026-08-27. Two independent defects, and
fixing only the first leaves the collision intact:

1. **SOURCE.** Under own-cloud `$WORLD_PATH` is a read-through cache, so the
   allocation could be computed from stale bytes (guard-157: default NO mirror,
   read the single authoritative source). `backend-cat.sh cat` is a pure
   to-memory authoritative read.
2. **SHELF LIFE — the load-bearing half.** N is a value DERIVED from a read, and
   the write happens many minutes later at Phase 8. `owncloud_backend` fences
   every PUT with `PutObject(IfMatch)`, which proves no LOST UPDATE and says
   NOTHING about whether the allocated VALUE is unique: box A reads 88 -> mints
   89 -> writes (fence passes); box B read 88 before A's write, mints 89, and B's
   write ALSO passes because `locked_rmw` re-reads and B's ETag is current at B's
   OWN write. Two N=89 sections in a file that stayed internally consistent
   throughout — so every drift and integrity probe reports `[match]` and nothing
   sees it. That is exactly how the 89a/89b collision was minted. (guard-5322,
   guard-1876: a measured verdict about mutable data has a shelf life.)

## Historical: the alpha shard's in-file ORDERING block (relocated 2026-09-10, N=148)

Relocated VERBATIM out of the header of
`world/knowledge/tree/system/directive-lane-compliance/directive-lane-series-alpha.md`
(5,586 B), where it had been paying rent on every fire of a size-budgeted shard while
this file was already its documented home. The N=146 and N=147 handoffs both named this
relocation as the cheapest structural trim available; N=148 executed it. This is a MOVE,
not a deletion — nothing below is new, and the shard now carries a one-line pointer here.

Its lasting value is a worked catalogue of four different wrong-but-well-formed N probes,
each of which read as plausible rather than as an error. Note the `N&#61;` escapes are
deliberate and load-bearing: they keep this text from matching the very probes it
documents (`guard-1238` — never probe with a pattern the probe itself contains).

> **⚠ ORDERING — still take the MAX, but the two-region hazard is GONE.**
> **One command, unchanged and still correct:**
> `grep -oE 'N=[0-9]+' <this-file> | sed 's/N=//' | sort -n | tail -1`
>
> **SUPERSEDED 2026-08-05 (alpha, cc-04) by the keep-newest-6 rollup below.** The
> warning this block replaced described **TWO append regions in OPPOSITE orders**
> (head newest-first N=35…N=8; tail oldest-first N=20…N=30) and told you to page to
> EOF for the tail. **That tail region no longer exists** — the rollup archived
> N=32 and older, leaving ONE region, newest-first, N=38…N=33. Its stated size
> (136,763 bytes / 1,948 lines / 2.27× the Read cap, "a Read returns lines 1–728
> and stops") is likewise stale: the file now reads whole. Following the old text
> today would send you hunting for N=20–N=30 in a file that no longer holds them.
> The full pre-rollup text survives verbatim in
> `world/knowledge/archive/directive-lane-series-alpha-2026-08-05.md`.
>
> **⚠ CANONICAL READ — PARENTHETICAL-ANCHORED. Corrected 2026-08-12 (alpha, N=66);
> the 2026-08-11 heading-anchored form below it is BROKEN and was broken on its own
> first real test.**
> **`grep -oE '^### Reading at [^(]*\(alpha, N&#61;[0-9]+' <file> | grep -oE '[0-9]+$' | sort -n | tail -1`**
>
> Substitute your own agent name for `alpha`. `[^(]*` cannot cross the opening paren,
> so the match stops at the FIRST `N&#61;` — the one inside the provenance parenthetical
> `(alpha, N&#61;NN, since ...)`, which is the only place an entry's own index appears.
>
> **The superseded form was:**
> `grep -oE '^### Reading at .*N&#61;[0-9]+' <file> | grep -oE '[0-9]+$' | sort -n | tail -1`
>
> **It fails because `.*` is GREEDY, so it matches the LAST `N&#61;` on the heading line,
> not the entry's own.** Measured 2026-08-12 the moment N=66 was appended: that heading
> cited the prior index twice in its summary (`N&#61;66 N&#61;65 N&#61;65` in line order), and the
> command returned **65** — the entry that had just been superseded. The failure direction
> is the OPPOSITE of the whole-file form's and worse: the whole-file form reads too HIGH
> (skipping an index), this one reads too LOW, so the next fire numbers itself 66 again,
> collides with a live entry, or concludes a fire already happened that never did.
>
> **This falsifies the sentence the 2026-08-11 fix rested on** — *"The heading-anchored
> form reads only entry HEADINGS, so prose cannot reach it."* Prose INSIDE a heading
> reaches it, and every entry in this series writes a summary into its own heading, so
> the exposure is structural rather than incidental. The fix narrowed WHICH LINES are
> read and left the within-line greediness untouched; `guard-1238`'s class (never probe
> with a pattern the probe itself contains) applies to the line as much as to the file.
> The parenthetical anchor closes both, and its one dependency — the
> `### Reading at ... (agent, N&#61;NN, since ...)` provenance shape — is a shape every
> entry already writes.
>
> Cross-check by eye against the newest heading when it matters. Positive control on
> this file at the time of the correction: the fixed form returns `66 65 64 62` for the
> four newest entries; the broken form returned `65` as the max.
>
> The whole-file MAX below (`grep -oE 'N&#61;[0-9]+' <file>`) is **not reliable and cannot
> be made reliable by discipline.** It matches the token ANYWHERE, and this file is a prose
> series *about* N-numbers — so any entry that cites a future, hypothetical, or partner's
> index silently raises the answer. Measured in one sitting: this fire wrote a forward
> reference to the next index in its own narrative and the command returned **one too high**;
> the warning written to prevent that recurrence cited two indices as examples and drove the
> answer **two too high**. The fix that removes the citation removes the warning's own
> teaching value — which is the proof the predicate, not the prose, is what is broken.
> This is `guard-1238`'s class (never probe with a pattern the probe itself contains), the
> same shape as needing the bracket in `pgrep -af "[r]un-full-suite"`.
>
> Consequence if you use the old form: you number yourself one or more past the real newest
> entry, **skipping an index silently**, or conclude a fire already happened that never did.
> The heading-anchored form reads only entry HEADINGS, so prose cannot reach it. Its one
> dependency is that the `### Reading at ...` heading shape holds — keep writing headings in
> that shape, and cross-check against the newest heading by eye when it matters.
>
> **Why the MAX command is kept anyway:** it was never a workaround for the two
> regions — it is the only read that cannot be fooled by *which end grew last*, and
> that hazard returns the moment anyone appends at the wrong end. The incidents it
> was forged from stand: N=34 took a tail read and was one edit from recording a
> phantom "~5 fires unrecorded, 129-goal hole in the series"; N=32 took the index
> from the cadence counter; N=33's remedy ("derive it from the FILE") was necessary
> and not sufficient. Before 2026-08-03 a truncated Read handed back **N=19 sitting
> where the newest point belongs**, so a successor obeying Phase 2.0 exactly read
> its own prior point **6.2pp too high, in the direction that hides a decline**.
> `guard-2288` and the normalization goal **g-115-4382** both still apply —
> g-115-4382's job is now to keep this file single-order, not to merge two.
>
> **Append new readings directly under `## Series — alpha`, newest-first.**

## Cross-references

- guard-1922, guard-3487 — shard divergence and vacuous ordering classifiers
- guard-2653 — exclude forward-reference headings; guard-2421 — positive-control
  the probe before trusting it
- guard-157 — read the single authoritative source, not the mirror
- guard-5322, guard-1876 — a measured verdict about mutable data has a shelf life
- g-115-6690 — this SKILL.md is over its injection ceiling; g-115-8055 — the
  authoritative-read + write-time-allocation fix
- `core/config/fresh-eyes-shard-readings.md` — the dated per-shard readings ledger
- `.claude/skills/fresh-eyes-review/SKILL.md` Phase 2.0 — consumer
