---
name: verify-learning
description: "Runs post-test verification: checks the agent's state against a comprehensive checklist of learning artifacts (tree encodings, resolved hypotheses, reflections, skill return-protocols, guardrails, pattern signatures) and reports missing or drifted items. Use whenever the user says /verify-learning, after major framework changes to confirm the loop still produces learning, after a session with many commits and no encodings, or when framework-level health needs a diagnostic sweep. User-invocable AND agent-callable."
triggers:
  - "/verify-learning"
conventions: [aspirations, pipeline, experience, reasoning-guardrails, pattern-signatures, spark-questions, journal, tree-retrieval, goal-schemas, session-state, infrastructure, secrets, handoff-working-memory]
minimum_mode: reader
revision_id: "skill-bootstrap-verify-learning-e6052f"
previous_revision_id: null
---

# /verify-learning — Post-Test Verification

User-invocable AND agent-callable (hybrid skill).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load Checklists

1. Read `core/config/verification-checklist.md` (framework checklist).
2. Read `core/config/verification-checklist-domain-specific.md` (foundational domain checklist template — see file header for the three-tier loading explanation).
3. IF `world/verification-checklist.md` exists:
   Read it (agent-discovered domain checks).
   ELSE: Note "No agent-discovered domain checks — skipping."

### Step 1.1: Load-Time Sanity (rot detection)

Counts active content lines per loaded file — BOTH `Check:`-prefixed lines
AND numbered-discovery lines (leading `\d+\. `). If any deployment-overlay
file contributes ZERO of BOTH AND the deployment has ≥10 completed goals,
emits a SOFT WARNING (does not fail verification). Catches the rot pattern
where a deployment-specific checklist has silently become a stub. Created
2026-05-17 (Phase 1.2 packaging cleanup) after the domain template had
silently been a 23-line stub since 2026-04-06 with no warning surfaced.

The OR-match on both prefix shapes was added 2026-05-19 (g-115-955) after
a populated-but-prose overlay (`world/verification-checklist.md` with 81
numbered-discovery lines from alpha-era encode-session) false-triggered the
"0 Check: lines" rot warning. Numbered ordered lists ARE substantive content
when they enumerate discovered checks — the previous `^\s*Check:`-only regex
treated them as zero. Real rot looks like a 23-line stub; populated prose
contributes either format and should PASS.

   Check: domain-overlay verification checklists are not rotted to placeholder content. Bash: `py -3 -c "import sys,re,pathlib,json; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; files=[('core/config/verification-checklist-domain-specific.md',pathlib.Path('core/config/verification-checklist-domain-specific.md')),('world/verification-checklist.md',WORLD_DIR/'verification-checklist.md')]; total=0; asp=WORLD_DIR/'aspirations.jsonl'; total = sum(sum(1 for g in __import__('json').loads(L).get('goals',[]) if g.get('status')=='completed') for L in asp.read_text(encoding='utf-8').splitlines() if L.strip()) if asp.exists() else 0; warns=[f'{label} loaded but 0 content lines (Check: or numbered-discovery) (deployment has {total} completed goals - likely rotted)' for label,p in files if p.exists() and sum(1 for L in p.read_text(encoding='utf-8').splitlines() if re.match(r'^\s*Check:|^\s*\d+\.\s',L))==0 and total>=10]; print('WARN: '+'; '.join(warns)) if warns else print(f'PASS: all loaded overlay checklists contribute content lines (deployment has {total} completed goals)')"`

## Step 2: Evaluate Each Section

For each item in ALL sections from all checklists (A through the last section):
1. Read the referenced file
2. Report **PASS**, **FAIL**, or **N/A** (if the agent didn't reach that stage)

For section G (Known Design Limitations):
- Confirm these are expected behaviors, not bugs

## Step 3: Evidence Check

Focus on what actually happened during the test — did the agent USE the new features, or did it just have them available? Look at:
- Resolved pipeline records: `pipeline-read.sh --stage resolved`
- Journal entries in `agents/<agent>/journal/`
- Reasoning bank entries: `reasoning-bank-read.sh --summary`


### The checks live in a registry, not in this file

The evidence checks are in `core/config/verify-learning-checks.jsonl` — 2,237 of
them at time of writing, though `sections` prints the live count and this
sentence does not, deliberately: a hardcoded total goes stale on the first add
and nothing notices. Prefer the number the tool reports over any number written
here.

They were moved out of this file on 2026-08-18 (g-115-6689) because inline they
had become **unreachable, not merely bulky**: SKILL.md had grown to 1,208,153
bytes against a skill-injection ceiling of 63,515, so a run that believed it was
"executing the checklist" received roughly the first 5% of it followed by a
truncation marker. Nothing errored. Every check past the opening section had been
invisible to every model for as long as the file exceeded the ceiling — which is
why a check's mere presence here was never evidence it ran.

The registry is byte-exact. Each record stores its source line verbatim, so
`show` reproduces the original text character for character and
`verify --against <copy>` proves it.

**Run the checks in windowed slices. Never try to load them all at once — that
is the failure this extraction exists to remove.**

1. Read the slice index. It is small and always loads whole:

   Bash: `bash core/scripts/verify-check-registry.sh sections`

   One row per section with its check and command counts. 61 slices at time of
   writing.

2. Load a section verbatim. Output is capped at 40,000 bytes per call, under the
   injection ceiling:

   Bash: `bash core/scripts/verify-check-registry.sh show --section <CODE>`

   Stdout is exactly what used to sit inline — `Check:` lines to evaluate,
   `Bash (name):` and `Bash:` commands to run, and the `#` comments explaining
   why each check exists. Execute them as written.

   **Read stderr.** When a section exceeds the cap the footer says so and gives
   the next offset:

   ```
   -- emitted blocks 0..326 of 1853 (39,970 B, 63 checks). MORE REMAIN: re-run with --offset 327
   ```

   Keep re-running with the printed `--offset` until the footer stops saying
   MORE REMAIN. Sections are genuinely this large: `4T` is 224,352 B across 6
   pages, `CG` 141,048 B, `SC` 75,889 B. A single unpaged read of any of those
   would truncate exactly the way the old inline corpus did.

3. Report PASS / FAIL / N/A per check, aggregated per section.

#### Choosing the slices

A full pass is every slice and does not fit one context. Pick scope
deliberately:

| Situation | Slices to run |
|---|---|
| Framework files just changed | `git diff --stat HEAD~5..HEAD`, then the sections whose checks cite those paths |
| A named subsystem changed | That subsystem's section(s), located from the `sections` index |
| General health sweep, no specific trigger | The three largest first — `4T`, `CG`, `SC` — then widen |
| Caller passed a section code | Exactly that slice |

**Name the slices you ran and the check count, and say plainly that the rest
were not evaluated.** A subset reported as "all checks pass" is precisely the
state this skill was in for months (guard-1760 — an instrument that reports what
it ran and never what it declined to look at).

#### Adding a new check

Author it in the registry, never inline in this file. An inline check re-creates
the growth that made this file unreadable and — because the ceiling truncates
silently — would take effect for nobody while appearing to be installed.

1. Add it with one command — pass `--dry-run` first to see what will move:

   Bash: `bash core/scripts/verify-check-registry.sh add --section <CODE> --check '<text>' --why '<rationale>'`

   It inserts after the section's last block and renumbers every later record.
   That renumbering is why this is a command and not a hand-edit: `seq` is
   positional and must stay strictly increasing, so inserting in the middle of
   a 1.8 MB JSONL by hand means rewriting thousands of records — which is how
   an add path turns into "I'll just inline it here instead".
2. Prove the registry still round-trips:

   Bash: `bash core/scripts/verify-check-registry.sh verify`

   This regenerates the source text, re-parses it, and requires every block back
   identical. It needs no external file, and it fails on a corrupted `raw`, a
   duplicated `seq`, or a broken indent.
3. Confirm the count moved:

   Bash: `bash core/scripts/verify-check-registry.sh count`

`extract --write` REBUILDS the registry from a full-corpus source. Since this
file is thin, running it against SKILL.md would parse a handful of checks and
delete the rest; a shrink guard refuses that. Rebuild from an archived full copy
with `--source <path>`.

`core/config/verification-checklist.md` remains the comprehensive reference
catalog (2000+ items) for per-section deep dives via targeted reads. It is a
reference, not an evaluation source — this skill does not run it.

## Step 4: Summary Report

The Step-4 sections are in the same registry, grouped under the step
`Step 4: Summary Report` in the `sections` index. Load them the same way, with
the same paging discipline:

   Bash: `bash core/scripts/verify-check-registry.sh show --section <CODE>`

Requesting the whole step at once returns 163,329 bytes and will page; prefer
naming the sections you actually need.

Provide a summary table:
- Total PASS / FAIL / N/A per section
- List of any FAIL items that need attention

## Chaining
- Calls: nothing
- Called by: User only. NEVER by Claude.
