# Verify Before Assuming

## Principle

Never accept a negative conclusion from a single signal. Negative conclusions are
claims that something CAN'T be done, IS broken, DOESN'T work, or ISN'T available.
They are uniquely dangerous because they prevent work.

## Rules

1. **Multi-signal requirement**: A negative conclusion requires 2+ independent
   verification signals before acceptance. "Independent" means different tools,
   different endpoints, or different evidence types.
2. **Cost-proportional verification**: If the conclusion blocks multiple goals
   or hours of work, require more signals and try harder to disprove it.
3. **Infrastructure-specific**: MUST NOT declare infrastructure unavailable
   without running `infra-health.sh check <component>`.
4. **Silent failure awareness**: Commands with silent-failure flags (`-sf`, `-q`,
   `2>/dev/null`) are ZERO signals, not one. A silently-failed command that
   returns empty output has told you nothing.
5. **Statistical / audit negations require schema verification** (rb-245):
   Before concluding "field Y has value 0 across N records" or "N records
   are missing field Y," read ONE record and verify field Y exists in the
   schema. A zero-count audit against a misspelled, renamed, or nonexistent
   field produces false positives that look authoritative. The schema
   probe IS signal 2. Enforced by `core/scripts/zero-count-gate.py` (called
   from aspirations-verify Q2); use `core/scripts/jsonl-field-probe.py`
   to produce the probe evidence.

   At blocker-creation time: `core/scripts/blocker-create-gate.py` (Step 2.55
   of CREATE_BLOCKER) also enforces this. A blocker whose `failure_reason`
   matches a statistical-negation pattern ("0 records", "all N have",
   "none have", "missing field", "N% of records have Y=0") must include a
   `schema_probe_evidence` field showing the claimed field was read from a
   live record. Multi-signal rule 1 is enforced by the same gate: `evidence[]`
   must have ≥2 entries with distinct tool/endpoint/evidence_type and
   silent-failure commands (`-sf`, `-q`, `2>/dev/null`, `--silent`,
   `--quiet`) count as zero.

## Positive File-State Claims

Negations are not the only dangerous assertions. A POSITIVE claim about a
file's existence, mtime, contents, or last-updated field — stated without an
in-turn read — is equally unverified. The failure mode: narrating from stale
context (prior session, summary memory, model prior) rather than from the
file as it exists right now.

Rule: A claim about a specific file's existence, mtime, or content requires
that file to have been Read, `ls`-probed, or `stat`-probed in the same turn
as the claim. Claims about file state without in-turn evidence are unverified
and MUST NOT be stated as fact.

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

## Post-Insertion Verification

A claim that an Edit or Write "succeeded" is not the same as a claim that the
expected content is on disk right now. The Edit tool returns success as soon
as the string replacement happens, but subsequent events — linter touches,
parallel edits from another session, or a stale pre-autocompact summary
carrying forward — can leave the file in a different state than the LLM
remembers. The failure mode: asserting "Step 8.78 is at line 606, verified
intact" from memory, when a fresh grep returns zero matches.

Rules:

1. **Verify-on-insert**: After any Edit or Write to a framework file
   (SKILL.md, core/scripts/, core/config/, .claude/rules/, world/conventions/),
   the next tool call in the same turn MUST be a Grep or Read that returns the
   inserted content. The grep output is the evidence. Do not proceed to the
   next task until the verification confirms presence.

2. **Re-verify after linter/user notification**: When a system-reminder says
   "file was modified by the user or by a linter," any prior claim about that
   file's contents is invalidated. Re-read or re-grep before restating the
   claim or building on it. The notification is a signal, not noise.

3. **Summaries are claim snapshots, not filesystem snapshots**: Post-autocompact
   summaries describe what the prior session INTENDED to write, not what is on
   disk now. Treat every "X is at line Y, verified intact" carried forward as
   a hypothesis requiring fresh evidence, not an established fact. This is
   the insertion-side complement to the Positive File-State Claims rule above.

Canonical incident (2026-04-20, asp-248 shape alpha): Pre-autocompact summary
claimed "Step 8.78 inserted at line 606, verified intact after linter touched
file." Post-autocompact verify-learning smoke test grep returned zero matches
for `post-state-update-gate.sh` in `aspirations-state-update/SKILL.md`. Root
cause: either the linter removed the step or the earlier "verified intact"
was a false positive matching a different reference. Fix: re-insert
immediately, then re-grep before proceeding. Applied protocol going forward:
verify-on-insert after every Edit.

## Causal Attribution Claims

Beyond positive and negative state claims, a third category slips through:
claims about WHY existing state is the way it is. The failure mode is
inferring a cause→effect link from co-existence ("X is the way it is
because Y exists") rather than observation, then writing the inference as
if it were established fact. Plausible mechanisms are not the same as
observed mechanisms.

Rule: A claim that uses causal language — "because", "due to", "as a
result of", "in response to", "compensating for", "caused by" — must rest
on directly observed cause→effect evidence (git log entries, telemetry,
dated audit trail, the agent's own first-person record of doing it). If
the link is inferred from correlation, do not use causal connectors. Use
one of these neutral forms instead:

- "observed: X. plausible mechanism: Y (inferred, not verified)"
- "observed: X (per <audit-trail or commit reference>). therefore: Y"
- "X exists; how it got that way is not recorded"

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

## Anti-patterns

- One failed curl = "it's down"
- `curl -sf` returns empty = "service not running" (silent 404 ≠ connection refused)
- SSH connection refused = "server is down" (could be stale host key)
- "I tried and it didn't work" without trying an alternative approach
- One tree search = "it's not built" (search multiple queries, categories, and data stores)
- "98% of records have X=0" without probing whether X is the correct field
  name (rb-245 — the original audit retracted after probing showed the
  counter was at a different nested path)
- "X is the way it is because Y caused it" without checking whether Y
  actually caused X (rb-734 — comment claiming sq-013 valid signals came
  from "LLM compensating after rejection" was retracted after acknowledging
  no trace was ever consulted; observation and inference must be stated
  separately, not blurred by a causal connector)

**Detail:** `core/config/conventions/negative-conclusions.md` for enforcement points,
verification tiers, and silent failure catalog.

**Knowledge-specific:** `core/config/conventions/exhaustive-search-before-negation.md`
for the exhaustive knowledge search protocol before concluding something doesn't exist.

**Statistical-specific:** `core/config/conventions/negative-conclusions.md`
"Statistical / audit negations" section for the schema-probe-first protocol.
