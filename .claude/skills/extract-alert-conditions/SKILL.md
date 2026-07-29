---
name: extract-alert-conditions
forged: true
forged_by: bravo
forged_date: "2026-07-27"
forged_from: gap-034
description: "Triages the agent inbox by the failure conditions alert BODIES report, not the reporters their SUBJECTS name, and returns a ranked condition table plus the failing service behind each row. Use whenever the agent needs to know which services are actually failing, wants to triage or count production alerts, build an alert-frequency table, or answer 'what is broken', 'which alerts are we getting', 'triage the inbox', or 'summarize recent failures'. MUST use this skill instead of counting alert subjects: summary reporters emit a constant subject across every downstream task they report on, so a subject histogram cannot tell one broken task from twelve and has already produced a triage table wrong in its top four rows. MUST use the companion script world/scripts/alert-conditions.sh, never a hand-rolled loop over email-read.sh."
user-invocable: false
triggers:
  - "triage the inbox"
  - "triage the alerts"
  - "what is broken"
  - "which services are failing"
  - "alert frequency table"
  - "summarize recent failures"
  - "aggregate alert conditions"
  - "which alerts are we getting"
parameters:
  - name: max
    description: "manifests to scan (default 40)"
    required: false
tools_used: [Bash]
companion_scripts:
  - world/scripts/alert-conditions.sh
  - world/scripts/alert_conditions.py
conventions: [inbound-mail-lanes, infrastructure]
minimum_mode: assistant
revision_id: "skill-forge-extract-alert-conditions-gap034"
previous_revision_id: null
---

# /extract-alert-conditions — Triage Alerts By What The Body Reports

Alert **subjects name the reporter**. Alert **bodies name the failure**. Counting
subjects therefore produces a table whose top rows are the noisiest reporters,
not the broken services. This skill reads the bodies.

## Why this exists

Two alert senders are pure summary reporters: their subject is identical no
matter which downstream task is failing.

| What the subject says | What the body says |
|---|---|
| a metrics-collector reporter | `[HIGH] Task 'RateLimitTest' has consecutive failures` |
| an error-analyzer reporter | `Task 'WarmPoolManager' has failed 16 times today` |

A subject histogram counts those as one row each. It cannot distinguish one
broken task from twelve, and the failing task's name appears in **no subject at
all**. A triage table built that way was measured wrong in its top four rows
(g-115-3433); the correction came only from reading bodies. See `rb-1236`
(closing an alert-derived Investigate from the subject hid a live recurrence)
and `rb-1944` (a summary-reporter subject describes OTHER tasks' failures).

## Restricted Operations

MUST use `world/scripts/alert-conditions.sh` — never a hand-rolled loop over
`email-read.sh`, and never an inline body-regex written at the call site. The
script owns the inbox I/O and the incomplete-scan contract; `alert_conditions.py`
owns every extraction and aggregation decision, so both are unit-tested and
neither drifts per-invocation.

Credentials are resolved inside `email-read.sh` via the standard environment
chain. This skill never handles them.

## Procedure

1. **Run the scan.**

   ```
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/alert-conditions.sh" --max 40
   ```

   The `$WORLD_PATH` resolution is load-bearing, not cosmetic: `world/` is an
   EXTERNAL path and Bash tool arguments are not prefix-rewritten, so a bare
   `bash world/scripts/...` dies rc=127 and reads exactly like a dead inbox
   (`.claude/rules/path-resolution.md`).

   Add `--json` for machine consumption, `--include-noise` when auditing the
   test/diagnostic mail itself.

2. **Read the CONDITIONS table, not the subject histogram.** The report prints
   conditions first and labels the histogram "reference only, NOT the failure
   table". The histogram is included so the contrast is visible — never quote it
   as the answer.

3. **Read each row's `seen:` span BEFORE its count.** The condition table is a
   histogram over the window, not a statement about now: a resolved incident
   keeps dominating it for as long as its mail stays in the window. A span that
   is a tight cluster is HISTORY; a span across the window is ONGOING. This is
   not a theoretical caution — on this tool's first live run its top two rows
   were both finished incidents, and reading the counts as current produced two
   wrong HIGH-priority goals that had to be retracted the same hour (`rb-5447`).

4. **Retrieve the tree for the named component before filing anything.** A
   goal-queue search is NOT an exhaustive search. Both wrong goals above were
   already documented in a knowledge-tree node, down to the identical exception
   class string. Run `retrieve.sh` on the failing component name first.

5. **Act on the top rows.** Each row carries `failing=` (the task, instance, or
   repo that is actually broken), `max_reported=` (the highest occurrence count
   the bodies stated), `seen:` (first..last), and `reported by:` (provenance
   back to the reporter). Quote the window alongside any count you report —
   "x11 within 07-25 14:47..07-26 05:32", never a bare "x11".

6. **Check `unparsed_failure`.** A non-zero count means failure mail arrived
   whose condition the extractor did not recognize — a real parser gap. Read
   those bodies by hand and, if a new alert shape has appeared, extend
   `alert_conditions.py` with a pattern derived from the live body (never an
   invented one — `rb-4832`) plus a test in
   `world/scripts/tests/test_alert_conditions.py`.

7. **Ignore `informational`.** Those are success and report-only notifications
   with no failure section. They are separated out precisely so a green inbox
   does not read as a broken extractor.

## Output contract

`--json` emits:

```
{
  "conditions": [ {kind, subject_of, severity, text, occurrences,
                   max_count, reporters, first_seen, last_seen} ],
  "subject_histogram": [ [subject, count] ],
  "totals": {scanned, noise_excluded, noise_by_kind,
             unparsed_failure, informational, distinct_conditions},
  "unparsed_failure": [ {subject, date, key} ],
  "informational":    [ {subject, date, key} ]
}
```

`conditions` is sorted by `occurrences` descending. `subject_of` is the answer
the caller came for; `reporters` preserves provenance so a row can be traced
back to the mail that carried it.

## Noise handling

Automated alert-test mail and the analytics diagnostic self-test both ship
subjects indistinguishable from real failures. They are excluded by **body
marker**, never by a subject grep — `guard-1265` records that a subject-emoji
grep counts the diagnostic's failure half as a real outage and cries wolf. Pass
`--include-noise` to see them.

## Error handling

| Condition | Behavior |
|---|---|
| Inbox listing fails (S3 lane down, creds expired) | exit **2** with `scan is INCOMPLETE` on stderr. Do NOT read the output as "nothing is failing" — treat it as no signal at all (`.claude/rules/verify-before-assuming.md` rule 4). |
| Listing succeeds, zero bodies fetched | exit **2**, same contract. |
| A single manifest fetch misses | skipped silently; the object may have been re-filed between list and read. The listing guard above is the real access-failure gate. |
| Zero conditions extracted | exit **0**. A quiet inbox is a valid answer, and it is reported as such rather than falling back to the subject. |

The exit-2 contract deliberately mirrors `email-read.sh`: an incomplete scan
must never be mistaken for a clean one.

## Cost

The scan costs **one S3 round-trip per manifest**, serially: a list call plus
`--max` body fetches. Measured 2026-07-27 at `--max 40`: ~40 fetches, well
inside a single tool call. Cost is linear in `--max`, so a large window is a
long wall-clock, not a cheap one — `--max 200` is roughly five times the wait.

Defaults chosen accordingly: **40** covers roughly the last day of alert mail on
current volume, which is the window that answers "what is broken right now".
Raise it only to answer a question the default window provably cannot — a
multi-day frequency trend — and expect the wait. There is no incremental or
cached mode; each run re-fetches.

## Mode

`minimum_mode: assistant` — the scan is read-only against the inbox; its only
writes are the two session freshness markers `email-read.sh list` maintains,
which assistant mode permits. Gating at `autonomous` was rejected: a user asking
"what is broken?" during a stopped session is the highest-value moment for this
skill, and that is exactly when autonomous-only would refuse.

## Chaining

- **Called by**: production health sweeps, inbox triage during the idle
  playbook, any goal answering "which services are failing".
- **Calls**: `world/scripts/alert-conditions.sh` → `world/scripts/email-read.sh`
  (`list`, `read`) → `world/scripts/alert_conditions.py`.
- **Reads**: agent-inbox manifests. **Modifies**: nothing beyond
  `email-read.sh`'s own session freshness markers.
- **Complements**: `alert-sweep.sh` files Investigate goals from alerts and
  dedups by subject; this skill answers *what is failing* and does not file
  anything. Use both — they are not substitutes.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the `alert-conditions.sh` Bash call whose report answers
the question (or, when the caller acts on a finding, the `aspirations-add-goal.sh`
that files it). Never end with a text summary.
