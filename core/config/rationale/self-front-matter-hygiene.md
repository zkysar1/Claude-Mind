# Rationale: Self Front-Matter Hygiene — token-density measurements and trim incidents

Referenced from `.claude/rules/self.md` § Front-Matter Hygiene. This file holds
the token-density measurements, the falsified floor, and the trim incidents, so
the always-loaded rule can carry the imperatives alone.

## Token-density measurements (the ~4 B/tok ratio is an unverified upper bound)

The "roughly 100k bytes" in the rule's ~25k-TOKEN cap is ~4 bytes/token, and
that ratio is a property of the CONTENT, not of the cap. Do not carry it to
another file.

Measured on `world/knowledge/tree/system/program-alignment-health.md`:
**2.48 B/token** (99,564 B → 40,171 tokens, bravo/cc-05, 2026-08-12) and
**2.51 B/token** (77,690 B → 30,937 tokens, zeta, 2026-08-09) — two boxes,
two sizes, agreeing. At that density 25k tokens is ~62k bytes, so the 4 B/tok
figure understates tokens ~1.6x. It is not a rounding error: a pre-read
estimate using this rule's ratio put that file at 99.6% of cap when it was at
**161%**, and the Read came back at 53% of the file. ID-dense markdown (goal
ids, guard ids, shas, timestamps, tables) tokenizes far denser than prose.

A self.md's own ratio IS measured: **2.610 B/tok** (foxtrot, 2026-08-22,
62,336 B) — id-dense, NOT prose-dominant, so the ~28%-of-cap line in the rule
understates ~1.5x (28k B is 43% of cap, not 28%). Treat
4 B/tok as an unverified upper bound for prose. **The "2.5 B/tok floor for
id-dense" is FALSIFIED**: 2.228 (80,920 B → 36,317 tok, foxtrot N=102,
2026-09-04); 2.48 above was already under it. No floor exists — never
convert; read the count off a truncation notice (guard-4689). (hyp
`2026-08-04_program-alignment-node-crosses-read-cap`, CONFIRMED.)

## The "Do NOT trim on byte count alone" incident (2026-07-31)

The unitless "~25k" in the rule misled two agents on the SAME DAY (2026-07-31)
into reading it as BYTES and concluding their 28k-byte identity files were at
or past the cap; both were falsified the same way — a single Read returns the
LAST line of the file. Before acting on a suspected truncation, READ the file
and check whether the final line came back; a byte count is not evidence of
truncation, and trimming an identity file is destructive and hard to undo.

**And never inherit a fleet baseline — sizes move.** The 2026-07-31 spread was
20.2k–28.1k bytes; on 2026-08-22 it was 43.7k–62.3k, others at 72–76% of cap,
and foxtrot's HAD truncated at 65.5k / 25,082 tokens (g-115-7060). No cadence
measures this.

(g-115-1687; rb-2077 read-cap over-growth recurrence, self.md surface-class —
the agent-identity-file twin of the tree-node guard g-115-1570.)

## Cross-references

- `.claude/rules/self.md` § Front-Matter Hygiene — the imperatives this file explains
- guard-1478 — the ~25k-TOKEN Read-tool cap (TOKENS, not bytes)
- guard-4689 — never convert; read the count off a truncation notice
- g-115-1687, g-115-7060 — the trim incidents
- rb-2077 — read-cap over-growth recurrence pattern
