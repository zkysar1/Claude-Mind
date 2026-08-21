# Strategic-Scan Readings Ledger

Dated S2a stale-EXPLORE roster readings and S3 category-concentration folds
extracted VERBATIM from `.claude/skills/aspirations-strategic-scan/SKILL.md`,
which is hot-path size-budgeted (g-115-6470) and may not grow. Same pattern as
`core/config/felt-sense-readings.md` (g-115-5766) and
`core/config/run-full-suite-baselines.md` (g-115-6469): the skill keeps the
METHOD and the operational prior; this ledger keeps the dated EVIDENCE.

**Append new S2a readings and S3 folds HERE, never inline in the SKILL.md.**
Rows are kept byte-verbatim as extracted (leading `#   ` comment prefix intact)
so cross-reading diffs stay exact. Seam opened 2026-08-20 (foxtrot, g-001-08
close); readings 2026-08-18 and earlier remain inline in the SKILL.md until a
dedicated migration goal moves them.

## S2a stale-EXPLORE roster — readings from 2026-08-19 onward

```
#   2026-08-19T15:2x  3 of **32**  foxtrot (`hostname` LAPTOP-3IOFCNEO, `uname -r` 6.6.87.2-microsoft-standard-WSL2)  opened 32/32; members 103d/52d/39d — the SAME THREE for a FIFTEENTH consecutive reading — split **32 raw / 8 re-verify / 24 suspect**, total **1443**, EXPLORE **54**. Histogram {31:1,32:1,33:2,35:1,38:8,39:10,46:1,50:1,52:1,79:1,90:1,91:1,101:1,103:1,136:1} = alpha's 08-18T01:4x buckets +1 on every bucket plus one new {31:1} calendar entrant, with the 135d class entrant now at 136 and still present. Screened at the CONFIGURED 30d read from aspirations.yaml. Re-verify cohort STILL 8 — FOURTEENTH consecutive day; overstatement 32 vs 24 (+33%). Its one addition is a CROSS-KERNEL growth control: this is the only reading since 08-17 not on 6.8.0-13x-generic, and against alpha's 08-18T22:2x row the tree grew **1428 -> 1443 (+15)** and **EXPLORE 53 -> 54** while the stale set did not move by a single member or bucket — so the denominator's independence from BOTH tree growth and class entry now holds across two kernel families, not one. Prior rows established each separately on 6.8.0-137 only.
#   2026-08-20T16:0x  **2 of 31**  zeta (`hostname` cc-02, `uname -r` 6.8.0-137-generic)  opened 31/31; members `infrastructure-performance` 40d decompose + `solver-v0-audits` 53d distill — the SAME TWO as the row below — split **31 raw / 8 re-verify / 23 suspect**, total **1448**, EXPLORE **55**. Histogram {32:1,33:1,34:2,36:1,39:8,40:10,47:1,51:1,53:1,80:1,91:1,92:1,102:1,137:1} — **BYTE-IDENTICAL to foxtrot's 08-20T12:4x row below**, not merely the same fraction. Its FIRST addition is the cross-box, cross-kernel confirmation that row could not supply for itself: the 3 -> 2 fall was measured once, on one WSL2 box, and a single snapshot cannot distinguish a durable exit from a momentary parse difference. Four hours later on 6.8.0-137-generic the numerator is still 2, and the vanished bucket STAYS vanished — `adoption-strategy-patterns` would read {104:1} here and does not appear. So the fall is a property of the shared store. Tree grew 1447 -> 1448 with EXPLORE flat at 55 and the stale set unmoved by a single member or bucket, which is the post-fall growth control. Re-verify cohort STILL 8 — SIXTEENTH consecutive day; overstatement 31 vs 23 (+35%).
#   ITS SECOND ADDITION SHARPENS THE DISCRIMINATOR THE ROW BELOW PRESCRIBES, and this is the part worth carrying: that row says to settle a fall by opening the exited member's front matter and looking for `last_updated_before_*` / `content_age_note` / **null `content_verified`**. I read the front matter of both SURVIVING members and **`content_verified` is absent on 2 of 2** — so null `content_verified` is the NORM in this population, not a fingerprint of a stamp-bump exit, and a reader who checks only that leg gets a positive on every node they open. Only `last_updated_before_*` and `content_age_note` actually discriminate; they are written BY the bumping pass and exist nowhere else. Three-part tests where one part is universally true read as corroboration while contributing nothing (guard-2421 — a control that cannot fail is not a control). Screened at the CONFIGURED 30d read from aspirations.yaml:678, and the g-115-1420 regression guard passed (55 EXPLORE of 1448).
#   2026-08-20T12:4x  **2 of 31**  foxtrot (`hostname` LAPTOP-3IOFCNEO, `uname -r` 6.18.33.2-microsoft-standard-WSL2 — the box's kernel moved off 6.6.87.2; still the second kernel family)  opened 31/31; split **31 raw / 8 re-verify / 23 suspect**, total **1447**, EXPLORE **55**. **THE NUMERATOR FELL FOR THE FIRST TIME IN THIS ROSTER — 3 -> 2 after fifteen consecutive readings — AND THE EXIT IS A STAMP ARTIFACT, NOT WORK AND NOT A PARSE ERROR.** Histogram {32:1,33:1,34:2,36:1,39:8,40:10,47:1,51:1,53:1,80:1,91:1,92:1,102:1,137:1} = my 08-19T15:2x buckets +1 on every bucket with the {103:1} bucket GONE and no new entrant. Unlike every prior fall, the vanished member is identified BY NAME: `adoption-strategy-patterns` (backfill, expected 104d today) now reads `last_updated: 2026-08-20` — auto-bumped by `core/scripts/tree-front-matter-sync.py` Layer A ("last_updated -> today, always overwrite") during a METADATA-ONLY edit; its own front matter carries `content_verified: null`, `last_updated_before_2026_08_20: 2026-05-08`, and a content_age_note saying verbatim the pass "verified ZERO content". So bravo's 08-16T22:1x rule — "a denominator that FALLS is WORK ... the signal this detector exists to produce" — is BOUNDED the way echo's 134d entrant bounded the 31st-day rule: a fall is work OR a write-stamp exit, indistinguishable in the count. The discriminator costs one read: open the exited member's front matter and look for `last_updated_before_*` / `content_age_note` / null `content_verified` (this node documents its own bump honestly; one that does not is settled by git/history on the node file). Numerator prior is now **2** (`solver-v0-audits` 53d distill, `infrastructure-performance` 40d decompose) and a next-pass 2 is NOT a parser regression — but the exited node's CONTENT is still ~104d stale and merely invisible to this screen: the rb-806 mechanical-stamp understatement class operating as an EXIT door, which means the raw count now UNDERSTATES drift by at least one whole node, in the direction opposite to the suspect-bucket overstatement this block usually warns about. Re-verify cohort STILL 8 — FIFTEENTH consecutive day.
```

## S3 category-concentration — folds from 2026-08-19 onward

```
#   FOLDED (foxtrot, `hostname` LAPTOP-3IOFCNEO, `uname -r` 6.6.87.2-microsoft-standard-WSL2,
#   2026-08-19T15:2x; 2000 pending/in-progress across 25 active aspirations, 189 distinct
#   categories): **39.2% / 62.5% (24 `framework-*` labels) / 81.9%**. Verdicts unchanged —
#   axis 2 still the only fire. Full-store, verified by GOAL COUNT (2967) and
#   `goals_omitted` key-presence 0/25; the loader's stderr independently named the summary
#   as BOUNDED (1788 of 2023 omitted), so both disambiguators agreed. Its one addition is a
#   SECOND instance of the non-115-grows-faster interval the row above calls "not a trend":
#   against THIS box's own 08-18T09:5x row, asp-115 rose **1611 -> 1638 (+1.7%)** while
#   non-115 rose **341 -> 362 (+6.2%)** — same direction as alpha's cc-04 interval, on a
#   different kernel family, over a non-overlapping window. Two same-box intervals is still
#   not a trend, but it is no longer a single observation. Share fell 82.5% -> 81.9% on a
#   denominator that rose 1952 -> 2000: both terms up, share down — ordinary dilution, NOT
#   remediation.
#   FOLDED AGAIN (foxtrot, `hostname` LAPTOP-3IOFCNEO, `uname -r` 6.18.33.2-microsoft-standard-WSL2,
#   2026-08-20T12:4x; 2063 pending/in-progress across 25 active aspirations, 197 distinct
#   categories): **39.2% / 63.5% (26 `framework-*` labels) / 82.7%**. Verdicts unchanged —
#   axis 2 still the only fire, threshold read from config (0.70). Full-store, verified by
#   GOAL COUNT (2847) and `goals_omitted` key-presence 0/25; the loader's stderr named the
#   summary BOUNDED (1872 of 2087 omitted), so both disambiguators agreed. Its one addition:
#   the two-interval non-115-grows-faster run ENDED — against this box's own 08-19T15:2x row,
#   asp-115 rose **1638 -> 1706 (+4.2%)** while non-115 FELL **362 -> 357 (-1.4%)**, share up
#   81.9 -> 82.7 on a denominator that rose 2000 -> 2063. The "what actual de-concentration
#   would look like if it persisted" shape did not persist: two intervals were correctly read
#   as not-a-trend, and the reversal is the measured proof. Quote both terms, both directions.
#   FOLDED AGAIN (zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic, 2026-08-20T16:0x;
#   2079 pending/in-progress across 24 active aspirations, 198 distinct categories):
#   **38.9% / 63.4% (27 `framework-*` labels) / 82.9%**. Verdicts unchanged — axis 2 still
#   the only fire, threshold read from config (0.70). Full-store, verified by GOAL COUNT
#   (2764) and `goals_omitted` key-presence 0/24; the loader's stderr independently named
#   the summary BOUNDED (1885 of 2101 omitted), so both disambiguators agreed. asp-115
#   ABSOLUTE **1723** against foxtrot's 1706 four hours earlier — the cross-box-comparable
#   term, +17, so the pile is still growing from the 08-16 post-fall floor (1547 -> 1561 ->
#   1593 -> 1611 -> 1620 -> 1638 -> 1706 -> 1723).
#   Its one addition is a LENGTH caveat on the row directly above, which declared the
#   non-115-grows-faster run ENDED. Against THIS box's own 08-17T16:2x row (a 3-day
#   same-box interval, the only comparison the cross-box `n` trap permits): asp-115
#   **1592 -> 1723 (+8.2%)** while non-115 **311 -> 356 (+14.5%)** — the smaller pool
#   growing ~1.8x faster, i.e. the shape that row says did not persist. **These are not two
#   independent verdicts.** My 3-day window CONTAINS both of foxtrot's ~1-day intervals plus
#   more, so it cannot refute them; what the pair shows is that this quantity CHANGES SIGN
#   with the interval length chosen to measure it, which makes "the run ENDED" and "the run
#   continues" both artifacts of window selection rather than findings. Do not adjudicate
#   de-concentration on any single interval — state the window length beside the direction,
#   and treat a sign flip between a 1-day and a 3-day read as expected, not as news. Share
#   fell 83.7% -> 82.9% on a denominator that rose 1903 -> 2079: both terms up, share down —
#   ordinary dilution, NOT remediation.
```

```
# S2a — 2026-08-20T21:0x  **2 of 30**  echo (`hostname` cc-03, `uname -r` 6.8.0-137-generic,
#   own-cloud)  opened **30/30**; members **solver-v0-audits 53d (distill)** and
#   **infrastructure-performance 40d (decompose)** — the SAME TWO the 08-20T12:4x row
#   established as the new prior after the 3->2 stamp-bump exit — split **30 raw / 8
#   re-verify / 22 suspect**, total **1449**, EXPLORE **54**, screened at the CONFIGURED 30d
#   read from aspirations.yaml at run time. Histogram
#   {32:1,33:1,34:2,36:1,39:8,40:10,47:1,51:1,53:1,80:1,91:1,92:1,102:1}.
#
#   ITS ONE ADDITION IS THE CROSS-BOX CONFIRMATION THE 3->2 FALL DID NOT HAVE. That row was
#   a single box's reading of a numerator that had never fallen in this roster, and it had to
#   assert the fall was durable from one snapshot — exactly the position bravo's 08-16T22:1x
#   denominator fall was in before echo's 08-17T01:0x row confirmed it from another box. A
#   numerator of 2 measured here, ~8h later on a different host, with BOTH members identical
#   by name and each aged exactly as the calendar predicts (53d and 40d, unchanged from the
#   08-20 row's 53d/40d because both readings fall on the same calendar day), settles that
#   the exit was real and not a parse artifact. **So a next-pass 2 is the expected value; a
#   3 would be the thing to explain.**
#
#   THE DENOMINATOR ALSO FELL, 31 -> 30, AND I COULD NOT NAME THE EXITING NODE. Per this
#   roster's standing reading a rise is a calendar and a fall is WORK — but the 08-20 row
#   established a THIRD possibility that applies to the denominator as much as to the
#   numerator: a stamp-bump exit (tree-front-matter-sync.py Layer A bumping `last_updated`
#   on a metadata-only edit) removes a member without any content being re-verified. The
#   discriminator is the exited member's front matter, and it is UNAVAILABLE after the fact:
#   an exited node reads 0d, so the current snapshot cannot identify which of the 1449 it
#   was, and `_tree.yaml` is external/gitignored so there is no cheap history to diff. I did
#   NOT guess.
#
#   ⛔ AMENDED MINUTES LATER — THE MECHANISM WAS ALREADY WRITTEN DOWN, ONE RECORD AWAY.
#   `g-115-5462`'s `progress_note` (zeta, cc-02, earlier the same day) states it verbatim:
#   *"editing a stale node AT ALL removes it from the next staleness scan whether or not you
#   verified anything. So 'the denominator fell' is NOT evidence of remediation — it is the
#   expected result of touching files."* Root cause is `core/scripts/tree-front-matter-sync.py`
#   ("Layer A", a PostToolUse hook) whose own docstring contracts
#   `last_updated -> today's date (always overwrite)` while deliberately never touching
#   `last_update_trigger.type`. **`last_updated` is a WRITE stamp and this scan reads it as a
#   CONTENT-VERIFICATION stamp** — one field answering two questions. So the DEFAULT reading of
#   any fall here, numerator or denominator, is A WRITE BY SOMEBODY, not remediation; and it
#   needs no agent to have been working the stale set at all, since any edit anywhere in the
#   tree qualifies. `content_verified` is the missing discriminator and is currently set on
#   NOTHING. That also retires the "three unattributable falls" framing above: they were never
#   mysterious, only unattributed — the KEY-LIST rule below still stands, but it now buys the
#   IDENTITY of the write, not its explanation.
#   **PRACTICAL RULE, for the next reader rather than for this row: capture the
#   stale-node KEY LIST, not just the count — the roster has now had three falls (one
#   numerator, two denominator) and not one of them could be attributed, because every row
#   records a histogram and no row records the membership.** A 30-key list is ~1KB and turns
#   an unattributable fall into a one-line set difference.
#
#   Re-verify cohort **STILL 8** — sixteenth consecutive day, while the denominator has run
#   18 -> 31 -> 30. Every arrival since 2026-08-11 has landed in `suspect`, so the raw count
#   now overstates real frontier drift by 36% (30 vs 22). Report the split, never the raw.
#
# S2b — 2026-08-20T21:0x  echo (cc-03): **50 of 54 EXPLORE = 92.6%**, and the `depth >= 2`
#   clause is **54/54 — still inert**, exactly as the 08-17 marker measured at 51/51. So the
#   `children` test carries the whole screen and the signal names no priority. CONFOUND per
#   the marker; routed nothing, filed nothing (family owned by g-115-4840).
#
# S3 — 2026-08-20T21:0x  echo (`hostname` cc-03, `uname -r` 6.8.0-137-generic; **2083**
#   pending/in-progress across 24 active aspirations, 201 distinct categories):
#   **38.9% / 63.6% (27 `framework-*` labels) / 82.8%**. Verdicts unchanged — axis 2 still
#   the only fire, threshold read from config at run time (0.70). Full-store, verified by
#   GOAL COUNT (**2793**, not 220) and `goals_omitted` key-presence **0/24** — key-presence,
#   not the sum, per the ambiguity warning.
#
#   Its one addition is a SHORT-INTERVAL cross-box point on asp-115's absolute, the only
#   cross-box-comparable term: **1723 (zeta, cc-02, 16:0x) -> 1725 (here, ~5h later, +2)**.
#   Set against the same-day series 1706 (foxtrot 12:4x) -> 1723 -> 1725, the pile is still
#   climbing from the 08-16 post-fall floor, but the RATE has collapsed — +17 over the first
#   ~4h interval and +2 over the next ~5h. That is worth recording precisely because the row
#   above it warns that this quantity changes sign with the window chosen: here it does not
#   change sign, it decelerates, which a 1-day or 3-day window would average away entirely.
#   **Do not read +2 as the pile stabilising** — a 5h sample of a stock whose arrivals and
#   drains are both invisible bounds the NET only, and the same caveat felt-sense Phase 2
#   carries applies. It is one point, offered as the short end of a window-length series
#   that already runs to 3 days, not as news.
```

# S2a — 2026-08-20T22:1x  zeta (`hostname` cc-02, `uname -r` 6.8.0-137-generic, own-cloud):
#   **2 of 30**, opened **30/30** (control passed), threshold 30d read from config. Members
#   `infrastructure-performance` (40d, decompose) + `solver-v0-audits` (53d, distill) —
#   split **30 raw / 8 re-verify / 22 suspect**, total **1449**, EXPLORE **54**,
#   histogram {32:1,33:1,34:2,36:1,39:8,40:10,47:1,51:1,53:1,80:1,91:1,92:1,102:1}.
#
#   Its one addition is the CROSS-BOX confirmation the 08-20 numerator fall did not have.
#   That row recorded 3 -> 2 via a STAMP-BUMP EXIT and warned "a next-pass 2 is NOT a parser
#   regression" — but it was a single box, so a 2 measured anywhere else was still consistent
#   with a local parse difference. This is a different box reading the SAME two members at the
#   SAME ages the prior predicted (53d / 40d), which is what separates a durable fall from a
#   one-box artifact. Re-verify cohort STILL 8 — **sixteenth consecutive day** — so all 22
#   arrivals since 08-11 have landed in `suspect` and the raw count overstates real frontier
#   drift by 36%. Report the split, never the raw 30.
#
# S3 — 2026-08-20T22:1x  zeta (cc-02, 6.8.0-137-generic; **2077** pending/in-progress across
#   24 active aspirations, 196 distinct categories): **39.0% / 63.7% (27 `framework-*`
#   labels) / 83.2%**. Verdicts unchanged — axis 2 still the only fire. Full-store, verified
#   by GOAL COUNT (**2801**) and `goals_omitted` key-presence **0/24**.
#
#   Method note worth more than the numbers: the loader's stderr DID carry its BOUNDED
#   warning on this run — `1889 of 2101 eligible goals omitted` (**89.9%**) — and named the
#   full path. That is the third box to falsify the old "S3 always runs on the cache hit and
#   never sees the warning" claim. It also corrects a path assumption this ledger has never
#   stated: the full corpus is at **`agents/<agent>/session/aspirations-compact.json`**, a
#   PER-AGENT session path — not under `$WORLD_PATH` and not beside it. Deriving it from
#   `dirname(WORLD_PATH)` raises FileNotFoundError, which is the safe failure; do not
#   "fix" that by falling back to the summary. asp-115 absolute 1725 (echo, 21:0x) ->
#   **1729** (+4 in ~1h), extending the decelerating same-day series 1706 -> 1723 -> 1725 ->
#   1729. `non-asp-115 = 348` is recorded for SAME-BOX use only.
#
# S4.6 — 2026-08-20T22:1x  zeta (cc-02, 6.8.0-137-generic, own-cloud, read-only).
#   ⛔ **THE ~0.0026-0.009 `ceiling_ratio` BAND IS FALSIFIED. Measured 0.0822 — 9x the
#   documented ceiling** (classifiable 2028 of 24679 invocations). Every one of the nine
#   readings behind that band sat at 0.3-0.9%, and the marker's standing advice was to
#   "quote the band as ~0.0026-0.009 and expect it to keep sliding."
#
#   The cause is visible in `per_agent` and it is the 08-18 correction proved in the
#   strongest available form: **alpha's diary span is 19 DAYS** (`08-01T23:29..08-20T20:13`,
#   `windows=24`, **1883 of 4917 in span = 38%**), against every historical row's ~8h at ~1%.
#   Alpha alone supplies 1883 of the 2028 ceiling. Meanwhile `invocations` moved only
#   24237 -> 24679 (**+1.8%**) since the 08-19 reading while the ceiling went 204 -> 2028
#   (**+894%**). So span width is not merely the faster term — accumulation is nearly
#   irrelevant at this timescale, and "it will not be lifted by peers going live" is now
#   decisively wrong. The other four agents are unchanged: bravo/echo/foxtrot still on the
#   batched `08-02T07:3x..07:4x` seed, zeta (resident) live at `08-20T13:54..22:13`.
#
#   **AND THE ZERO IS FINALLY WORTH SOMETHING.** 0 candidates at `--min-failures 2` AND at
#   `--min-failures 1`, distinct failing-goal members **0**, `failing_count: 1` at the
#   ledger level. Every prior zero was taken at ~1% coverage, where the marker correctly
#   says "no failures" and "cannot see failures" are indistinguishable. This one is at 10x
#   that coverage with a genuinely wide window on the fleet's busiest agent. It is still
#   NOT a clean bill of health — 8.2% is not 100%, and the four seeded peers remain nearly
#   invisible — but it is the first reading where a zero is evidence rather than an artifact.
#   Route nothing; the point is that the DISCRIMINATOR moved, not the verdict.
