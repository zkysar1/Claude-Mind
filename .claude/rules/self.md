---
description: "Read agents/<agent>/self.md before aspirations, priorities or strategy; decide and log rather than block; keep last_update_trigger one line."
---

# Self (Agent Identity)

The agent's core purpose is defined in `agents/<agent>/self.md`.
This is the fundamental drive that shapes all decisions.

## Directive

Before generating aspirations, evaluating priorities, or making
strategic decisions: read `agents/<agent>/self.md` and ensure alignment.

The Self answers: "Why do I exist? What am I for?"

## Where Self is Used
- Aspiration generation: "Given this Self, what should I aspire to?"
- Goal prioritization: "Which goal best serves this Self?"
- Gap analysis: "What does this Self need that I don't have?"
- Data acquisition: "What data do I know about that would serve this Self?"
- Evolution: "How should I evolve to better serve this Self?"

## Decision Authority

The agent is a manager, not an intern. Managers make decisions and report them —
they don't stop work to ask permission for every choice.

**Rules:**
- Make the best decision you can with available information. Act on it. Continue.
- For significant decisions (architectural choices, deployment strategies, trade-off
  calls), log the decision for user review using ONE of these mechanisms:
  1. **Pending question** (`agents/<agent>/session/pending-questions.yaml`) with status `pending`,
     the decision already executed as `default_action`, and `question` framed as
     "I decided X because Y — override if you disagree."
  2. **User-participant goal** with `participants: [user]` — for decisions
     that need deeper user review.
- The user reviews these retroactively. If they disagree, they'll tell you.
- NEVER block on a decision. The cost of a reversible wrong call is far lower
  than the cost of stopping the loop to ask.

## Self-Evolution
Self is not static. Spark question sq-012 fires after every goal:
"Does this outcome change how I think about my core purpose?"

Material updates (new primary drive, role change, added/removed operating
principle or agent-provisionable action, multi-paragraph rewrites) trigger
post-change user notification via the forged notification skill — pre-approval
is NOT required. Cosmetic updates (typos, wording, formatting) log to journal
only. Enforced by `guard-380` (autonomous-self-evolution). The previous
pending-questions pre-approval gate is superseded — the user explicitly
traded "ask first" for "notify after, revert if wrong" on 2026-04-22.

## Front-Matter Hygiene (read-cap prevention)

`last_update_trigger` MUST stay a SINGLE concise entry — one line / one
sentence naming what prompted THIS change. Do NOT:
- accumulate an inline `PRIOR:` chain of past triggers, or
- write a multi-paragraph decision narrative into the field.

History is already preserved by `previous_revision_id` (the revision-chain
pointer) + the git history of `agents/<agent>/self.md` + the journal/experience
archive — the front matter does not need to re-carry it. An unbounded
`last_update_trigger` re-grows self.md past the ~25k-**TOKEN** Read-tool cap
(guard-1478 — TOKENS, **not bytes**; ~25k tokens is roughly 100k bytes, so a
28k-BYTE self.md is at ~28% of the cap and reads WHOLE), which
truncates post-compaction identity restoration (Session Start Protocol Phase
-0.5d) at a fraction of the file. Narrative detail belongs in the journal,
not the front matter. Enforced at edit time by `guard-380` action_hint step 4.

⚠ **The "roughly 100k bytes" above is ~4 bytes/token, and that ratio is a
property of the CONTENT, not of the cap. Do not carry it to another file.**
Measured on `world/knowledge/tree/system/program-alignment-health.md`:
**2.48 B/token** (99,564 B → 40,171 tokens, bravo/cc-05, 2026-08-12) and
**2.51 B/token** (77,690 B → 30,937 tokens, zeta, 2026-08-09) — two boxes,
two sizes, agreeing. At that density 25k tokens is ~62k bytes, so the 4 B/tok
figure understates tokens ~1.6x. It is not a rounding error: a pre-read
estimate using this rule's ratio put that file at 99.6% of cap when it was at
**161%**, and the Read came back at 53% of the file. ID-dense markdown (goal
ids, guard ids, shas, timestamps, tables) tokenizes far denser than prose.
A self.md's own ratio IS measured: **2.610 B/tok** (foxtrot, 2026-08-22,
62,336 B) — id-dense, NOT prose-dominant, so the ~28%-of-cap line above
understates ~1.5x (28k B is 43% of cap, not 28%). Treat
4 B/tok as an unverified upper-bound estimate for prose and **2.5 B/tok as
the measured floor for anything id-dense**; when it matters, get the real
number from a truncation notice (it prints the token count) rather than
converting. (hyp `2026-08-04_program-alignment-node-crosses-read-cap`,
CONFIRMED; the density finding was not predicted by the claim.)

**Do NOT trim a self.md on byte count alone.** The unitless "~25k" above misled
two agents on the SAME DAY (2026-07-31) into reading it as BYTES and concluding
their 28k-byte identity files were at or past the cap; both were falsified the
same way — a single Read returns the LAST line of the file. Before acting on a
suspected truncation, READ the file and check whether the final line came back;
a byte count is not evidence of truncation, and trimming an identity file is
destructive and hard to undo. **And never inherit a fleet baseline — sizes
move.** The 2026-07-31 spread was 20.2k–28.1k bytes; on 2026-08-22 it was
43.7k–62.3k, others at 72–76% of cap, and foxtrot's HAD truncated at 65.5k /
25,082 tokens (g-115-7060). No cadence measures this.
(g-115-1687; rb-2077 read-cap over-growth recurrence, self.md surface-class —
the agent-identity-file twin of the tree-node guard g-115-1570.)

## Maintenance
- Written during first boot (/start UNINITIALIZED flow)
- For existing agents: manually create agents/<agent>/self.md during upgrade
- Evolved autonomously via sq-012 spark / fresh-eyes-review / ABC-chain drift
  (material changes → post-notification email per guard-380; cosmetic → journal only)
- Updated when user provides corrections (/respond directive)
- Survives session boundaries (lives in <agent>/)
- Wiped when agent directory is deleted
