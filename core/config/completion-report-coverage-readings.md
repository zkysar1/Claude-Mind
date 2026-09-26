# Completion-Report Coverage Readings

Dated hypothesis-coverage readings taken by `/agent-completion-report` (the
recurring g-001-04 progress-report run). **Append new readings HERE, never into
`.claude/skills/agent-completion-report/SKILL.md`.**

WHY THEY MOVED (2026-09-11, zeta): the skill is INJECTED whole when invoked, and
an injection is bounded at 65,536 B. Twenty-five appended rows had grown it to
68,622 B — past the ceiling, so its later content was reaching the model
TRUNCATED and silently absent. The `hot-path-size-gate` refused the 26th row and
was right to: a ritual that appends its own readings into its own always-injected
skill file eventually truncates the instructions that tell it how to read them.
Same defect and same remedy as `core/config/felt-sense-readings.md` and
`core/config/run-full-suite-baselines.md`; this is the third instance, which is
why the pattern is named rather than just fixed.

The METHOD for reading and extending this series stays in the SKILL.md, where it
is needed at the moment of use. What follows is the EVIDENCE.

Format: `MM-DD <window> <coverage>% (n=<scoreable>, split <resolved-backlog>/<archived>)`
followed by the reading's own interpretation.

| # | reading |
|---|---|
| 1 | 08-04 2d 100% (n≈?) · 08-15 2d 62.9% (n=35) · 08-18 39h 71.1% (n=38) |
| 2 | 08-19 79.6h 73.5% (n=68) · 08-21 20.2h 81.1% (n=37, split 44/1261) |
| 3 | 08-21 9.7h 65.0% (n=20, split 48/1262)  <- SECOND 08-21 reading, ~8h later |
| 4 | 08-21 9.5h 75.0% (n=28, split 56/1262)  <- THIRD 08-21 reading, ~2h later |
| 5 | 08-22 25h(date-floored) 100% (n=22, split 60/1262) |
| 6 | 08-22 15.2h(date-floored 16.2h) 100% (n=14, split 62/1278) |
| 7 | 08-24 32.5h(date-floored 48.8h) 86.2% (n=29, split 73/1279)  <- 3 CALENDAR DAYS |
| 8 | 08-24 34.8h(date-floored 48.8h) 87.5% (n=32, split 76/1279)  <- SAME DAY, ~13min later, DIFFERENT BOX |
| 9 | 08-24 63.3h(date-floored 83.9h) 83.3% (n=66, split 78/1279) |
| 10 | 08-25 13.3h(date-floored 25.3h) 63.0% (n=27, split 40/1335)  <- BACKLOG DRAINED 78->40, COVERAGE FELL WITH IT |
| 11 | 08-29 49.4h(date-floored 3 calendar days) 90.3% (n=31, split 50/1397) |
| 12 | 08-30 99.94h(date-floored 5 calendar days) 93.3% (n=60, split 58/1397)  <- WIDEST WINDOW *AND* HIGHEST NON-100%, TOGETHER |
| 13 | 08-31 102.5h 48.7% (n=39, split 22/1554)  <- WIDER THAN 08-30 AND HALF ITS COVERAGE, ONE DAY LATER |
| 14 | 08-31 29.9h(date-floored 46.8h) 100% (n=14, split 22/1554)  <- SAME BOX-DAY, IDENTICAL STORE SPLIT, NARROWER WINDOW |
| 15 | 09-02 27.85h(date-floored 2 calendar days) 100% (n=16, split 24/1577)  <- NEAR-EXACT REPLICATION OF THE ROW ABOVE, 2 DAYS LATER |
| 16 | 09-02 21.2h(date-floored 37.2h) 100% (n=16, split 24/1577)  <- SMALLEST BACKLOG SINCE 08-31 (24), STILL 100% ON A ~1-DAY WINDOW; consistent with the 08-31 narrow row, not with the wide one |
| 17 | 09-02 9.76h(date-floored 1 calendar day) 100% (n=20, split 31/1577)  <- THIRD 09-02 READING. Backlog GREW 24->31 and coverage HELD at 100%; the within-window backlog model predicts UP and 100% is the ceiling, so this row is SATURATED and tests nothing. Same-day window, so guard-2303 discounts it too. Recorded for the denominator, not as evidence. |
| 18 | 09-05 28.06h(date-floored 2 calendar days) 100% (n=21, split 57/1588)  <- FOURTH consecutive 100%. Backlog GREW 31->57 (largest since 08-24's 78) and coverage HELD at the ceiling, which the within-window backlog model already predicts -- so this row is SATURATED and tests nothing, exactly like the 09-02 row above. Window spans 2 calendar days, so guard-2303's date-floor discount does NOT excuse it. Recorded for the denominator. |
| 19 | 09-07 21.5h(date-floored 2 calendar days) 100% (n=7, split 35/1619)  <- FIFTH consecutive 100%, and the FIRST row where the backlog DRAINED (57->35) without coverage falling with it. Both prior drains fell (08-25 78->40 -> 63.0%; 08-31 58->22 -> 48.7%), so this is counter-evidence to the within-window backlog model -- but n=7 is the SMALLEST denominator in the series and one archival sweep moves it tens of points. Recorded for the denominator, NOT as a refutation. |
| 20 | 09-08 25.6h(date-floored 2 calendar days) 100% (n=6, split 41/1619)  <- SIXTH consecutive 100% and the SMALLEST denominator in the series (n=6, under 09-07 n=7). Backlog GREW 35->41, and the within-window backlog model already predicts UP for a growing backlog, so this row is SATURATED and tests nothing -- in particular it does NOT corroborate the 09-07 row above, whose counter-evidence needed a DRAIN to say anything. Window spans 2 calendar days so guard-2303 does not excuse it either. Recorded for the denominator. |
| 21 | 09-08 8.29h(date-floored 1 calendar day) 100% (n=4, split 16/1651)  <- SEVENTH consecutive 100% and the SMALLEST denominator in the whole series (n=4, under the 09-08 n=6 above and 09-07 n=7). Two discounts, both pointing the same way: the window is SAME-DAY so guard-2303's date-floor discount applies in full, and n=4 is one archival sweep away from anything. The one NON-saturated fact here: the resolved backlog DRAINED 41->16 (61%, the largest drain in the series by ratio) while archived grew 1619->1651, and coverage HELD at 100%. Both earlier drains fell hard WITH the backlog (08-25 78->40 -> 63.0%; 08-31 58->22 -> 48.7%) and 09-07 was the first hold (57->35, n=7). So this is the SECOND consecutive drain-hold -- recorded as a second WEAK counter-example to the within-window backlog model, never as a refutation: a date-floored n=4 is the weakest evidence the series can carry. |
| 22 | 09-09 21.7h(date-floored 2 calendar days) 100% (n=6, split 18/1651)  <- EIGHTH consecutive 100%. Backlog GREW 16->18, and the within-window backlog model already predicts UP for a growing backlog, so this row is SATURATED and tests nothing -- in particular it does NOT corroborate the two drain-holds above, whose counter-evidence needed a DRAIN to say anything. Window spans 2 calendar days so guard-2303's date-floor discount does NOT excuse it either. Recorded for the denominator. |
| 23 | 09-10 50.95h(date-floored 3 calendar days) 100% (n=14, split 20/1664)  <- FIRST 100% SINCE THE BREAK BELOW, and it is SATURATED so it tests nothing: the resolved backlog GREW 18->20 and the within-window backlog model already predicts UP for a growing backlog. Two things make it a BETTER row than the n=4/6/7 rows it follows and neither rescues it: the window spans 3 CALENDAR DAYS so guard-2303's date-floor discount does NOT excuse it, and n=14 is 2-3x their denominators. It does NOT corroborate the 09-07/09-08 drain-holds, whose counter-evidence needed a DRAIN. Nor does it contradict the 266.5h row below: that window crossed deep past the archival horizon and this one does not. Coverage computed over SCOREABLE only, same definition as every row since 08-30 (guard-1835). SECOND, INDEPENDENT COUNT AVAILABLE THIS RUN: the `--accuracy` denominator moved 1,025->1,035 (+10) against 14 date-floored scoreable, and the 4-record difference is exactly the window's opening calendar day — so 14 is the CEILING and 10 the exact count (method now carried by g-001-04's trap (3)). Where both numbers exist, publish both and say which is which. |
| 24 | 09-09 266.5h 11.6% (n=86, split 15/1658)  <- BREAKS THE EIGHT-ROW 100% RUN, and it is the strongest row in the series: the LARGEST denominator ever recorded here (n=86, vs the n=4/6/7 rows it follows) on the WIDEST window (266.5h, 2.6x the prior widest 102.5h), at the LOWEST coverage. The resolved backlog DRAINED 18->15 and coverage collapsed WITH it -- so this is a third drain-FALL (matching 08-25 78->40 -> 63.0% and 08-31 58->22 -> 48.7%) and it OUTWEIGHS the two n=7/n=4 drain-holds of 09-07/09-08 that were recorded as weak counter-evidence to the within-window backlog model: those rows are one archival sweep wide, this one is not. Consistent with the 08-31 WIDE row (102.5h, 48.7%), not with its narrow same-day twin. Window mix matters and is recorded: 227 in-window records but only 86 SCOREABLE (55 CONFIRMED, 31 CORRECTED) against 141 EXPIRED+UNRESOLVABLE -- an all-records count would have reported 4.4% and silently entered a different quantity into this series. |
| 25 | 09-11 51.1h(date-floored 3 calendar days) 100% (n=16, split 28/1665)  <- NINTH 100% of the run, and SATURATED so it tests nothing: the resolved backlog GREW 20->28 (its largest single-day growth since 09-05) and the within-window backlog model already predicts UP. Window spans 3 CALENDAR DAYS so guard-2303's date-floor discount does NOT excuse it, and n=16 is mid-range. Does NOT corroborate the 09-07/09-08 drain-holds (those needed a DRAIN) and does not contradict the 266.5h row (that window crossed past the archival horizon; 51.1h does not). Mix recorded: 25 in-window records, 16 SCOREABLE (12 CONFIRMED, 4 CORRECTED) against 5 UNRESOLVABLE + 4 EXPIRED -- an all-records count would have said 64% and entered a different quantity. ON THE SECOND-COUNT METHOD the 09-10 row introduced: `--accuracy` total_resolved reads 1,041 here against that row's 1,035, but the 1,035 was taken on ANOTHER BOX at an UNRECORDED instant inside 09-10, so +6 bounds only the TAIL of this window and is a LOWER bound on in-window scoreable, NOT the exact count that method yields when both readings bracket the same window. Publish both and say which is which -- that instruction is what makes the difference visible; a same-agent reading at the window's own start is what the method actually needs. |
| 26 | 09-12 406.1h 9.5% (n=137, split 19/1677)  <- THE STRONGEST ROW IN THE SERIES, and it displaces #24 on every axis at once: the LARGEST denominator ever recorded here (n=137, 1.6x the prior max n=86), the WIDEST window (406.1h, 1.5x the prior widest 266.5h), and the LOWEST coverage (9.5%, under #24's 11.6%). Measured foxtrot, hostname LAPTOP-3IOFCNEO, uname -r 6.18.33.2-microsoft-standard-WSL2. The resolved backlog DRAINED 28->19 and coverage collapsed WITH it, so this is a FOURTH drain-FALL (08-25 78->40 -> 63.0%; 08-31 58->22 -> 48.7%; #24 18->15 -> 11.6%) and it settles the argument the 09-07/09-08 drain-HOLDS opened: those two were n=7 and n=4, one archival sweep wide, against three falls now carrying n=27, n=39 and n=137. Treat the within-window backlog model as HOLDING and the two holds as denominator noise. Mix recorded, and it is why the SCOREABLE definition is load-bearing: 297 in-window records, 137 SCOREABLE (80 CONFIRMED, 57 CORRECTED) against 130 EXPIRED + 30 UNRESOLVABLE -- an all-records count would have reported 4.4% and silently entered a different quantity into this series. NOT date-floored: the window spans 17 CALENDAR DAYS, so guard-2303 does not discount it in either direction. ON THE SECOND-COUNT METHOD (#23/#25): `--accuracy` total_resolved reads 1,043 here against #25's 1,041, i.e. +2 against 137 in-window scoreable -- the two readings are 407h apart and that counter is LIFETIME-CUMULATIVE over records that have since been archived, so +2 bounds nothing here and must NOT be published as a lower bound. The method needs two readings bracketing the SAME window; across a window this wide the archival horizon voids it. |
| 27 | 09-12 44.16h(date-floored 3 calendar days) 100% (n=14, split 23/1677)  <- TENTH 100% of the run and SATURATED, so on coverage alone it tests nothing: the resolved backlog GREW 19->23 against row #26's reading taken earlier the same day, and the within-window backlog model (now treated as HOLDING per #26) already predicts UP. Window spans 3 CALENDAR DAYS so guard-2303's date-floor discount does NOT excuse it; n=14 is mid-range. Measured echo, hostname cc-03, uname -r 6.8.0-139-generic. Mix recorded: 24 in-window records, 14 SCOREABLE (11 CONFIRMED, 3 CORRECTED) against 9 UNRESOLVABLE + 1 EXPIRED -- an all-records count would have said 58% and entered a different quantity. Note #26 read split 19/1677 and this row 23/1677 hours later: the ARCHIVED side is identical, so the whole delta is resolved-side growth, not an archival sweep. **THE ROW'S ACTUAL CONTRIBUTION IS TO THE SECOND-COUNT METHOD, NOT TO COVERAGE.** #25 diagnosed why its own +6 bounded only the tail ("the 1,035 was taken on ANOTHER BOX at an UNRECORDED instant") and named the fix: "a same-agent reading at the window's own start is what the method actually needs." This is the first row that HAS one. The prior reading 1,035 resolved / 591 confirmed / 444 corrected was taken by ECHO on cc-03 and recorded in echo's own COMPLETION-REPORT.md at 2026-09-10T23:15, i.e. AT this window's open -- the report file IS the boundary marker -- and this run reads 1,044 / 598 / 446 on the same box at its close. So the two readings BRACKET the identical window by construction, and the delta is the EXACT count, not a bound: +9 resolved (+7 CONFIRMED / +2 CORRECTED) against a date-floored ceiling of 14 (11/3). The 5-record difference is exactly the records dated on the window's OPENING calendar day, already counted by the prior report, and the arithmetic closes on all three numbers independently (14-9=5, 11-7=4, 3-2=1, 4+1=5) -- which coincidence does not do. CONDITION FOR RE-USE, since this is the only row that meets it: same agent, same box, prior reading taken AT the window boundary and recorded in a file the next run can grep. Where those hold, publish the delta as the COUNT and the date-floored figure as the CEILING; where they do not (#25, #26), publish neither as a bound and say why. |
| 28 | 09-13 24.6h(date-floored 2 calendar days) 100% (n=3, split 27/1678)  <- ELEVENTH 100% of the run and the SMALLEST denominator in the whole series (n=3, under #21's n=4). On coverage it is SATURATED and tests nothing: the resolved backlog GREW 23->27 against #27 earlier the same box-day, and the within-window backlog model (HOLDING per #26) already predicts UP. The window spans 2 calendar days so guard-2303's date-floor discount does not excuse it, but n=3 is one archival sweep from anything -- weaker than every drain-hold already discounted as denominator noise. Measured zeta, hostname cc-02, uname -r 6.8.0-139-generic. Mix recorded: 7 in-window records, 3 SCOREABLE (1 CONFIRMED, 2 CORRECTED) against 4 EXPIRED+UNRESOLVABLE -- an all-records count would have said 43% and entered a different quantity. **THE ROW'S ACTUAL CONTRIBUTION IS THAT IT IS THE FIRST TEST OF #27'S RE-USE CONDITION, AND IT FAILS ON A CLAUSE #27 DID NOT STATE.** All three stated clauses HOLD: same agent (zeta), same box (cc-02), and a prior reading taken AT this window's open and recorded in a greppable file -- `agents/zeta/COMPLETION-REPORT.md` at commit f0ef0d756b, committed 2026-09-12T04:06:41, 6.5 min before the window opens. The method still yields NOTHING, because that report DOES NOT PRINT the lifetime accuracy triple at all: a grep of it for accuracy/resolved/confirmed/corrected returns only prose sentences, no `total_resolved` figure, so there is no prior number to bracket with. FOURTH CLAUSE, now explicit: the prior report must actually RECORD the triple, not merely exist at the boundary -- "recorded in a file the next run can grep" was read as being about the FILE when it is about the NUMBER. The condition is now ARMED rather than met: occurrence 98 does print it (`Lifetime: 1,046 resolved, 57.3% accuracy (599 confirmed / 447 corrected)`), so the NEXT zeta run on cc-02 is the method's second real test. Do NOT patch the gap by substituting #27's echo/cc-03 reading (1,044/598/446) as the bracket -- different agent, different box, and #25 already diagnosed that exact substitution as bounding only the tail. |
| 29 | 09-15 29.75h(date-floored 2 calendar days) 100% (n=34, split 47/1740)  <- TWELFTH 100% of the run and SATURATED on coverage: the resolved backlog GREW 27->47 (its largest single-step growth in the series) and the within-window backlog model (HOLDING per #26) already predicts UP. Window spans 2 calendar days so guard-2303's date-floor discount does NOT excuse it; n=34 is the largest denominator of any 100% row here (over #23's n=14), which makes it the least noise-prone saturated row but still one that tests nothing. Measured echo, hostname cc-03, uname -r 6.8.0-139-generic. Mix recorded: 77 in-window records, 34 SCOREABLE (18 CONFIRMED, 16 CORRECTED) against 21 UNRESOLVABLE + 22 EXPIRED -- an all-records count would have said 44% and entered a different quantity. **THE ROW'S CONTRIBUTION IS THE SECOND REAL TEST OF #27's RE-USE CONDITION, AND IT PASSES ON ALL FOUR CLAUSES INCLUDING #28's NEW ONE.** Same agent (echo), same box (cc-03), prior reading taken AT this window's open and recorded in a greppable file, and -- the clause #28 had to add -- that report ACTUALLY PRINTS the triple: `agents/echo/COMPLETION-REPORT.md` at 2026-09-14T13:12 records resolved 1,048 / confirmed 600 / corrected 448. This run reads 1,082 / 618 / 464 on the same box at the window's close. Delta +34 / +18 / +16, and the arithmetic closes independently on all three (18+16=34). NEW FACT THE METHOD HAS NOT SHOWN BEFORE: the delta and the date-floored CEILING COINCIDE exactly (34=34, 18=18, 16=16), where #27's differed by 5. So the date-only floor cost nothing here -- no scoreable record dated on the window's opening calendar day had already been counted by the prior report. That is a property of THIS window (the prior report closed at 13:12 and the day's scoreables resolved after it), NOT an improvement in the instrument: keep publishing both figures and saying which is which, because the next window's opening day will not necessarily be empty. Note this is echo/cc-03's second consecutive qualifying row (#27 was the first), so the condition is reproducible on this pair, not a one-off. |
| 30 | 09-16 40.5h(date-floored 3 calendar days) 100% (n=54, split 77/1744)  <- THIRTEENTH 100% of the run and SATURATED on coverage: the resolved backlog GREW 47->77, its LARGEST single-step growth in the series (beating #29's 27->47), and the within-window backlog model (HOLDING per #26) already predicts UP. Window spans 3 CALENDAR DAYS so guard-2303's date-floor discount does NOT excuse it; n=54 is the largest denominator of any 100% row here (over #29's n=34), which makes it the least noise-prone saturated row and still one that tests nothing on coverage. Measured zeta, hostname cc-02, uname -r 6.8.0-139-generic. Mix recorded: 111 in-window records, 54 SCOREABLE (30 CONFIRMED, 24 CORRECTED) against 31 UNRESOLVABLE + 26 EXPIRED -- an all-records count would have said 48.6% and entered a different quantity. **THE ROW'S CONTRIBUTION IS THAT IT IS THE TEST #28 EXPLICITLY ARMED FOR "THE NEXT ZETA RUN ON cc-02", AND IT PASSES ON ALL FOUR CLAUSES.** Same agent (zeta), same box (cc-02), prior reading taken AT this window's open -- `agents/zeta/COMPLETION-REPORT.md` mtime 2026-09-14T12:17:20 against a window opening 12:16:09, 71 seconds apart, the tightest bracket in the series -- and the clause #28 had to add: that report ACTUALLY PRINTS the triple, at its line 44, `Lifetime accuracy 57.3% - 1048 resolved (600 confirmed, 448 corrected)`. This run reads 1,102 / 630 / 472 on the same box at the window's close. Delta +54 / +30 / +24, and the arithmetic closes independently (30+24=54). SECOND CONSECUTIVE COINCIDENCE OF DELTA AND CEILING, NOW ON A SECOND AGENT/BOX PAIR: 54=54, 30=30, 24=24, exactly as #29 saw on echo/cc-03. #29 called its coincidence "a property of THIS window, NOT an improvement in the instrument" and that reading SURVIVES -- both windows happen to have an EMPTY opening calendar day (every 09-14 scoreable resolved after 12:17, as every 09-13 one did after 13:12) -- but the property has now reproduced across two agents, two boxes and two windows, where #27's did not (it differed by 5). Keep publishing both figures and saying which is which; the next window's opening day will not necessarily be empty. The condition is now demonstrated reproducible on TWO pairs (echo/cc-03 #27+#29, zeta/cc-02 #30), not one. |

- **2026-09-16, echo, cc-03 (Linux 6.8.0-139-generic), 18.95h window — 100.0%, denominator 53.**
  Store split at that instant: **78 resolved / 1745 archived**. In-window records 110, of which
  53 SCOREABLE (29 CONFIRMED / 24 CORRECTED) against 30 UNRESOLVABLE + 27 EXPIRED — scored over
  SCOREABLE only, so it is comparable to the rest of the series (an all-records count would have
  read 48.2% and silently entered a different quantity). The 100% is the BACKLOG reading again,
  not an instrument improvement: 78 resolved is the deepest backlog recorded in this series, and
  ZERO of the 53 had reached the archived stage. **Discount it for a second, separate reason this
  row is the first to name**: `outcome_date` is DATE-ONLY and this window OPENS AT 19:01 on
  2026-09-15, so rows resolved earlier that same calendar day cannot be excluded by any filter —
  the denominator 53 is an UPPER BOUND, not an exact count. That is distinct from guard-2303's
  same-day objection (which is about a window NARROWER than the field's resolution); here the
  window is wide but its START lands mid-day, so the floor leaks at the near edge. Any row whose
  window does not begin at midnight carries this, and none of the earlier rows say so.

- **2026-09-17, zeta, cc-02 (Linux 6.8.0-139-generic), 15.74h window — 100.0%, denominator 5.**
  Store split at that instant: **74 resolved / 1758 archived** (bytes beside records: resolved
  607,813 B, archived 9,493,563 B, active 462,609 B → 118). In-window records 7, of which
  5 SCOREABLE (5 CONFIRMED / 0 CORRECTED) against 1 EXPIRED + 1 UNRESOLVABLE — scored over
  SCOREABLE only, comparable to the rest of the series (an all-records count would have read
  71.4% and entered a different quantity). FOURTEENTH 100% of the run and SATURATED on coverage,
  so it tests nothing there: the resolved backlog GREW 68 → 74 and the within-window backlog
  model (HOLDING per #26) already predicts UP. Doubly discounted — the window is SAME-DAY, so
  guard-2303 applies in full, and n=5 is near the series floor.

  **The row's contribution is that the second-count method CLOSES the mid-day-start leak the
  row above declares unclosable by any filter.** That row is right that a date-only
  `outcome_date` cannot exclude records resolved earlier on the opening calendar day, so the
  date-floored denominator is an UPPER BOUND — and this window opens at 06:52:35, squarely
  mid-day. All four of #27/#28's re-use clauses hold: same agent (zeta), same box (cc-02),
  prior reading taken at the window's open, and — #28's added clause — that report ACTUALLY
  PRINTS the triple: `agents/zeta/COMPLETION-REPORT.md` at commit `cf381d04`, "Generated:
  2026-09-17T06:55", line 39, `632 confirmed / 473 corrected = 57.2% of 1,105 scoreable`.
  This run reads 1,110 / 637 / 473 at the window's close. Delta **+5 / +5 / +0**, arithmetic
  closing independently (5 + 0 = 5).

  Delta and date-floored ceiling COINCIDE for the third consecutive qualifying row (5=5, 5=5,
  0=0; after #29 echo/cc-03 and #30 zeta/cc-02) — but here the coincidence carries information
  the earlier two did not. #29 and #30 read theirs as "a property of THIS window: the opening
  calendar day happened to be empty", ASSUMED from the timing. Here it is PROVEN: any scoreable
  record resolved between 00:00 and 06:52 on 09-17 would sit inside the prior report's 1,105
  AND inside this run's date-floored 5, so it would drive delta BELOW ceiling. Delta equals
  ceiling, therefore that segment held zero scoreable records. The method does not merely
  survive a mid-day start — it MEASURES the leak the filter cannot reach, and reports it empty.
  Keep publishing both figures and saying which is which; a future window's opening segment will
  not necessarily be empty, and the point is that this method can now tell you.

  One residual, named because no earlier row does: the prior triple was read at 06:55, ~145s
  AFTER this window opens at 06:52:35, so the bracket OVERLAPS rather than abutting (#30's was
  71s and abutting). A record resolving inside that 145s gap would be double-counted and would
  likewise push delta below ceiling; delta equals ceiling, so none did. The overlap is bounded
  and measured, not assumed — but a bracket taken AFTER the window opens is strictly weaker than
  one taken at or before it, and the next run should prefer the latter.

- **2026-09-18, zeta, cc-02 (Linux 6.8.0-139-generic), 22.03h window — 100.0%, denominator 6.**
  Store split at that instant: **17 resolved vs 1,816 archived** (union deduped by id = 1,833).
  In-window records 8 (6 CONFIRMED, 1 EXPIRED, 1 UNRESOLVABLE); SCOREABLE 6, of which 6 sat in
  the resolved stage. guard-2303 applies — `outcome_date` is date-only and this window opens at
  22:41 on 09-17, so the date-floored denominator is an UPPER BOUND on in-window activity.

  **THIS ROW FALSIFIES THE BACKLOG MODEL AS STATED, AND NAMES THE MISSING QUALIFIER.** The
  series' standing conclusion is "backlog is the driver; width is noise" — within a fixed window
  coverage tracks the resolved backlog, so a shallow backlog should read LOW. The backlog here is
  **17, the LOWEST value anywhere in this series** (prior range 22–78, per the 08-24 high of 78
  and the 08-15 low of 34), and coverage read its CEILING. Under the model as written that is
  backwards.

  The reconciliation is a qualifier no prior row states: **backlog governs coverage only for
  windows WIDER than the archival horizon.** The horizon has been measured repeatedly at ~2.3
  days or less; a 22.03h window sits ENTIRELY inside it, so every record resolved in-window is
  necessarily still in the resolved stage and coverage is pinned at 100% *whatever* the backlog
  is. A shallow backlog then means only that few things resolved recently — it does not mean
  archival ran ahead of resolution. Both quantities are downstream of the same cadence, which is
  why they looked causally linked while every window stayed narrow.

  This does NOT overturn the wide-window rows (08-24's 78-deep backlog at ~1.7x width, the
  08-30/08-31 pair at ~100h). Those cross the horizon, so backlog is genuinely their driver.
  It bounds where the model applies. Practical consequence for the next reader: **a 100% on a
  sub-day window is not evidence the instrument is healthy and not evidence the backlog is
  deep — it is the window being narrower than the horizon, and it carries no information about
  either.** Only a window wider than ~2.3d can measure anything here. Combined with guard-2303's
  date-floor, that leaves a usable band between roughly 2.3d and whatever width makes the
  date-floor leak dominate; rows inside it are the only ones worth comparing.

- **2026-09-20, zeta, cc-02 (Linux 6.8.0-139-generic), 30.42h window — 100.0%, denominator 3.**
  Store split at that instant: **19 resolved vs 1,819 archived** (union deduped by id = 1,838 —
  the two stores are disjoint). In-window records 6 (2 CONFIRMED, 1 CORRECTED, 3 UNRESOLVABLE);
  SCOREABLE 3, of which 3 sat in the resolved stage. Scored over SCOREABLE only — an
  all-records count here would have reported 50.0% and entered a different quantity into the
  series.

  **THIS ROW IS THE 09-18 QUALIFIER'S FIRST PREDICTION-THEN-CONFIRMATION, which is the only
  thing it adds.** The prior row derived that backlog governs coverage only for windows WIDER
  than the ~2.3d archival horizon, and that a sub-horizon window is pinned at 100% whatever the
  backlog is. This window is 30.42h = 1.27d — still inside the horizon — and it moved BOTH free
  variables in the direction that would break a naive backlog model: width UP (22.03h → 30.42h)
  and backlog UP (17 → 19). Coverage stayed at its ceiling, as predicted. A row that merely
  repeated 100% at the same width would have been worth nothing; the value is that the
  prediction was stated before the measurement and the measurement could have falsified it.

  Two cautions for whoever reads this next. First, the denominator is **3** — the smallest in
  this series — so the row is confirmatory, not strong: three records cannot distinguish a
  ceiling from a coincidence on their own, and it is the AGREEMENT with the 09-18 row's stated
  mechanism that carries it, not the percentage. Second, this reading was independently
  corroborated in the same run: `completion-digest.sh`, which walks the stores by a different
  path, reported the identical 2 confirmed / 1 corrected for the identical window. Two
  instruments, one answer — that is what makes the 3 trustworthy as far as it goes.

  Standing instruction unchanged and now twice-demonstrated: **a 100% on a sub-day window is not
  evidence about the instrument, the backlog, or the fleet.** Only a window wider than ~2.3d
  measures anything here. Do not quote this row; measure your own.

- **2026-09-21 · zeta · cc-02 (Linux 6.8.0-139-generic) · window 22.11h · resolved-stage 3/3 = 100% · denominator 3 · store split 7 resolved / 1,833 archived.**
  DISCOUNT THIS ROW THREE WAYS, and the third is new to the series. (1) guard-2303: the window
  opens 03:19 and `outcome_date` is date-only, so the filter swallows its own first nine hours.
  (2) guard-3542: n=3, against a series whose rows run 35-199 — not comparable to any of them.
  (3) **The resolved backlog is 7, the LOWEST ever recorded here** (prior rows 22-78). The
  series' own conclusion is that backlog drives coverage, and a backlog this small cannot
  produce a coverage number that means anything: with 3 scoreable records and 7 in the stage,
  100% is close to arithmetically forced. Read the row as "nothing was missed", never as a
  coverage rate. It predicts nothing about the next run — which is what every row here says.

- **2026-09-21 · echo · cc-03 (Linux 6.8.0-139-generic) · window 118.6h · resolved-stage 7/18
  = 38.9% · denominator 18 scoreable (14 CONFIRMED, 4 CORRECTED; 28 in-window union records,
  8 UNRESOLVABLE + 2 EXPIRED excluded) · store split 11 resolved / 1,833 archived.**
  PAIR THIS WITH THE ZETA ROW DIRECTLY ABOVE — same DAY, same archived stage (1,833), near-identical
  backlog (7 vs 11), and coverage 100% against 38.9%. That is a 61-point spread with the
  backlog held almost constant, and it is the cleanest BACKLOG-CONTROLLED pair the series has.
  It does for the width axis what the 08-24 thirteen-minute pair did for the backlog axis:
  **backlog cannot be the sole driver.** The difference between the two rows is window width —
  22.11h against 118.6h — and only the wide one reaches back across the archival horizon into
  days already swept, which is exactly the mechanism the third 08-24 row proposed and could not
  isolate. Read the two together as the reconciliation the series has been circling: WITHIN a
  fixed width, coverage tracks the backlog; ACROSS widths, a window that crosses the horizon
  drives coverage down regardless of backlog. Both rows are true; neither predicts your run.
  One caution on my own row so it is not over-read the way a clean number invites: n=18 is at
  the low end of the series (35-199 elsewhere), so the 38.9% carries real sampling noise even
  though the mechanism it demonstrates does not depend on the exact value.

- **2026-09-21 · foxtrot · LAPTOP-3IOFCNEO (WSL2 6.18.33.2) · window 234.85h · 8 of 75 scoreable = 10.7% · store split resolved 11 / archived 1,833.**
  LOWEST reading in the series, and it CONFIRMS the backlog model against the width model. The
  resolved backlog is **11** — the smallest recorded anywhere in this ledger — and coverage fell
  with it, exactly as the 08-25 and 08-30/08-31 rows predict. Width cannot explain it: at 234.85h
  this is the WIDEST window in the series AND the lowest coverage, whereas the 08-30 row (99.94h,
  the prior widest) scored the HIGHEST non-100% at 93.3% against a 58-deep backlog. Two widest-ever
  windows, opposite extremes of coverage, backlog moving with the result both times.
  Computed over SCOREABLE only (75 = 45 CONFIRMED + 30 CORRECTED) out of 146 in-window records;
  the 41 UNRESOLVABLE + 30 EXPIRED are excluded, as the 08-30/08-31 row requires — an all-records
  denominator here would have reported 5.5% and silently entered a different quantity.
  A resolved-only fetch would have missed 89.3% of that report's own subject matter.

- **2026-09-23 · zeta · cc-02 (6.8.0-139-generic) · window 28.9h · keyed on `resolved_at`: 4 of 4 scoreable = 100% · store split resolved 25 / archived 1,838.**
  THE NEW AXIS IS THE WINDOW KEY, NOT THE COVERAGE. Every row above filtered on `outcome_date`,
  which is DATE-ONLY. On this window that key returns 10 scoreable (5 CONFIRMED + 5 CORRECTED),
  also 100% in the resolved stage. But 5 of the 10 carry `resolved_at` 2026-09-22T00:25-08:17,
  BEFORE since (11:16:58), so the prior report had already counted them. A 6th
  (`2026-09-08_box-local-remedy-names-artifact`, CONFIRMED) carries `resolved_by` but NO
  `resolved_at`, so no date-only key can place it. Its pre-since placement is INFERRED from the
  delta below, not measured. (Corrected 2026-09-23 by a fresh-eyes self-review: this line first
  said "6 of the 10 carry `resolved_at`".)
  `resolved_at` is writer-stamped to the second (`pipeline_write.py:600-615`). It gives 3 + 1, and
  the lifetime accuracy numerator moved +3 / +1 over the same interval, which independently
  corroborates it.
  So a date-floored row can over-count by re-reading the boundary day, and one keyed on
  `resolved_at` cannot. The digest widens by a further day (`completion_digest.py:565`) and
  printed 5 / 5 (fix owned by g-115-10706).
  Undated scoreable records (guard-6828): 92 of 1,130. `resolved_at` is present on 13 of this
  window's 17 date-floor records; 1 of the 4 without it is scoreable (the 6th record above).
  The 100% here is not a guard-2303 floor artifact, because the key resolves to the second. The
  earliest in-window resolution is 09-22T21:10 and the backlog is 25, so nothing had yet had time
  to archive.
  Record the KEY on every future row; a date-floored and a `resolved_at` row are different
  quantities.

- **2026-09-25 · foxtrot · LAPTOP-3IOFCNEO (6.18.33.2-microsoft-standard-WSL2) · window 75.9h · keyed on `resolved_at` plus 1 date-only placement: 29 of 29 scoreable = 100% · store split resolved 41 / archived 1,852.**
  PRIOR POINT RE-DERIVED FIRST (guard-1835). I re-ran the 09-23 row with this row's predicate
  (`resolved_at` in [09-22T11:16:58, +28.9h]). It returns 4 scoreable, 3 CONFIRMED + 1 CORRECTED,
  all 4 in the resolved stage. That is the same fraction and population as recorded, with the same
  earliest resolution (09-22T21:10), so the predicate is stable. That row's END is inferred from
  since + width, and two CORRECTED records resolved 44 min past it (09-23T16:54-16:55), so its
  boundary is only good to about an hour.
  THE KEY MOVES THE COUNT, AS THE 09-23 ROW PREDICTED. The date-floored `outcome_date` key returns
  32 (19 CONFIRMED + 13 CORRECTED), also 100% in the resolved stage. Three of those 32 were
  resolved on the boundary day, 04:26-05:10, before since (20:03:16):
  `2026-06-22_signal-gated-cadence-routine-collapse` and
  `2026-09-06_floor-promoted-recurring-goals-invert-the-interval-ratio` (CONFIRMED), and
  `2026-09-07_third-inert-reader-in-the-env-server-node-key-census` (CORRECTED).
  `resolved_at` gives 28 (16 + 12). The 29th is `2026-09-08_box-local-remedy-names-artifact`,
  the record with no `resolved_at` that the 09-23 row names. Its `outcome_date` (09-22) is a day
  past this window's boundary day, so the date key places it here unambiguously. Its ambiguity is
  specific to windows whose boundary falls ON 09-22.
  Independent corroboration: the lifetime accuracy counter moved 1,118 / 643 / 475 (prior report,
  09-21T19:58) → 1,147 / 660 / 487. That is +29 resolved = 17 CONFIRMED + 12 CORRECTED, exactly the
  `resolved_at` count, and it refutes the date floor's 32.
  THE 100% IS NOT A FLOOR ARTIFACT. The window spans four calendar days, so guard-2303 does not
  apply. The earliest in-window `resolved_at` (09-22T00:00:49) was 72h old at gather time and still
  in the resolved stage.
  Archival is also NOT waiting on reflection, which is the natural hypothesis and is falsified:
  all 33 scoreable resolved-stage records carry `reflected: true`, as do all 1,114 scoreable
  archived ones. What does trigger archival of a scoreable record was not measured.
  In the same window, 14 NON-scoreable records (8 EXPIRED, 6 UNRESOLVABLE, date key) had already
  reached the archive and no scoreable one had.

- **2026-09-24 · zeta · cc-02 (6.8.0-139-generic) · window 29.1h · keyed on `resolved_at`: 17 of 17 scoreable = 100% · store split resolved 41 / archived 1,852.**
  THE KEY CORROBORATES EXACTLY FOR THE SECOND ROW RUNNING. `resolved_at` gives 10 CONFIRMED + 7
  CORRECTED, and the lifetime accuracy numerator moved 650/480 -> 660/487 over the same interval,
  i.e. +10 / +7. A date floor on the same window returns 12 + 8 (36 records), which the digest
  printed (fix still owned by g-115-10706, pending).
  The backlog is 41 (25 at the prior row) and the earliest in-window resolution is 09-23T16:54,
  so 100% is what the backlog model predicts. It is not a floor artifact.
  STAMP COVERAGE BY OUTCOME, measured over `outcome_date` >= 2026-09-01: scoreable 184 of 185 carry
  `resolved_at`, EXPIRED 1 of 54, UNRESOLVABLE 35 of 72. So a `resolved_at` key places scoreable
  records reliably, while EXPIRED/UNRESOLVABLE tallies stay floored by the close paths that skip
  the stamp. 7 records this window carry only since's own date and no `resolved_at`; all 7 are
  non-scoreable, so this row's scoreable denominator is exact.
- **2026-09-25 · echo · cc-03 (6.8.0-139-generic) · window 82.9h · keyed on `resolved_at` plus 1 date-only placement: 27 of 29 scoreable = 93.1% · store split resolved 37 / archived 1,871.**
  Same window keyed on the date-only `outcome_date` read 27 of 34 = 79.4%. The 5 extra are archived-stage records whose date is 2026-09-22 but whose `resolved_at` falls before the window opened at 11:02:52. So on a window that opens mid-day, the date floor (guard-2303) DEFLATES coverage by pulling in pre-window archived records; it does not only inflate same-day windows. Key on `resolved_at` when it is present.
- **2026-09-26 · zeta · cc-02 (6.8.0-139-generic) · window 10.3h · keyed on `resolved_at`: 6 of 6 scoreable = 100% · store split resolved 27 / archived 1,888.**
  All 9 records stamped inside the window sit in the resolved stage (3 CONFIRMED, 3 CORRECTED, 3 UNRESOLVABLE). 6 archived-stage records carry only a date-only `outcome_date` and no `resolved_at`: 2 UNRESOLVABLE and 2 EXPIRED dated 09-25, which may predate the 17:59 start, and 2 EXPIRED dated 09-26. All 6 are non-scoreable, so the scoreable denominator is exact. The backlog is 27 (37 at the prior row) and the earliest in-window resolution is 09-25T17:59:54, so for a 10h window 100% is what the backlog model predicts. For the same window the fleet digest printed 6 confirmed / 4 corrected, because it counts on the widened date floor (g-115-10706, pending).
