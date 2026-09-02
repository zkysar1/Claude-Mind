---
name: aspirations-curate-memory
description: "Daily curation flow for guardrails + reasoning bank. Each fire pulls 15 lowest-evidence guardrail candidates and 15 rb candidates from utilization-stats.py, walks them with a deterministic RETIRE/KEEP/REVISE/SKIP decision protocol (default RETIRE on zero-evidence + high exposure + age), writes exemption windows on KEEP, files Maintain goals on REVISE, and appends a batch entry to world/curation-log.jsonl. Use whenever the daily g-115-348 recurring goal fires, the user runs /aspirations-curate-memory directly, or the orchestrator hits a manual curation checkpoint. Internal sub-skill — slot in g-115-348's procedure block."
user-invocable: true
triggers: [curate-memory, curate-guardrails, curate-rb, daily-curation, curation-sweep, retire-low-evidence]
tools_used: [Bash, Read]
companion_scripts:
  - core/scripts/utilization-stats.py
  - core/scripts/utilization-stats.sh
  - core/scripts/guardrails-update-field.sh
  - core/scripts/reasoning-bank.py
conventions:
  - reasoning-guardrails
  - learning-routing
minimum_mode: assistant
revision_id: "skill-bootstrap-aspirations-curate-memory-2d74a1"
previous_revision_id: null
---

# /aspirations-curate-memory — Daily Curation Sweep

Walks the bottom of the guardrail and reasoning-bank populations, retiring entries
that have accumulated no positive evidence over many exposures and time, keeping
those with recent signal, and routing genuinely-broken triggers to a Maintain goal.

This is the procedure that g-115-348 (daily, recurring, interval 24h) runs. The
agent can also invoke it directly when the curation-log shows a backlog.

## Why this exists

Audit on 2026-05-09 found 320 active guardrails and 693 active reasoning-bank
entries; 143 of the guards and 378 of the rb entries had zero positive evidence
(no helpful, no inferred, no active, no cited) across hundreds of LLM exposures.
No retirement protocol fired. The framework was additive without being subtractive.
This skill is the subtractive arm.

## Cadence

g-115-348 runs every 24h (interval_hours: 24). At 15 items/day per kind, the
guardrail population walks once in ~3 weeks; rb in ~7 weeks. After the first
sweep, the next_review_eligible_at exemption window self-throttles daily volume
to whatever has aged out (14d for zero-evidence, 30d for positive).

## Procedure

### Phase 0 — Load conventions

Bash: `load-conventions.sh reasoning-guardrails learning-routing` (no-op if already loaded).

### Phase 1 — Pull candidates

```
Bash: bash core/scripts/utilization-stats.sh guardrails candidates --limit 15
```

Parse JSON. Each candidate item carries:
- `id`, `evidence` (composite score, see utilization-stats.py)
- `retrieval_count`, `times_helpful`, `times_inferred_helpful`, `times_active`,
  `times_cited`, `times_skipped`, `times_inferred_unknown`
- `age_days`, `created`
- `next_review_eligible_at`, `auto_flagged_for_review`
- `category`, `rule` (truncated to 140 chars)

If `candidate_count == 0`: skip to Phase 4. Routine close — population is
caught up.

### Phase 2 — Per-item decision

For each candidate (process in returned order — already sorted by evidence asc,
age desc, exposure desc):

1. **Read the full record** — Bash: `bash core/scripts/guardrails-read.sh --id <candidate-id>`
   Look at: full `rule` text, `trigger_condition`, `source`, `category`, `tags`,
   `created`, all evidence counters.

2. **Decide RETIRE | KEEP | REVISE | SKIP**:

   **Default RETIRE if all of:**
   - `evidence == 0` (no helpful, inferred, active, or cited across all exposures)
   - `retrieval_count >= 200` (enough exposures to have proven value)
   - `age_days >= 60` (enough time)
   - No recent journal/board citation of the rule's source or trigger keyword

   **KEEP if any of:**
   - `times_cited > 0` in the last 14 days
   - `times_active > 0` in the last 14 days
   - The rule encodes a hard safety invariant (deletion / push to main /
     destructive-op gate) — even unused safety rules are kept (low-cost insurance)
   - The rule is auto-flagged but the underlying record still describes a
     real, currently-relevant invariant — the flag fired because --infer
     couldn't classify; agent judgment overrides

   **Reviewed-valid vs provisional KEEP (selects the exemption window below):**
   a KEEP is *reviewed-valid* (`reviewed_durably_valid=yes` → 90d window) when
   the agent read the full record AND affirmatively judged it durably-valid via
   EITHER (a) a grep of CLAUDE.md + .claude/skills + .claude/rules + core/config
   for the entry ID that found a LIVE doc-reference (guard-707 — doc-referenced
   entries are load-bearing regardless of counters; EXCLUDE `.history/`), OR
   (b) content that is a stable framework-engineering invariant certain to stay
   relevant (an OS/shell gotcha, a parser/subprocess-contract discipline, an
   architecture principle). A KEEP that is merely "couldn't confidently retire,
   holding provisionally" is NOT reviewed-valid — it stays at the 14d window.

   **REVISE if:**
   - The rule is correct but `trigger_condition` is wrong (too narrow, too
     broad, references retired skill names) — file a Maintain goal and KEEP

   **SKIP only if:**
   - The agent genuinely can't decide in <60s — defer to next cycle (no
     window write; record stays at top of next batch)

3. **Apply the decision**:

   **RETIRE** (use the appropriate update-field script):
   ```
   Bash: bash core/scripts/guardrails-update-field.sh <id> status retired
   Bash: bash core/scripts/guardrails-update-field.sh <id> retirement_date $(date +%Y-%m-%d)
   Bash: bash core/scripts/guardrails-update-field.sh <id> retirement_reason "<one-line: why>"
   ```

   For rb: substitute `bash core/scripts/reasoning-bank-update-field.sh <id> ...`.
   (Use the .sh wrapper, not the .py directly — the wrapper sources `_paths.sh`
   which sets up the python shim PATH that redirects `python3` to `py -3` on
   Windows. Calling `python3 core/scripts/reasoning-bank.py ...` without the
   wrapper can hit the Microsoft Store stub on Windows and fail with exit 49.)

   **KEEP** (write the exemption window):
   ```
   # Window tiers (agent selects based on the KEEP decision above):
   #   90d — a zero-evidence entry the agent CONTENT-REVIEWED and affirmatively
   #         judged durably-valid (reviewed_durably_valid; see KEEP criteria).
   #         Rationale: evidence-tracking counts retrieval-session citations, NOT
   #         the background-reasoning value of framework lessons, so a valid-but-
   #         uncited entry keeps evidence=0 across hundreds of retrievals forever;
   #         a 14d re-review is then perpetual low-value churn AND the entry would
   #         wrongly trip the retrieval>=200 auto-RETIRE default. A reviewed-valid
   #         KEEP earns the long window. (g-115-2081)
   #   14d — zero-evidence, PROVISIONAL keep (couldn't fully judge — more suspect)
   #   30d — positive-evidence (proven, lower priority)
   if [ "$reviewed_durably_valid" = "yes" ] && [ "$evidence" = "0" ]; then
       exemption_days=90
   elif [ "$evidence" = "0" ]; then
       exemption_days=14
   else
       exemption_days=30
   fi
   eligible_date=$(date -d "+${exemption_days} days" +%Y-%m-%d)
   Bash: bash core/scripts/guardrails-update-field.sh <id> next_review_eligible_at $eligible_date
   ```

   If `auto_flagged_for_review == true`, also clear it on KEEP (the agent has
   reviewed it; the flag has served its purpose):
   ```
   Bash: bash core/scripts/guardrails-update-field.sh <id> auto_flagged_for_review false
   ```

   **REVISE**:
   - File a Maintain goal under asp-115 with corrected trigger:
     ```
     echo '{"title":"Maintain: revise <id> trigger_condition","priority":"MEDIUM","participants":["agent"],"category":"framework-maintenance","origin_signal":"maintain:","description":"Curation review found <id> trigger_condition needs revision: <reason>. Correct to: <new trigger>"}' | bash core/scripts/aspirations-add-goal.sh asp-115
     ```
   - Then KEEP with 30-day exemption (the Maintain goal will fix the trigger).

   **SKIP**: no action — the entry will resurface in tomorrow's candidate list.

### Phase 3 — Repeat for rb

```
Bash: bash core/scripts/utilization-stats.sh rb candidates --limit 15
```

Same per-item flow as Phase 2, with these substitutions:
- Field name: `title` instead of `rule`
- Update commands route through `bash core/scripts/reasoning-bank-update-field.sh <id> <field> <value>` (same .sh-wrapper rule as Phase 2)
- Same RETIRE/KEEP/REVISE rules; same exemption windows

### Phase 4 — Append batch entry to curation-log

```
Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" python3 -c '
import json, os
from datetime import datetime
batch = {
    "date": datetime.now().strftime("%Y-%m-%d"),
    "batch_id": datetime.now().strftime("%Y%m%dT%H%M%S"),
    "agent": os.environ.get("MIND_AGENT", "unknown"),
    "kind_breakdown": {
        "guardrails": {"retired": [...], "kept": [...], "revised": [...], "skipped": [...]},
        "rb":         {"retired": [...], "kept": [...], "revised": [...], "skipped": [...]},
    },
}
with open(os.environ["WORLD_DIR"] + "/curation-log.jsonl", "a") as f:
    f.write(json.dumps(batch) + "\n")
'
```

Pattern follows guard-165 (env-var injection, single-quoted python source):
- `WORLD_DIR="$WORLD_DIR"` re-exports the shell variable for THIS invocation
  only. `_paths.sh` deliberately doesn't export it; the prefix is the
  per-call bridge. Without this prefix `os.environ["WORLD_DIR"]` raises
  KeyError because shell vars from `source` are NOT inherited by children.
- `python3 -c '...'` uses single-quotes so bash does no expansion inside
  the python source. Don't interpolate `$WORLD_DIR` into the python text
  directly — paths can contain quotes/backslashes that break the parse,
  and the env-var path stays robust as the value's character set widens.
- `MIND_AGENT` is auto-injected by the PreToolUse[Bash] hook, so it does
  NOT need a prefix.

If `world/curation-log.jsonl` doesn't exist, the open-with-append creates it.
The file is append-only audit trail — never edit existing entries.

### Phase 5 — Journal entry

Bash: `journal-append.sh` with a one-line summary:
```
Curation sweep <date>: guard(retired N, kept M, revised K, skipped S), rb(retired N, kept M, revised K, skipped S). Auto-flagged cleared: F.
```

### Phase 6 — Done

Return to caller (g-115-348 completion path). The recurring goal closes via
`aspirations-complete-by.sh g-115-348` (it is the recurring closer; it takes only
--source / --key-finding — there is no --recurring flag).

## Edge cases

- **Empty candidate list** — Phase 1 returns 0 candidates. Phases 2-3 are skipped.
  Phase 4 appends an empty batch (still useful audit trail showing the cadence
  fired). Phase 5 journals "Curation sweep: caught up — no candidates."

- **Lock contention on guardrails.jsonl / reasoning-bank.jsonl** — `_atomic_write`
  retries 8 times then falls through to in-place rewrite. Treat any write failure
  message in stderr as advisory, not fatal. Re-run the cycle next cadence.

- **All 15 candidates SKIP** — possible if the agent is context-poor and can't
  reliably judge any item. Log the skip count in Phase 5; do not retry within
  the same fire. Tomorrow's batch will surface the same items.

- **Auto-flagged item with positive evidence** — appears in candidate list because
  C.3 forces inclusion regardless of evidence. Default decision: KEEP (the flag
  fired because --infer couldn't classify, but the evidence shows it IS being
  used). Clear the flag on KEEP per Phase 2.

## Mode requirement

`minimum_mode: assistant` — the procedure performs writes (status changes,
exemption windows, Maintain goals, journal entries). Reader mode would refuse
all of those.

## Chaining

- Called by: g-115-348 (daily recurring goal under asp-115); user via `/aspirations-curate-memory`
- Calls: `utilization-stats.sh`, `guardrails-update-field.sh`, `reasoning-bank.py`,
  `aspirations-add-goal.sh`, `journal-append.sh`
- Does NOT call: `/aspirations`, `/aspirations-execute`, `/reflect` (those run
  outside this skill's scope; curate-memory is bounded by design)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the journal-append.sh call in Phase 5, OR a `Bash: echo`
acknowledging completion when this skill is invoked from /aspirations during
g-115-348 execution. When called from inside the autonomous loop, control returns
to the orchestrator after the journal write.
