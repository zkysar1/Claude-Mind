# Rationale: Shape-fork measurement cases (tree maintain DISTILL step 1.6)

Referenced from `.claude/skills/tree/SKILL.md` DISTILL Step 1.6 (SHAPE FORK). The
measured cases behind two instructions there: measure BYTES per section, and route a
node by what its dominant section IS. The tool's own WHY (bytes are the primary unit,
thresholds are read and never hardcoded) lives in the docstring of
`core/scripts/tree_shape_fork.py`; this file holds only the numbers that docstring
does not carry.

## Why the unit is bytes, and what a line-span profile cost

Step 1.6 said "line spans" until 2026-08-17. Measured that day (foxtrot, `hostname`
LAPTOP-3IOFCNEO, `uname -r` 6.6.87.2-microsoft-standard-WSL2) on a per-agent series
shard (an index TABLE plus dated narrative entries): its `## Series` TABLE section is
456 B/line against 63 B/line for the narrative entry sections, so by lines it is 6.4%
of the node and by bytes it is 32.5%.

The consequence is not academic. A RANGED Read of just that node's first 215 lines
returned 35,462 tokens and was REFUSED for exceeding the 25,000 cap, so the fork's own
prescribed measurement step could not complete on a table-dense node while the line
count looked small. Tables, `|`-rows, id lists and timestamps tokenize far denser than
prose (the same density trap `.claude/rules/self.md` measures at 2.48-2.51 B/token for
ID-dense markdown). One call to `tree-shape-fork.sh` profiles the node without the Read.

## The two live shape-(d) cases (one section dominates)

Both measured 2026-08-17 (foxtrot, LAPTOP-3IOFCNEO, 6.6.87.2-microsoft-standard-WSL2):

- A per-agent SERIES SHARD. Its `## Series` index TABLE is 82,104 of 252,815 B (32.5%)
  and about 35.7k est tokens, i.e. over the 25k cap by itself. Archiving every one of
  its 28 dated entries still leaves the node 1.46x over cap. Untouchable by rollup.
- A FAILURE-MODE CATALOG node: 284,057 B, 4.9x cap, `refresh_sections: 30`,
  `recommended_action: distill`. Its cost was 145,425 B (51%) of dated `### n=NN` cycle
  entries accumulated under a `## Cross-references` heading, plus a second 72,423 B
  (25%) append series. 76.7% of a "catalog" node was series, filed where no heading
  said so.

The skill keeps the routing consequence (route by what the dominant section is, never
by the crit3 label); these numbers are the evidence for it.

## Cross-references

- `core/scripts/tree_shape_fork.py` docstring -- why bytes are the primary unit
- `.claude/skills/tree/SKILL.md` DISTILL Step 1.6 -- the consumer (guard-2109, rb-6055)
- `core/config/verify-learning-checks.jsonl` -- `tree-shape-fork-precedes-archive`
- g-115-10095 -- needed about 1 KB of room in the skill and paid for it with this
  extraction (`core/config/conventions/hot-path-size-budget.md`, Tier 2 ceiling)
