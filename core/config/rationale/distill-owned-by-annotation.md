# Rationale: `owned_by` on read-cap distill candidates

Referenced from `.claude/skills/tree/SKILL.md` DISTILL step 1 (OWNERSHIP block).
Explains why the read-cap rows carry an owner id, why it annotates instead of
filtering, and why a `null` must not be read as "unowned".

## Why the field exists at all

`--distill-candidates` emitted `trigger` and `recommended_action` and nothing
about ownership, so a reader of tree-debt output could not distinguish **"nobody
has looked at this"** from **"a goal owns it, pending"**. The disposition record
already existed — it is the per-node goal — and what had never been run was the
**join**.

The cost of not running it is measured, not hypothetical. Per the `g-115-4077`
goal record in `world/aspirations.jsonl` (read via
`aspirations-query.sh --goal-field id g-115-4077 --full`), which is also where
`sig-229` records the recurrence: the same eleven nodes were independently
re-censused on 2026-07-30 and again on 2026-08-30 — 31 days apart, same
population, two full investigations. Neither reader was careless; the output
simply did not say who owned the rows, so re-deriving the census was the only
way to find out.

## Why it ANNOTATES and never SUPPRESSES

The tempting design is to filter owned rows out of the candidate list. That
would break a pinned invariant. `distill_exempt` deliberately does **not**
suppress the read-cap arm (`core/scripts/tree.py`, pinned by
`test_distill_exemption_does_NOT_suppress_the_readcap_arm`), because a node that
is genuinely over the read cap stays over it no matter what any record says
about it. An `owned_by` that filtered would hide genuinely over-cap nodes behind
goals that may be stale, closed, or wrong — the precise opposite of what the
annotation is for.

So the row still appears and still takes the routing. The only thing that
changes is that it carries its owner. `test_distill_owned_by_annotation.py`
pins this directly: rows are identical whether or not an owner resolves, and
`owned_by` is present on every emitted row.

## Why `null` is weak evidence rather than proof

Three independent reasons, each of which alone would be enough:

1. **The join is a token match**, over goal titles and descriptions — not a
   declared foreign key. A goal that owns a node without naming it in either
   field resolves to `null`.
2. **It fails open by design.** Candidate production must never depend on the
   aspiration store being readable, so an unreadable or missing store yields
   `null` for *every* row rather than an error. A blanket `null` therefore looks
   exactly like "nothing is owned". Fail-open is correct — a weaker annotation
   beats a broken producer — but it makes the negative uninformative.
3. **Terminal goals still match.** A `null` is the absence of any match at all,
   not evidence that no goal ever covered the node.

Treat a non-null value as a lead worth following and a `null` as "the join found
nothing", never as "no owner exists".

## Why the ranking is title-first, then open-first

Recall and precision pull in opposite directions here, and both were measured
against the real corpus rather than assumed:

| axis | why both halves are needed |
|---|---|
| live **and** archive | A completed aspiration is archived and its goals vanish from the live store, so a live-only join reports a false `owned_by: null`. Archived goals are ~44% of the corpus. The sample could not discriminate this (live-only also resolved 6/6); the discriminating evidence is the unit test that plants a goal *only* in the archive store. |
| title **and** description | An owner names the SYMPTOM in its title and the artifact only in its description. Title-only resolved 3 of 6 stems; title+description resolved 6 of 6. |
| title **outranks** description | The counterweight, and the defect the first implementation had. A CENSUS goal that merely ENUMERATES node names would otherwise claim ownership of every node it counted. Unranked first-match named the census goal as owner of nodes it had only tallied, and picked a wrong goal over one an independent census had identified. A title hit is a claim ABOUT the node; a description hit may be a claim about a LIST that happens to contain it. |
| open **outranks** terminal | "Is someone on this?" and "has anyone looked?" are different reader questions, and the open goal answers the more useful one. |

Also load-bearing: match each stem with **more than one spelling** (hyphenated
and space-separated). A single-spelling match turned a real owner into a
"no owner" probe artifact.

## Why the consumer instruction had to be written down

The producer half shipped first, and a grep for `owned_by` across
`.claude/skills`, `core/config` and `mind_api/src` then returned hits **only
inside `core/scripts/tree.py` itself** — a field nobody read. That is the same
defect g-115-4077 was filed to fix one layer up: `recommended_action` was
emitted and the consuming SKILL.md never branched on it, and 36 distill tests
plus a clean full suite all passed throughout, because every test asserted on
the EMITTED value and none asserted that anything READS it.

"A branched action no caller reads changes no behavior and is invisible to unit
tests by construction" is the `g-115-4077` goal record's own statement of the
class (`world/aspirations.jsonl`, description), and it applies unchanged one
layer down. The OWNERSHIP block in `.claude/skills/tree/SKILL.md` DISTILL step 1
is that caller; the pin is a check in section PU2 of
`core/config/verify-learning-checks.jsonl`, so the producer and consumer cannot
drift apart again.

## Why the row also carries the strength of the hit (g-115-10095)

`owned_by` alone was read as "cite that goal". The join returns an id for a
title hit and for a description-only hit alike, so a reader could not tell a goal
whose title names the node from a sweep that merely lists it, and 4 of the top 10
links a reader followed named non-owners (filed by bravo, 2026-09-16). The join
already computed the difference: `rank = (matched_in_title, owner_is_open)` picks
the winner, and the return then threw the rank away. `owner_match`
(`title` | `description`) and `owner_open` (bool) hand it to the consumer without
a second scan of the aspiration store.

**Why a dict subclass.** The tests that patch the join in, and the fail-open
return, use a plain `fn(stems) -> {stem: goal id | None}`. A new return shape or
an extra keyword would raise `TypeError` inside the call site's `try`, which
swallows it and leaves every `owned_by` null. `_OwnerMap(dict)` keeps the mapping
every caller already consumes and adds `.rank`; the call site reads it with
`getattr(owners, "rank", {})`, so a patched or fail-open join leaves both fields
null, meaning "strength unknown, do the lookup", never a guessed "strong".

**Why only title + open + a size verb licenses a skip.** A description hit may be
a claim about a LIST that contains the node (the census trap above). A terminal
owner means the last look is over and nobody is on the node now. A title can name
a node for a reason unrelated to its size (an Investigate about a retrieval bug,
a contradiction between nodes), so the owner's title must also name the size (fold,
split, carve, over-cap, read cap, distill). The reader checks that last condition;
the two fields make the first two readable on the row.

**Why an owner is a plan, not a worker** (bravo's outcome 4, rb-12846). Even a
real owner is a pointer to a plan. An OPEN, UNCLAIMED, executable carve plan for
an over-cap row is carried out inside the `/tree maintain` pass, and
`owned_by_open_goal_annotated_not_recensused` is no accepted skip reason for it.
Without this clause the licensed skips would recreate the original failure at a
smaller scale: a plan nobody is executing, cited as if somebody were.

**Measured** (alpha, hostname cc-04, `uname -r` 6.8.0-142-generic, 2026-10-10
01:38 UTC, `bash core/scripts/tree-read.sh --distill-candidates`): 1,145 rows, of
which 101 are read-cap rows. 85 are owned: 51 description-only, 30 title with an
open owner, 4 title with a terminal owner. 16 are unowned. Of the 30 title+open
rows, 22 have an owner whose title also names a size verb, so 22 of 101 rows
license a skip and 63 of the 85 owned rows (74%) are a false owner under the new
reading. Echo's recheck of 00:30Z counted 82 owned and 19 unowned, with the same
51 description-only; the 3-row shift is title-named owners filed since.

The pin is `test_distill_owned_by_annotation.py` (unit tests for the join's rank,
the call site, the real join into the real row, and a text pin that the OWNER
STRENGTH paragraph of `.claude/skills/tree/SKILL.md` names exactly the `owner_*`
fields a row carries). It was checked against nine in-memory mutants of the
producer and the consumer; each makes at least one new test fail.

## Cross-references

- `g-115-10095` — the strength fields and the plan-not-worker clause
- `rb-12846`, `guard-6849` — an owner id is a pointer to a plan; the false-owner class
- `sig-229` — the two-census recurrence this annotation exists to end
- `guard-1555` — archived goals disappear from the live store
- `guard-2228` — title-only probes miss owners named only in a description
- `core/scripts/tests/test_distill_owned_by_annotation.py` — the 17 tests pinning
  every constraint above
- `.claude/skills/tree/SKILL.md` DISTILL step 1 — the consumer
- `core/config/verify-learning-checks.jsonl` section PU2 — the producer/consumer
  pins for both `recommended_action` and `owned_by`
