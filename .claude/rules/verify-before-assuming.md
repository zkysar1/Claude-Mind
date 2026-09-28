---
description: "Negative conclusions (broken, missing, cannot) need 2+ independent signals; silent-failure commands count zero; read the log, not the rc."
alwaysApply: true
---

# Verify Before Assuming

## Principle

Never accept a negative conclusion from a single signal. Negative conclusions are
claims that something CAN'T be done, IS broken, DOESN'T work, or ISN'T available.
They are uniquely dangerous because they prevent work.

Mechanism, enforcement wiring, and the canonical incidents behind every rule
below live in `core/config/conventions/negative-conclusions.md`
(`load-conventions.sh negative-conclusions`). This file keeps the imperatives.

## Rules

1. **Multi-signal requirement**: A negative conclusion requires 2+ independent
   verification signals before acceptance. "Independent" means different tools,
   different endpoints, different evidence types, or a different WORLD — Tier 2.5
   `peer-retrieve.sh` asks the peer Mind deployments, and only `status: empty` at
   `completeness: complete` is a signal at all; rc=3 / `partial` means a lane was
   blind, which is rule 4's silent failure arriving through a new door.
2. **Cost-proportional verification**: If the conclusion blocks multiple goals
   or hours of work, require more signals and try harder to disprove it.
3. **Infrastructure-specific**: MUST NOT declare infrastructure unavailable
   without running `infra-health.sh check <component>`.
4. **Silent failure awareness**: Commands with silent-failure flags (`-sf`, `-q`,
   `2>/dev/null`) are ZERO signals, not one. A silently-failed command that
   returns empty output has told you nothing. **A `try/except` or `|| echo 0`
   around a parse you wrote this turn is the same class** — sibling scripts
   disagree on output shape, so it converts a mismatch into a confident zero.
   Before writing the parser, print the SHAPE and the
   BYTE COUNT beside the record count in the same call: `bytes: 8044449`
   next to `records: 0` is self-refuting. (guard-2298; do not expect retrieval
   to surface this — it fires 932:4.)
4a. **A background task's reported exit code is not the command's exit code**
   (g-115-3202): a `<task-notification>` "completed (exit code 0)" is a claim
   about the process the harness launched — a trailing pipe, a runner that
   writes its verdict to a LOG, or a fail-open wrapper all return 0 over a
   failed run. **Read the log before accepting the verdict.** No hook can fire
   at that moment (a notification is not a tool call); the Read of the output
   file is the one chokepoint, and it fires by hand.
5. **Statistical / audit negations require schema verification** (rb-245):
   Before concluding "field Y has value 0 across N records" or "N records are
   missing field Y," read ONE record and verify field Y exists in the schema.
   The schema probe IS signal 2. Enforced by `core/scripts/zero-count-gate.py`
   (aspirations-verify Q2) and, at blocker-creation time, by
   `core/scripts/blocker-create-gate.py` (Step 2.55 of CREATE_BLOCKER —
   `schema_probe_evidence` required for statistical-negation
   `failure_reason`s; `evidence[]` needs ≥2 distinct-tool entries, silent
   commands count as zero); produce probe evidence with
   `core/scripts/jsonl-field-probe.py`.

## Positive File-State Claims

A POSITIVE claim about a file's existence, mtime, contents, or last-updated
field — stated without an in-turn read — is as unverified as a negation.
**Rule**: such a claim requires that file to have been Read, `ls`-probed, or
`stat`-probed in the SAME TURN as the claim; without in-turn evidence it MUST
NOT be stated as fact. Enforced by `core/scripts/positive-state-gate.py`
(aspirations-verify Q1; advisory in agent-completion-report and
aspirations-state-update Step 8.75). Canonical incident: "handoff.yaml
reflects session 50" narrated from a summary while the file did not exist.

A positive claim about the WORLD written into a knowledge node is the third
positive class — see `.claude/rules/ground-truth-citation.md` (path-scoped).

## Post-Insertion Verification

An Edit/Write "succeeded" is not evidence the content is on disk NOW.

1. **Verify-on-insert**: After any Edit or Write to a framework file
   (SKILL.md, core/scripts/, core/config/, .claude/rules/, world/conventions/),
   the next tool call in the same turn MUST be a Grep or Read that returns the
   inserted content. Do not proceed until it does.
2. **Re-verify after linter/user notification**: a "file was modified by the
   user or by a linter" reminder invalidates every prior claim about that
   file's contents. Re-read or re-grep before restating or building on it.
3. **Summaries are claim snapshots, not filesystem snapshots**: a
   post-autocompact summary describes what the prior session INTENDED to
   write. Treat every carried-forward claim as a hypothesis requiring fresh
   evidence.

## Causal Attribution Claims

**Rule**: causal language ("because", "due to", "caused by") must rest on
directly observed cause-effect evidence. If inferred from correlation, use
"observed: X. plausible mechanism: Y (inferred, not verified)". Place the
hedge in the goal TITLE and guardrail rule's FIRST clause (guard-1421).
Detail and neutral-form templates: `core/config/conventions/negative-conclusions.md`
(rb-734).

## Capability-Absence Claims ("Y Needs To Be Built")

"Y needs to be built" / "there is no support for W yet" is a negative
conclusion too — the build-side twin of "X doesn't exist"; when wrong it
builds a duplicate. **Rule**: before concluding something must be built, apply
rule 1 plus the exhaustive knowledge search of
`core/config/conventions/exhaustive-search-before-negation.md` (it carries
the surface list) — and only when 2+ independent surfaces come back empty is
"needs to be built" verified rather than assumed.

## Anti-patterns

- One failed curl = "it's down"; `curl -sf` empty = "not running" (silent
  404 ≠ connection refused); ping "0% loss" = "up" (guard-4902 — require
  `Reply from <TARGET>: bytes=`)
- "98% of records have X=0" without probing the field name (rb-245)
- "X because Y" without checking Y actually caused X (rb-734)
- One tree search = "not built" — run the Capability-Absence search above
- Trusting exit 0 without reading the log (guard-1431, guard-1096)

**Detail:** `core/config/conventions/negative-conclusions.md` — enforcement,
verification tiers, silent-failure catalog, all moved sections.
**Knowledge-specific:** `exhaustive-search-before-negation.md`.
**Statistical-specific:** `negative-conclusions.md` § "Statistical / audit negations".
