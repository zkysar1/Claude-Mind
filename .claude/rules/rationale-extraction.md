# Rationale Extraction (core/config/rationale/)

## Principle

When extracted pseudocode (digests, size-budgeted specs, shared loop bodies) needs
multi-paragraph explanation of WHY it looks the way it does, that explanation
belongs in `core/config/rationale/<kebab-case>.md` — NOT inline in the source
(would inflate the hot-path budget), NOT in the reasoning bank (not
time-anchored experience), NOT in the knowledge tree (not domain knowledge),
NOT in a conventions file (not a reusable schema).

## Niche

Rationale files hold multi-paragraph design reasoning for a SPECIFIC piece of
extracted code. They answer "why does this phase structure exist" for a future
reader who sees only the surface pseudocode. They do not generalize beyond
their pointer source.

Distinct from:
- **Inline comments**: single-sentence WHY, no structural reasoning
- **Reasoning bank (`world/reasoning-bank.jsonl`)**: time-anchored incidents,
  lessons learned, "this failed because X"
- **Conventions (`core/config/conventions/`, `world/conventions/`)**: reusable
  schemas, script APIs, protocols that multiple call sites consume
- **Knowledge tree (`world/knowledge/tree/`)**: domain knowledge, facts about
  the world the agent operates in

## When To Extract

Extract when ALL of the following hold:

1. **Multi-paragraph WHY**: 2+ short paragraphs, or a table + prose. Single
   sentences stay inline.
2. **Non-obvious structural choice**: the file clarifies why 4 blocks exist, why
   a reset fires at a specific point, why a redundant check is intentional, or
   similar structural non-obviousness.
3. **Size-budgeted source**: the pseudocode lives in a digest or spec where the
   hot-path reload cost is the whole point of the extraction (inlining would
   waste the reload savings).
4. **Cross-references**: the rationale points at other parts of the system
   (sibling rationale files, rb/guard entries, SKILL.md phases).

## When NOT To Extract

- Single-sentence explanation → inline comment
- Time-anchored "this broke because X" → reasoning bank entry
- Reusable schema consumed by multiple call sites → conventions file
- Domain fact about the world → knowledge tree node

## Consistent Structure

```
# Rationale: <Title>

Referenced from `<pointer-to-source>`. <1-line purpose statement>.

## Why <design choice 1>
<paragraphs explaining the first choice>

## Why <design choice 2>
<paragraphs explaining the second choice>

## Cross-references
- rb-NNN — related lesson
- guard-NNN — related guardrail
- `<sibling-rationale>.md` — related extraction
- `<SKILL.md path>` Phase N — consumer of this rationale
```

## Pointer Format in Source

The pointer line in the extracted pseudocode MUST follow this format:

```
# Rationale (WHY <phrase>): core/config/rationale/<kebab-case>.md
```

The filename names the phase/mechanism (`signal-mutation.md`,
`circuit-breaker.md`), NOT the rb/guard that motivated it (no
`rb-XXX-fix.md`). This keeps the filename stable if the underlying
incident catalog is renumbered.

## Distinguishing Test

If >2 inline-comment lines are needed to explain a structural choice AND
future readers will trip over the surface pseudocode without that context,
extract to `core/config/rationale/`. Otherwise inline.

## Anti-patterns

- Extracting a single-sentence comment to a standalone file (over-extraction —
  the comment does the job)
- Putting incident analysis in a rationale file instead of the reasoning bank
  (rationale is "why the code looks this way," not "why we wrote this code")
- Naming rationale files after the rb number that motivated them
  (`rb-349-prose-filter.md`) — use the mechanism name instead
  (`prose-filter-pattern.md`)
- Duplicating content between a rationale file and a sibling conventions file
  (conventions describe schema + API; rationale describes structural reasoning —
  never both in the same file)

## Origin

Pattern formalized 2026-04-19 to 2026-04-20 across three rationale files
(`signal-mutation.md`, `circuit-breaker.md`, `maintenance-tick.md`). Formal
convention decided in g-115-127 (rb-375), filed as user-gated goal g-115-140.
The three existing files already implement the pattern — this rule makes it
discoverable for future extractions.
