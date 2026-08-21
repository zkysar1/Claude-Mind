# Exhaustive Knowledge Search Before Negative Conclusions

**Reinforces**: `.claude/rules/verify-before-assuming.md` (multi-signal requirement)

## Principle

Before concluding that something "isn't built," "doesn't exist," "isn't possible,"
or "can't be done" — the agent MUST exhaustively search the knowledge tree. A single
category search or a single query is insufficient. Absence of evidence in one location
is not evidence of absence.

## Protocol

When forming a negative conclusion about capabilities, features, or possibilities:

1. **Multi-query search**: Use `tree-find-node.sh --text` with at least 3 different
   query variations. Rephrase the concept using synonyms, related terms, and
   alternative framings.
   ```bash
   bash core/scripts/tree-find-node.sh --text "llama server CUDA" --top 3
   bash core/scripts/tree-find-node.sh --text "GPU inference binary" --top 3
   bash core/scripts/tree-find-node.sh --text "external model server" --top 3
   ```

2. **Cross-category check**: Don't search only the obvious category. If looking for
   whether something exists in "performance," also check "infrastructure,"
   "architecture," and any other category that could plausibly contain the information.

3. **Read matching nodes**: For each matching node returned, READ the full `.md` file.
   Summaries in `_tree.yaml` are compressed — the detail is in the articles.

4. **Check adjacent systems**: reasoning bank, guardrails, pattern signatures,
   experience archive. ONE free-text call reaches all four — prefer it over four
   separate reads:
   ```bash
   bash core/scripts/retrieve.sh --category "<free text>" --depth shallow --read-only
   ```
   Read the `reasoning_bank`, `guardrails`, `pattern_signatures`, and
   `experiences` keys of the response. Note `experiences` is PLURAL; a literal
   `experience` read returns None and looks like an empty store.

   **There is no `--search` flag on any of these wrappers.** This step prescribed
   one until 2026-08-09. Measured (alpha, `hostname` cc-04, `uname -r`
   6.8.0-136-generic): `--search` occurs ZERO times in all four, and each is on
   the g-115-5438 list of 22 wrappers that append unknown flags to a write-only
   `PASSTHROUGH` array — so it is swallowed there, then caught by each wrapper's
   filter-required check. All four prescribed calls therefore failed rc=1 with
   `Error: at least one filter is required`. Loud, which is the safe direction,
   but unrunnable as written, so this step's four lines had a 0% execution rate.

   `pattern-read.sh` was the dangerous one: that script has never existed (it is
   `pattern-signatures-read.sh`), so it died rc=127 — and under the `2>/dev/null`
   common to probe chains, rc=127 becomes an EMPTY result with no error. That is
   the silent false negative this whole convention exists to prevent
   (`verify-before-assuming.md` rule 4: a silently-failed command is ZERO signals,
   not one). A step-4 line that cannot run is not a weaker check; it is an absent
   one that reads as present.

   Per-store filters, when you want one store rather than the blended read:
   `reasoning-bank-read.sh --category|--tag|--recent|--active|--universal` ·
   `guardrails-read.sh --category|--active|--summary` ·
   `pattern-signatures-read.sh --active|--all|--summary` ·
   `experience-read.sh --category|--goal|--type|--recent`.

5. **Retrieval escalation**: Follow `core/config/conventions/retrieval-escalation.md`
   — Tier 1 (tree) → Tier 2 (codebase) → **Tier 2.5 (peer worlds)** → Tier 3 (web
   search) before concluding something doesn't exist.

   **Tier 2.5 is `core/scripts/peer-retrieve.sh`, and it answers with TWO fields
   that must BOTH be read**: `status` (`hit` / `empty` / `unreachable` — "did I
   find anything?") and `completeness` (`complete` / `partial` — "did I see
   everything?"). **Only `status: empty` at `completeness: complete` (rc=0)
   licenses a negative conclusion.** rc=3 means the reader ran fine but at least
   one world could not be fully read, so the lane came back `partial` — and a
   `partial` or `unreachable` lane found nothing for exactly the reason a
   `2>/dev/null` command finds nothing (rule 4 of `verify-before-assuming.md`:
   it never looked). Peer deployments hold their own trees, reasoning banks and
   guardrails, so "not in MY world" is not "not built", and a sibling world is
   the independent second signal that rule's multi-signal requirement asks for.

6. **Search the GOAL QUEUE — not just the code** (added 2026-07-14, zeta). A large
   class of invocations lives in a **recurring goal's `description`**, never in
   `.claude/skills/`, `core/config/`, or `core/scripts/*.sh`. Grepping only the code
   surfaces and concluding "nothing invokes this / this detector has no consumer" is a
   **structurally guaranteed false negative** for every goal-queue-driven sweep.

   Before any "nothing runs X / X has no consumer / X was built and abandoned" claim:
   ```bash
   # search DESCRIPTIONS, not just titles — the invocation lives in the body
   py -3 -c "import json,sys; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; import os; \
   [print(g['id'],'|',g.get('title','')[:60]) for r in (json.loads(l) for l in open(os.path.join(str(WORLD_DIR),'aspirations.jsonl'),encoding='utf-8') if l.strip()) \
    for g in (r.get('goals') or []) if '<script-or-tool-name>' in json.dumps(g)]"
   ```
   **`aspirations-query.sh --title-contains` is NOT sufficient** — it matches TITLES only, so it
   returns empty for a script named solely in a description. An empty result from it is a
   silent-failure negative (`verify-before-assuming` rule 4: a silently-empty command is
   ZERO signals, not one), *not* a confirming second signal. Two weak signals drawn from
   the same blind spot are not two signals.
   (The flag was written `--contains` here until 2026-08-09; that spelling does not exist.
   It used to be swallowed silently, so a reader who copied it got an UNFILTERED result —
   a strictly worse negative than the empty one this paragraph warns about. It now exits 2,
   g-115-5214.)

   **The queue is also a DECISION store, not only an invocation store** (added
   2026-08-06, foxtrot, g-350-121). A user directive is routinely captured as a goal
   DESCRIPTION and nowhere else, because filing the work is the natural way to record
   "the user told us to do X". So the same grep answers a second, differently-shaped
   negation — "no prior decision was ever made about this" — and the trigger list below
   did not name it until now, which is why rule 6 can be followed correctly on
   invocations and still miss a decision.

   Measured: a goal asked whether a production gap was deliberate or drift and
   explicitly said to check for a prior decision record first. `world/conventions/`,
   the decisions board at 2000h, and the knowledge tree were all searched and all
   empty, and "no decision record exists either way" was written. It existed — a
   sibling goal filed the previous day carried `USER DIRECTIVE (user, 2026-08-05)`
   plus the exact scope decision in its description. Three empty stores felt like
   exhaustive search; they were exhaustive over the stores that *feel* like decision
   stores, and the queue does not, because it reads as a work list.

   Note the failure shape repeated the incident below exactly, ~3 weeks later, on the
   same store and via the same catch: the goal-duplication gate refusing a follow-up
   filing, not the search. A write-time gate catching a read-time search failure means
   an investigation that never tried to WRITE anything would have shipped the wrong
   answer with nothing to stop it. (guard-2844.)

   Canonical incident (2026-07-14, zeta fresh-eyes): grepped skills/config/scripts for
   `fixture-leak-scan.py`, found nothing, and wrote "a working detector with ZERO
   consumers" into a briefing. It is in fact wired to **g-115-1651** (recurring, 24h) via
   that goal's Step 2, and had run **that same morning**. The empty
   `--contains "fixture-leak"` query was read as corroboration; it only ever searched
   titles. Caught by the goal-duplication gate, not by the agent.

## Trigger Phrases

Apply this protocol whenever the agent is about to output or act on:
- "This isn't built yet"
- "There's no way to..."
- "This can't be done because..."
- "No existing implementation for..."
- "We'd need to build..."
- "Not possible with current..."
- "Doesn't support..."
- "Nothing invokes this" / "this has no consumer" / "this was built and never wired"
  (→ rule 6 above: search the goal queue's DESCRIPTIONS before asserting)
- "No prior decision exists" / "this was never decided" / "no decision record either
  way" / "no directive covers this" / "nobody ever settled the scope"
  (→ rule 6 above, DECISION-store half: a user directive most often lives in a goal
  description and nowhere else. Every phrase above is a negation about a DECISION
  rather than a CAPABILITY, which is why the capability-shaped phrases in this list
  do not fire on it.)

## Scope the search to the REPOS you actually searched, and name them

A product that spans repositories makes a single-repo sweep *feel* exhaustive
while being blind by construction — the sweep really was exhaustive, over one
repo, and nothing in its output says so. Before stating any "no live path does
X" conclusion:

1. **Enumerate the repos in scope and name them in the claim.** Write "no path
   in <repo>" — never "no path", which silently promotes a repo-scoped finding
   into a product-wide one.
2. **For every REMOVED call site the conclusion rests on, ask MOVED or DIED.**
   A deletion has two causes that look identical from inside one repo. The tell
   for a relocation is usually sitting in the same repo: a test pinning the
   absence ("this action must STAY GONE", "no longer imported here") or a
   tombstone comment naming the successor. Nobody writes a regression test to
   protect an accident, so an absence-pinning test is evidence of a deliberate
   move, not of a death.
3. **Query PRODUCTION for the artifact the claim says is never created**, before
   reading any source. It is the cheapest signal available and it is decisive.

Canonical incident (g-364-27 → g-364-28, 2026-08-20): an "exhaustive origin/main
sweep" concluded no live path wrote a required registration row and spawned two
forward-fix goals, one of which another agent executed. The writer had been
extracted into the sibling app months earlier and had 22 live production rows,
one serving a running customer world. The removed call site the sweep relied on
was pinned by a same-repo test naming exactly that relocation. See rb-8634,
guard-4588, rb-8518 (corrected).

## Anti-Patterns

- Searching one node and concluding a feature doesn't exist
- Checking `_tree.yaml` summaries without reading the actual `.md` files
- Assuming something isn't built because the agent doesn't remember building it
- Concluding "not possible" from a single tool/library limitation without checking
  if alternative approaches exist in the knowledge base
- Declaring "we'd need to build X" without searching for existing implementations
- Grepping ONE repo of a multi-repo product and stating the negation unqualified
- Reading a deleted call site as "the capability died" without checking whether it
  MOVED — especially when a test in the same repo pins the absence (that test is
  the fingerprint of a relocation)
- Concluding a row/record is never written without once querying the production
  store for it

## Relationship to Verify Before Assuming

This convention is the knowledge-tree-specific implementation of the "Verify Before
Assuming" rule. While that rule requires 2+ independent verification signals for any
negative conclusion, THIS convention specifies exactly how to verify within the
knowledge system:

- 3+ query variations (not just one search)
- Cross-category (not just the obvious node)
- Full article reads (not just summaries)
- Adjacent data stores (reasoning bank, guardrails, etc.)
- Retrieval escalation (tree → codebase → web)

## Motivation

Session 18 (2026-04-01): During LLM benchmarking, the agent assumed that GPU
inference for Qwen3.5 was impossible based on a single signal (pip wheel limitation).
A more thorough search would have revealed that pre-built llama-server.exe binaries
with CUDA support existed — solving the problem without any code changes. The cost
of the missed search was multiple hours of wasted effort and a premature negative
conclusion that blocked progress.
