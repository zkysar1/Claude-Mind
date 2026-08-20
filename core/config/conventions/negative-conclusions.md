# Negative Conclusions Convention

A negative conclusion is any claim that something CAN'T be done, IS broken,
DOESN'T work, or ISN'T available. These are uniquely dangerous because they
prevent work — often silently blocking multiple goals for extended periods.

## What Counts as a Negative Conclusion

- "Service X is not running"
- "Infrastructure Y is unavailable"
- "This approach won't work"
- "The data doesn't exist"
- "This endpoint/API/path is wrong"
- "The test failed" (when the test harness itself might be broken)

What is NOT a negative conclusion (no special treatment needed):
- "Goal completed successfully" (positive conclusion — Phase 5 handles this)
- "Value X equals Y" (factual observation, not a negation)
- "This code has a bug" (assertion about presence, not absence)

## Independent Verification Signals

Two signals are "independent" when they use different evidence paths:

| Signal 1 | Independent Signal 2 | NOT Independent |
|----------|---------------------|-----------------|
| curl to endpoint A | curl to endpoint B | Same curl retried |
| HTTP health check | Process/PID check | Same HTTP check with different flag |
| SSH command fails | `infra-health.sh check` | Same SSH retried |
| File not found at path A | Search for file by name | Same path checked twice |
| API returns error | Check API logs/status | Same API call retried |

## Cost-Proportional Verification Tiers

| Downstream Cost | Required Signals | Additional |
|-----------------|-----------------|------------|
| Blocks 0 goals | 1 signal sufficient | — |
| Blocks 1-2 goals | 2 independent signals | — |
| Blocks 3+ goals or creates a blocker | 2+ signals | Must try at least one alternative approach |

"Blocks" means the conclusion would prevent execution of those goals — either via
a formal blocker or by the agent choosing to skip/defer them.

## Silent Failure Catalog

These commands/flags produce ZERO-information results. Empty output from
these means "I don't know," not "it's down":

| Pattern | Why It's Zero-Information |
|---------|-------------------------|
| `curl -sf` | `-f` fails silently on HTTP errors; `-s` suppresses all output. A 404 looks identical to connection refused. |
| `curl -s ... 2>/dev/null` | Swallows both the response and error output |
| `command 2>/dev/null` | Hides the error that would explain the failure |
| `command \|\| true` | Masks the exit code |
| `grep -q` | Returns exit code only — no way to distinguish "not found" from "file error" |
| `test -f` on remote paths | SSH wrapper failures look like "file doesn't exist" |

When a silent-failure command returns empty: re-run WITHOUT the silent flag to see
what actually happened. Then that verbose output counts as signal 1.

## Enforcement Points

1. **Phase 4.0** (fast-path SKIP): Before CREATE_BLOCKER, verify the failure with
   a second independent signal. If the initial failure came from a silent-failure
   command, it counts as zero signals.

2. **Phase 2.5** (metacognitive assessment): When infrastructure probing concludes
   a component is down, record the conclusion in working memory with evidence count.

3. **Phase 0.5b** (blocker resolution): Active reprobing already happens every
   iteration. The convention adds: if a re-probe contradicts the blocker's original
   conclusion, clear the blocker immediately.

4. **Any inline decision**: When the agent decides mid-execution that something
   doesn't work and plans to skip or defer, apply the multi-signal requirement
   before accepting the conclusion.

5. **Phase 5 Q2 (aspirations-verify)**: Automated gate wire-up — a negation
   claim in the result text or Q2 failure-mode answer is routed to the
   appropriate gate script. See `aspirations-verify/SKILL.md` Q2 NEGATIVE CHECK.

## Statistical / Audit Negations (rb-245)

A class of negative conclusion distinct from operational and knowledge
negations: claims about *data values across many records*. Examples:

- "98% of records have times_triggered=0" <!-- DRIFT-EXEMPT: anti-pattern illustration -->
- "0 goals have priority=HIGH"
- "all experience entries are missing the verified_values field"
- "N/M pipeline records lack resolution_criteria"

These look authoritative but fail silently when the field name drifted. The
2026-04-17 guardrail-utilization audit concluded "98% zero-utilization" by
aggregating `times_triggered`; the real counter was `utilization.times_active`.  <!-- DRIFT-EXEMPT: anti-pattern illustration -->
The conclusion had to be retracted the next iteration.

### Protocol

Before accepting a statistical/audit negation, BOTH must hold:

1. **Schema probe**: A `core/scripts/jsonl-field-probe.py` run confirmed the
   field exists in at least one sampled record. The probe is signal 2 — the
   statistical claim itself is signal 1.
2. **Gate pass**: `core/scripts/zero-count-gate.py` exits 0 with
   `--file-probed`, `--field-probed`, and `--probe-result="found"`.

A probe result of `missing` means the field does not exist in the sample —
the statistical claim is almost certainly a schema-drift artifact and must
not be accepted.

### Enforcement

- `aspirations-verify/SKILL.md` Q2 NEGATIVE CHECK invokes the gate for any
  matching claim. A gate exit of 1 maps to Q2 FAIL and the goal goes back
  to pending, same as the infrastructure and knowledge gates.
- The gate is fail-open on internal errors: it prefers letting a legitimate
  claim through to silently suppressing output. Integrity of the enforcement
  surface comes from repeated use across verification cycles, not from the
  gate being a hard wall.

### Two gates, one principle (g-115-58)

Two scripts enforce rb-245 at different points in the audit lifecycle —
they are **complementary, not redundant**:

| Gate | When it fires | Input | What it checks |
|------|---------------|-------|----------------|
| `audit-schema-gate.py` | BEFORE an audit runs | Caller passes file path + field names | Samples live records; blocks if any claimed field is absent or always-null across the sample |
| `zero-count-gate.py` | AFTER a claim is made | Triggered by aspirations-verify Q2 on phrases like "98% have X=0" | Scans the claim text for statistical-negation patterns; requires a separate `jsonl-field-probe.py` run as signal 2 |

**Call order for a new audit/analysis script**:
1. Call `audit-schema-gate.py` with the fields you plan to aggregate over.
   If the gate exits non-zero, the audit is misconfigured — STOP, do not
   proceed to aggregate. This prevents producing the false-authoritative
   claim in the first place.
2. If schema-gate passes, run the aggregation.
3. When the resulting claim reaches aspirations-verify Q2, zero-count-gate
   catches any claim that bypassed step 1 or that used unscoped field
   inference. This is the belt-and-suspenders layer.

The pre-audit gate (step 1) is load-bearing for new code; the post-audit
gate (step 3) is the safety net for code paths that don't yet call
step 1.

## Integration with infra-health.sh

`infra-health.sh check <component>` always counts as 1 real signal (it runs
actual probe scripts, not silent commands). When verifying an infrastructure
conclusion:
- Signal 1: the original failure (unless silent → 0)
- Signal 2: `infra-health.sh check <component>`
- If both agree (down): conclusion accepted
- If they disagree: try a third method before accepting either

## Recording Conclusions

When a negative conclusion is accepted (sufficient signals), record it in the
`conclusions` working memory slot (see working-memory convention). Include:
- The conclusion text
- Evidence signals with weights (0 = silent/zero-info, 1 = real)
- Which goals it blocks
- A re-verify timestamp (30 min for blocking conclusions)


## Claim classes beyond the operational negation (moved from `.claude/rules/verify-before-assuming.md`, 2026-08-17, g-115-6581)

The rule keeps one imperative per class; the mechanism, enforcement wiring
and canonical incidents live here.

### Hand-written parsers are silent failures (rule 4)

**A hand-written parser over a framework script's output is the same class,
and it is the common one.** A `try/except` (or `|| echo 0`) around a parse
you wrote this turn converts a shape mismatch into a confident zero. Sibling
scripts do not agree on shape — measured: `board-read.sh --json` emits
**JSONL**, `guardrails-read.sh` emits a **pretty-printed JSON array**, so a
whole-stream `json.load` is right for one and silently yields 0 for the
other. Before writing the parser, print the SHAPE (type, plus keys if dict /
len + first element if list) and the BYTE COUNT next to the record count, in
the same call: `bytes: 8044449` beside `records: 0` is self-refuting, while
`records: 0` alone reads as a measured zero. Cost is one probe per script,
not per parse (measured: 161 distinct scripts across 4,108 parse events —
1.4% of Bash calls). Do NOT reach for retrieval here: querying the bare
script name returns **zero** guardrails, and the query that does surface the
lesson requires already suspecting the shape. guard-2298 carries the full
form; its `times_active` is 932 against `times_helpful` 4, which is why the
habit is stated here rather than left to be retrieved.

### A task notification's exit code is not the command's exit code (rule 4a, g-115-3202)

4a. **A background task's reported exit code is not the command's exit code**
(g-115-3202). When a `<task-notification>` arrives saying "completed (exit
code 0)", that is a claim about the process the harness launched, and it is
routinely a different number from the one you care about: a trailing pipe
replaces it with the pipe's status (guard-1150), a runner that classifies its
own result writes the verdict to a LOG rather than to `$?`, and a wrapper
that fails open exits 0 by contract. **Read the log before accepting the
verdict.** Measured: a notification reported exit 0 while the same run's log
read `RUNNER_EXIT=2 VERDICT=INVALID`.

This is encoded four times over — guard-1431, guard-1341, guard-1150,
guard-1096 — and still landed a 4th time in one session, so treat it as a
RETRIEVAL problem, not a knowledge one: the moment of acceptance is where the
obligation has to be present, and nothing surfaces it there. **No hook is
possible at that moment** — a task notification is not a tool call, so
PreToolUse has nothing to intercept (same structural limit as the chat lane
in `capability-before-user.md` § The Fourth Surface). The one real chokepoint
is the Read of the task's output file, which is where the discipline has to
fire by hand. Catalog entry: `retrieval-triggers.md` G21.

### Statistical negations at blocker-creation time (rule 5)

At blocker-creation time: `core/scripts/blocker-create-gate.py` (Step 2.55
of CREATE_BLOCKER) also enforces this. A blocker whose `failure_reason`
matches a statistical-negation pattern ("0 records", "all N have",
"none have", "missing field", "N% of records have Y=0") must include a
`schema_probe_evidence` field showing the claimed field was read from a
live record. Multi-signal rule 1 is enforced by the same gate: `evidence[]`
must have ≥2 entries with distinct tool/endpoint/evidence_type and
silent-failure commands (`-sf`, `-q`, `2>/dev/null`, `--silent`,
`--quiet`) count as zero.

### Positive file-state claims

Negations are not the only dangerous assertions. A POSITIVE claim about a
file's existence, mtime, contents, or last-updated field — stated without an
in-turn read — is equally unverified. The failure mode: narrating from stale
context (prior session, summary memory, model prior) rather than from the
file as it exists right now.

Rule (kept in the rule file): a claim about a specific file's existence, mtime,
or content requires that file to have been Read, `ls`-probed, or `stat`-probed
in the same turn as the claim.

Enforced by `core/scripts/positive-state-gate.py` — takes the claim text plus
a concatenation of in-turn tool outputs, pattern-matches positive file-state
assertions (e.g., "handoff.yaml reflects session N", "X was updated at T",
"file Y contains Z"), and exits 1 when the claimed path is not present in
the evidence. Fail-open on parser errors. Called from aspirations-verify
Q1 evidence check; advisory in agent-completion-report and
aspirations-state-update Step 8.75.

Anti-pattern (canonical incident): Asserting "handoff.yaml reflects session
50" when the file did not exist on disk. The prior session's summary claimed
session 50, and that claim was reused without re-reading. Fix: read the
file (or `ls` the directory) before narrating its contents, every time.

### Post-insertion verification

A claim that an Edit or Write "succeeded" is not the same as a claim that the
expected content is on disk right now. The Edit tool returns success as soon
as the string replacement happens, but subsequent events — linter touches,
parallel edits from another session, or a stale pre-autocompact summary
carrying forward — can leave the file in a different state than the LLM
remembers. The failure mode: asserting "Step 8.78 is at line 606, verified
intact" from memory, when a fresh grep returns zero matches.

The three imperatives (verify-on-insert; re-verify after a linter/user
notification; summaries are claim snapshots, not filesystem snapshots) stay in
the rule file.

Canonical incident (2026-04-20, asp-248 shape alpha): Pre-autocompact summary
claimed "Step 8.78 inserted at line 606, verified intact after linter touched
file." Post-autocompact verify-learning smoke test grep returned zero matches
for `post-state-update-gate.sh` in `aspirations-state-update/SKILL.md`. Root
cause: either the linter removed the step or the earlier "verified intact"
was a false positive matching a different reference. Fix: re-insert
immediately, then re-grep before proceeding. Applied protocol going forward:
verify-on-insert after every Edit.

### Causal attribution claims

Beyond positive and negative state claims, a third category slips through:
claims about WHY existing state is the way it is. The failure mode is
inferring a cause→effect link from co-existence ("X is the way it is
because Y exists") rather than observation, then writing the inference as
if it were established fact. Plausible mechanisms are not the same as
observed mechanisms.

The rule (causal connectors only on directly observed cause→effect evidence;
otherwise one of the three neutral forms) stays in the rule file.

Causal claims appear most often in:
- code comments explaining why existing code or state is the way it is
- reasoning-bank `content` and `failure_lesson` fields
- knowledge-tree node summaries that narrate a system's history
- verify-summary paragraphs that try to explain a failure
- goal descriptions that justify the work by attributing prior failure to a cause

The same rigor applies to all of them: same-turn evidence, or no causal
language.

Canonical incident (rb-734, 2026-05-08): a comment in
`aspirations-spark/SKILL.md` sq-013 handler claimed "the 16 sq-013-derived
goals all carry valid origin_signal because the LLM has been compensating
after rejection." Two observations (16 goals exist + a gate exists that
rejects missing signals) were chained into a causal claim without checking
git history, audit log, or any trace that proved the link. The 16 goals
could equally come from organic LLM judgment with the gate never having
fired. The codification work itself (the discovery_type → origin_signal
mapping) was correct; only the WHY-comment was unfounded. Fix going
forward: when explaining why existing state exists, write observation +
plausible-mechanism separately, never blurred.

#### Separating them is not enough — POSITION decides whether the hedge is read (2026-08-19, g-115-6823)

The fix above ("write observation + plausible-mechanism separately, never
blurred") was FOLLOWED and still failed, which is why this sub-section exists.
guard-4449 was filed carrying a correct neutral form in its own text:

> Mechanism INFERRED as an argv/exec length ceiling [...] — measured behaviour
> is certain, the exact cause is not.

That clause sat ~1,000 characters into the `rule` field (measured 1,035 on the
recorded original). The field OPENED with
`tree-update.sh --set passes VALUE as a positional argv and SILENTLY TRUNCATES
a large one`, and the companion goal title was `Fix: tree-update.sh --set
SILENTLY TRUNCATES a large value at ~8KB` — no hedge at all. Both assertions
were falsified within the hour (`--set` reproduced clean at 12,000 / 17,708 /
20,000 B through the exact failing shape). The observation was real; only the
mechanism was invented.

**Why the head of the field is the whole budget.** guard-1421 measured, across
1,382 active guardrails, that a survey slice shows the topic and hides the
imperative ~85% of the time, and that no slice width fixes it. Its remedy is to
survey by **id + title ONLY**, treating rule text as absent.

⚠ **That remedy is not implementable as written, and this file asserted the
wrong reason for it until 2026-08-19.** The original claim here — "a guardrail
record has NO title field" — is FALSE: ~12% carry one (5 of a 40-record live
sample via `retrieve.sh`; the reasoning bank is 100%). The true constraint is
the instrument, not the schema. `guardrails-read.sh --summary` — the actual
survey surface, 4,190 lines — emits `id: [category] <rule truncated>` and
**never the title, even for records that have one** (verified on guard-1244,
which does). So there is no title lane to survey by, for any record.

This STRENGTHENS the writer-side rule rather than weakening it. The head of
`rule` is what a survey shows for 100% of guardrails, titled or not — so a
hedge placed anywhere but that head is unread BY CONSTRUCTION, and the entry
radiates a confidence its author did not intend. (Measured alpha,
DESKTOP-O91DLK2, 2026-08-19, during the /encode-session pass that shipped this
section — found by reading guard-1244 for an unrelated reason and noticing it
had a `title`. The section's advice survived; its stated justification did not.)

**This is not guard-1421's rejected "tune the slice."** That was a READER-side
move — widen the window until the payload fits — and guard-1421 killed it with
numbers (at 200 chars you still miss 70%), because the author's payload position
and the reader's cut point are uncorrelated. The rule here is the WRITER-side
complement: move the payload into the window that already survives. It is what
makes guard-1421's own remedy epistemically sound rather than systematically
over-confident, so the two compose — do not read them as opposed.

**Scope is deliberately two surfaces, not "everywhere"** (guard-336): a goal's
`title` and the first clause of a guardrail's `rule`. Those are the two measured
here and the two that survive truncation on the paths that matter (selector
scan, `--summary` index, human skim). Widening it to "every field" would be a
universal quantifier with no enforcement behind it.

#### Derive the deliverable from whichever half is MEASURED

The companion failure is scope, not wording. A defect filing carries a measured
OBSERVATION and an inferred MECHANISM; whoever executes it inherits the
mechanism as scope, so falsifying the mechanism destroys the goal.

Worked example, same incident. g-115-6823 was scoped to the mechanism — "add a
length cap to `--set` / route it through the stdin path" — and became worthless
the moment `--set` was cleared, requiring a full rewrite of title and
description. Re-scoped to the OBSERVATION ("a silent partial write happened and
nothing in the tree-write path compares stored bytes against sent bytes"), the
deliverable is correct under every candidate mechanism including the one still
unknown, and it survived the falsification that killed the original.

**The rule is conditional, not absolute.** Where the mechanism IS measured,
scope to it — that is the smallest correct fix, and reaching for a general
detector instead is the speculative-feature anti-pattern
(`implementation-discipline.md` rule 2). Where the mechanism is INFERRED, the
deliverable must be one that stays correct if it is falsified. Ask before
filing: *if the mechanism turns out to be wrong, is any of this work still
right?* If the answer is no, the goal is scoped to a guess.

Distinct from `retrieve-before-deciding.md` #11, which fires on the same filing
moment: #11 says CONSULT the corpus about the remedy before writing it down;
this says DERIVE the remedy from the measured half. #11 can be satisfied in full
and still leave a goal that dies with its mechanism.

### Capability-absence claims ("Y needs to be built")

"Y needs to be built", "X must be created", "we need to add Z", and "there is
no support for W yet" are negative conclusions too -- the build-side twin of
"X doesn't exist". Both assert that a capability does NOT currently exist, and
both misdirect work when wrong: the symmetric failure mode is building a
duplicate of something that already exists (a script, a gate, a tree node, a
convention) because no search was run first.

Rule: Before concluding that something must be built, created, or added, apply
the same discipline as any other negative conclusion -- the multi-signal
requirement (rule 1) plus the exhaustive knowledge search of
`core/config/conventions/exhaustive-search-before-negation.md`. Search the
codebase, the skill/script registry, `world/forged-skills.yaml`, the knowledge
tree, and `world/conventions/` for an existing implementation BEFORE filing
build work; only when 2+ independent surfaces come back empty is "needs to be
built" verified rather than assumed. Same root as
`.claude/rules/encode-stable-facts.md` "retrieve before discovering" and
`.claude/rules/capability-before-user.md` "check provisionability first". A
goal-creation-time gate that flags build-verb goals lacking a prior-search
note is a possible future hardening, deferred until the cycle detector shows
the pattern is frequent enough to warrant it (g-305-10 scope decision).
