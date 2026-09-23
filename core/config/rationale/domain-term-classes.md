# Domain-term classes at the core/domain border

Governs the classification half of the border wall: which kinds of term are a
leak in a core file, and where each kind's membership is enumerated.

- Scanner: `core/scripts/domain-leak-check.sh`
- Term list: `core/config/domain-term-blocklist.txt`
- Marker doctrine: `.claude/rules/domain-free-examples.md` and
  `core/scripts/marker-placement-gate.py`
- Recorded by: g-115-10049 outcome 3. The governing owner decision is carried
  verbatim in guard-6792 (user directive, 2026-09-15).

## Why this file names no terms

**Read this before adding an example.** The scanner's scan scope includes
`core/config/**`, so this file is scanned by the very check it governs. A
governing document that enumerated its subject terms would violate the rule it
states, and could only exist by carrying an exemption marker (the token `domain-leak-exempt` followed by a colon).

That marker is reserved for two carriers — executable code where domain strings
are FUNCTIONAL (regex patterns, sentinels, fixtures), and rule files that teach
by example (`domain-leak-check.sh` Phase 5.6). A rationale document is neither,
and `core/config/rationale/` carried zero markers when this file was written
(measured 2026-09-18: 0 of 70 files; 125 marker-carrying files fleet-wide, 93 of
them under `core/scripts`). Planting the first marker here would open a new
carrier class while a sibling goal (g-115-10050) is ratcheting that surface down.

So each class below is defined by **where its membership is enumerated**, never
by listing members. This is the single-source-of-truth rule
(`communication-clarity.md` rule 5) applied to the blocklist itself, and it is
also the input contract for the census in this goal's outcome 2: derive candidate
terms from the registries named here, never from a second hand-typed list.

The general shape is guard-1668 (a check whose scan surface includes the file
defining it) and guard-3518 (documentation about a scanner will be scanned by
it — describe the literals, never reproduce them). Same defect, different
scanner; the same remedy works.

## The three classes

### Class A — product and world-facing names

**Membership registry**: `world/forged-skills.yaml` skill and trigger names;
proper nouns in `world/program.md`; headings in `world/conventions/*.md`.

**Ruling**: a leak in every core file. These belong on the blocklist, in every
case form the registry produces.

### Class B — deployment identities

**Membership registry**: `core/config/environments/*.yaml` — each file's
environment id, plus the file basenames themselves.

**Ruling**: a leak, with **no identity exemption**. The owner decision admits no
carve-out for "it is just the name of a deployment". The registry directory is
the single sanctioned home; being the registry does not license repeating an
identity in a rule, a script, a skill, a convention, or the root project guide.

The registry is itself domain DATA, and that is not an exemption granted here.
Since g-373-124 (5a32cc76d7) it no longer rides the promotion seed: the seed
ships only the generic entry, every destination keeps the entries it already
holds, and `domain-leak-check.sh` counts hits only in files the seed ships. All
three decide from one predicate, `_peer_registry.is_deployment_registry_entry`.
One hop still carries the registry: the customer tarball, which the staging
deployment builds with `git archive` (g-374-88). The six readings ledgers stay
with g-115-10047.

### Class C — infrastructure and operator identities

**Membership**: box hostnames, private host and network names, and personal
handles. There is deliberately **no registry**, because these are not vocabulary —
they are **measurement provenance**.

**Ruling**: legitimate inside a dated measurement, an experience record, a goal
note, or a ledger entry, where naming the box that produced a reading is what
makes the reading checkable. A leak in a rule, a script, a convention, or config
prose, where the same string is an unreproducible local fact asserted as general
truth.

Class C is therefore the one class whose verdict depends on the **surface**, not
on the term. A scanner cannot settle it from the term alone; the blocklist entry
must be paired with the ledger relocation that gives provenance a legitimate home.

## What must be tested, not merely stated

1. **Both artifacts, not one.** The dev tree and the SEED OUTPUT are different
   corpora: the seed applies rename transforms, so a term absent from the dev
   tree can appear in the export and a term present in the dev tree can be
   rewritten out of it. A clean dev scan is not evidence about what ships.
   The promotion-time sweep (guard-6936) is where the seed half is owed.
2. **Report scope before blocking scope** (guard-1426). A new term or case rule
   enters reporting scope first; the backlog is filed with per-file counts; only
   then may it block, and only on ADDED lines.
3. **Classification, then growth.** Adding a term whose class is undecided moves
   the failure from the scanner to the reviewer, which is where it is most
   expensive.

## This file's own placement is measured, not argued

The placement claim above — a registry-referential ruling needs no exemption
marker — was verified by running the real scanner against this file, not by
reasoning about it. Measured 2026-09-18 (g-115-10049), two arms, each with a
planted probe proving the scanner can go red on THIS path:

| arm | file contents | default scan | `--ignore-case` |
|---|---|---|---|
| 0 | this file as written | 0 hits, rc=0 | 0 hits on this file |
| 1 | + one probe line holding a capitalised blocklist term | **1 hit, rc=1** | — |
| 2 | + one probe line holding only the lowercase variant | 0 hits, rc=0 | **1 hit** |

Arm 1 going red is what makes arm 0 mean anything. Without it a clean scan is
indistinguishable from a scan that never looked.

### The trap this file fell into first, and every future editor will too

The FIRST run of the control above reported arm 1 CLEAN — the scanner stayed
rc=0 with a blocklisted term planted in this very file, while a hand-run grep
using the scanner's own arguments found it at this file's own path.

Cause: an earlier draft of the paragraph above wrote the exemption marker as a
literal token followed by a colon. The scanner's marker-honor filter tests for
that exact substring ANYWHERE in the file with no anchoring, so **this document
exempted itself from the entire wall merely by naming the marker correctly**.
Nothing reported it: the misplacement scan and the placement gate are both
scoped to two other directories.

So: **in any file under the scan scope, never write the exemption token followed
by a colon unless you intend to exempt that whole file.** Write the token and
describe the colon, as the paragraph above now does. The defect itself is filed
separately; this note stays because the hazard is a property of writing ABOUT
the wall, which is exactly what this file does.

A future editor who adds a concrete domain term here will break the clean
property in the other direction. If an example is genuinely required, put it in
the domain's own convention tree (`world/conventions/`), which is outside the
scan scope and is the sanctioned home for domain-specific operational detail,
and link to it from here.
