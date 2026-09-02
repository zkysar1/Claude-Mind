# Encoding Protocol — Shared Digest (E16)

Single source of truth for the encoding mechanics shared between the two
encoding lanes:

- `.claude/skills/aspirations-state-update/SKILL.md` Step 8 — autonomous
  deep-outcome encoding (Phase 8 of the perpetual loop).
- `.claude/skills/encode-session/SKILL.md` Lane 1 — chat-mode encoding,
  invoked manually in assistant mode.

Both lanes write to the same stores via the same scripts (`tree-update.sh`,
`reasoning-bank-add.sh`, `guardrails-add.sh`, `pattern-signatures-add.sh`,
`experience-add.sh`, `decision-rules-append.sh`). The gate math, dedup
contracts, and propagation behavior are identical. The differences are
SCALE (Phase 8 emits a full precision manifest; chat-mode collapses to a
single Key Insight paragraph) and TIMING (Phase 8 runs per-goal; chat-mode
runs per-invocation), NOT mechanics.

When changing encoding behavior, edit this digest AND both consuming
skills together. Drift between the two skills is the failure mode this
digest exists to prevent.

## Section A — Encoding-Gate Scoring

Both lanes apply the same gate before writing. The formula SHAPE is:

```
encoding_score = (novelty             * w_novelty)
               + (outcome_impact      * w_outcome_impact)
               + (surprise            * w_surprise)
               + (goal_relevance      * w_goal_relevance)
               + (repetition_strength * w_repetition_strength)
```

All weights AND thresholds live in `core/config/memory-pipeline.yaml`
(`encoding_gate.score_formula`, `encode_threshold`, `skip_threshold`,
`review_range`). Read at call time — do NOT hardcode the numbers here or
in the consuming skills. Single source of truth.

Category-class multiplier (`memory-pipeline.yaml::category_class_multiplier`)
is applied AFTER precision bonus, BEFORE threshold compare. Lookup:
`goal.category → _tree.yaml node.domain_class`. Unknown category → 1.00.

Score components (0-1 each, scaled to threshold):
- novelty:        how new is this concept vs. existing tree coverage
- outcome_impact: did this change material behavior or just refine a label
- surprise:       gap between predicted and actual (use surprise_level/10
                  if a hypothesis is involved)
- goal_relevance: how directly does this serve the active goal/category
- repetition_strength: how many times has this signal recurred recently

## Section B — Curator Quality Gate

After the encoding score passes, the curator gate filters by quality.
Formula SHAPE:

```
curator_score = (coverage      * w_coverage)
              + (specificity   * w_specificity)
              + (actionability * w_actionability)
```

Investigation-aware reweighting applies when goal title starts with
Investigate/Research/Audit/Analyze/Diagnose/Trace/Review, OR
goal.category is in {"analysis", "diagnosis", "research"} — the
investigation weight set replaces the standard set.

All weights AND the pass_threshold live in
`core/config/memory-pipeline.yaml::curator_gate`. Read at call time.

Failure (curator_score < pass_threshold): write the candidate to
`curator_overflow` WM slot — consolidation re-considers it at session
end with full context.

## Section C — Tree Write Steps

```
1. PRECISION extract — scan execution/session content for VERBATIM values.
   Build manifest items: {type, label, value, unit, context}. Types:
   threshold, formula, constant, reference, measurement, config_value.
   Schema + heuristics: core/config/conventions/precision-encoding.md.

2. PRECISION compose — render manifest into Verified Values entries:
       - **{label}**: `{value}` {unit} — {context}

3. NARRATIVE compose — single paragraph of Key Insight from the encoded
   content. Avoid LLM-self-congratulation; describe WHAT the agent now
   knows, not HOW it figured it out.

4. CURATOR gate (Section B) — score the candidate. Below threshold,
   demote to overflow; do NOT proceed to step 5.

5. PRECISION audit — re-read the destination node. Verify each manifest
   item appears in Verified Values (no silent omissions during the
   compose step).

6. TREE write — Edit the node file. Update front matter:
       last_updated:         <today YYYY-MM-DD>
       last_update_trigger:  <"phase-8" | "encode-session" | other>

   The PostToolUse hook (T21 in encoding-triggers.md, via
   `tree-front-matter-sync.py`) atomically mirrors `.md` last_updated into
   BOTH `_tree.yaml::nodes[key].last_updated` AND the top-level
   `_tree.yaml::last_updated` on every Edit/Write of a tree node file.
   This is the single canonical sync — DO NOT add an explicit
   `tree-update.sh --set <key> last_updated <today>` register call after
   the Edit. Historical drift had both consuming skills doing that triple-
   write (Edit → T21 → explicit set); collapsed 2026-05-12 to single
   source of truth.

7. DECISION RULES — if a clear IF-THEN rule emerged from execution:
       echo '{"if": "<observable condition>", "then": "<specific action>"}' \
         | bash core/scripts/decision-rules-append.sh \
             --goal <goal-id-or-session-marker> \
             --node-path <node-md-path>
   Empty stdin is legitimate ("no rule emerged this pass") — bumps the
   staleness marker without writing.

8. CAPABILITY recalc — if the node's capability_level may have changed
   (new threshold crossed), run propagation:
       bash core/scripts/tree-propagate.sh <node.key>
   Emit a CAPABILITY UNLOCK log line when the level rises.
```

## Section C2 — Tree Freshness Sweep (read-side)

The write steps above encode NEW content. This sweep keeps EXISTING nodes
truthful — it operationalizes `knowledge-freshness.md` + guard-1710 (sweep
every artifact still asserting a corrected conclusion) + guard-1538
(gap/absence claims need re-verification) inside the encoding lanes, where
the session's evidence is still in context. Added 2026-08-21 after a
measured miss: a node's "Open (unfiled)" claim sat stale ~3 weeks through
every agent's encode passes because no lane ever re-read standing claims —
the user had to prompt the tree review that found it in one probe.

Inputs — bounded to nodes whose subject the session actually touched:
(a) every node retrieved this pass (Lane 1.0 snapshot / Phase 8
retrieval-session), and (b) the catalog node of any subsystem where the
session ran a MAJOR OPERATION (promotion, seed plant, migration, incident,
measurement campaign) — probe `tree-find-node.sh --text "<subsystem>"
--top 3` for those.

For each such node, four checks:

```
1. CLAIM RECONCILIATION — scan for standing claims: "Open", "unfiled",
   "pending", "not yet", "TODO", "CONTESTED", "no X exists", "nothing
   does Y", dated as-of assertions. Did this session's work COMPLETE or
   OVERTAKE one? → rewrite the claim IN PLACE ("RESOLVED (g-NNN): ...",
   keeping enough of the original that the history reads). Never retire
   on age alone — evidence only.
2. CONTRADICTION CHECK — does session EVIDENCE (a measurement, probe
   output, live-fire outcome) contradict what the node asserts? →
   correct the wrong sentence WHERE IT STANDS, citing the evidence
   inline (date, box, command). A contradiction outranks an addendum:
   never append a correction below text that still asserts the old
   conclusion (guard-1710).
3. LIVE-FIRE ADDENDUM — if the operation's catalog node keeps dated
   addenda (## YYYY-MM-DD sections), append this session's event in the
   node's OWN format: what fired, what held, what was newly filed,
   traces (rb/guard/goal ids).
4. BOLSTER AGAINST REPEAT — if the session hit a mistake or wrong path
   that an EXISTING node could have prevented but its text lacked the
   warning: add the missing gotcha / decision rule to that node, so the
   next reader fails differently. If the node HAD the warning and it
   was not retrieved, that is a RETRIEVAL gap — route to the blind-spot
   lens (encode-session Lane 4.2), not a node edit.
```

Every edit follows write step 6 (front matter + T21) and Section D
coordination. Report counts per pass: claims reconciled / contradictions
corrected / addenda appended / nodes bolstered.

## Section D — Cross-Agent Coordination (T23)

Before writing to a tree node, check for in-flight encoding by another
agent:

```
Bash: board-read.sh --channel coordination --type encoding --since 30m --json
IF any message has tag matching node.key AND author != current agent:
    DEFER — queue to WM encoding_queue, skip the immediate write
ELSE:
    Bash: echo "Encoding: <node.key>" | board-post.sh --channel coordination \
        --type encoding --tags <node.key>
    PROCEED with steps 6-9 above
```

Consumer side: `/prime` Phase 2 step 5.5a (E11) surfaces pending encodings
to the OTHER agent so they don't queue a competing edit.

## Section E — Knowledge-Debt Consumption

Both lanes are consumers for `knowledge_debt` entries filed by:
- `/respond` Step 4.5 (E1) — tier-escalation debt
- `/respond` Step 6.5 (E2) — post-edit reconciliation debt
- `/respond` Step 7.5e (E3) — tool-result-surprise dual-write
- `/respond` Step 7.5f (E6) — review-finding debt
- aspirations-execute Phase 4.5 (E7) — probe-outcome surprise dual-write

Algorithm (SSOT — consumers carry the invocation and this pointer, never a
re-statement. Reconciled 2026-08-21: this section said `>= 5` while the
operational lane said `>= 2` and the null-key lane below existed only in
the skill — the exact drift this digest exists to prevent):

```
0. NORMALIZE ON READ (g-115-4021). An element may be a BARE STRING, not an
   object. Every step below indexes sub-fields (debt.created, debt.node_key,
   debt.reason, debt.priority, debt.sessions_deferred), and all of those miss
   on a string — so such an entry can never auto-resolve, never age, never
   reach the ceiling that retires it. It lives forever while BOTH sweeps
   report success: the rb-5650 / guard-1802 looks-like-coverage-delivers-none
   shape. Measured across three boxes: 1 string (cc-01 2026-07-30), 2 (cc-02
   07-31), 4 of 14 (cc-05 08-08) — it grows, it is not a one-off slip.
   Coerce before anything else:
       IF isinstance(entry, str):
           entry = {node_key: null, reason: entry, priority: "medium",
                    created: null, sessions_deferred: 0}
   It then flows the normal path: step 2's null-key lane can still mine a
   goal id out of `reason`, and failing that it ages to the step-4 ceiling.
   created:null must NOT auto-resolve at step 1 — a missing date is not a
   node-update date, and treating it as one would resolve debt nobody paid.
   Coercing on READ (rather than rewriting the stored element) is deliberate:
   the sweep's RMW is lock-free between read and write-back, so the fewer
   writes racing a concurrent wm-append, the better.
1. AUTO-RESOLVE on node update: node.last_updated >= debt.created —
   date-only [:10] compare on BOTH sides (node stores date-only; >= not >
   so a same-day node edit counts) → resolution_method:
   auto_resolved_by_node_update.
2. NULL-KEY LANE (the MAJORITY shape, not an anomaly — /respond files
   debt precisely when no single node covers the correction, so
   node_key=null is DESIGNED; g-115-5150): extract the first g-\d+-\d+
   from reason / routed_goal / source_goal; derive asp-<NNN> from it
   (aspirations-read.sh --id takes an ASPIRATION id — a goal id returns
   not_found, which reads like "the goal is gone"); routed goal
   status=completed → auto_resolved_by_routed_goal. No goal id, or goal
   not completed → fall through. NEVER resolve null-key debt on age —
   "it got old" is not evidence the gap was filled.
3. INLINE RESOLUTION when priority==HIGH OR sessions_deferred >= 2: read
   the target, resolve from in-context knowledge or a quick probe
   (front matter last_update_trigger: {type: "debt-reconciliation"});
   else sessions_deferred += 1 and carry.
4. MAX-DEFER CEILING (10): DURABLE DROP FIRST — write the full reason to
   execution-diary BEFORE removing the entry (a dropped entry's only
   other trace is a log line that dies with the session; null-key
   renders as "DROPPED debt for null", naming nothing recoverable). A
   HIGH debt at ceiling ALSO files a MEDIUM Investigate — 10 failed
   sweeps is a finding about the RESOLVER. Then max_defer_dropped.
5. WRITE-BACK: filter resolved entries yourself and wm-set the slot,
   CARRYING THE CAS TOKEN (g-115-8667) so a concurrent append is REFUSED
   rather than silently clobbered:
     Bash: bash core/scripts/wm-ages.sh --json    # -> .knowledge_debt.update_count
     Bash: <filtered json> | bash core/scripts/wm-set.sh knowledge_debt \
             --expect-update-count <that value>
   Read the token in the SAME turn as the wm-read whose value you filtered — a
   token read later than the data it guards attests to nothing.
   rc 9 == 409 stale_write: a peer wrote between your read and your write and
   NOTHING LANDED. Re-read knowledge_debt, re-apply your resolutions to the
   FRESH list, re-send ONCE with the new token. If the retry also returns 9,
   write without the token and say so in the report — the sweep is idempotent
   (it filters already-resolved entries), so a second collision is not worth a
   third round trip, and a debt sweep that refuses to write is worse than one
   that occasionally re-does work.
   The retry is the load-bearing half, not the flag: CAS DETECTS the collision,
   it does not prevent it. A caller that passes the token and then treats rc 9
   as failure has converted a silent loss into a loud one and fixed nothing.
   knowledge_debt is NOT in item_stale_minutes, so wm-prune's
   array-item gate is unreachable for this slot — resolved entries
   otherwise live forever.
6. DEDUP BY node_key ON WRITE-BACK (g-115-4021, measured cc-05 2026-08-08).
   Exact-record dedup does NOT bound this slot and reads as clean while
   failing: 10 of 14 entries there were FIVE node_keys each appearing TWICE,
   identical except for embedded counters that only ever RISE
   (retrieval_count 237/242, 230/236, 222/226, 211/216, 207/209;
   total_stale_at_scan 838/837). A re-scanned node therefore never matches
   its own earlier row and always appends a fresh one. An exact-string
   comparison over the slot finds 0 duplicates — only a node_key-keyed one
   surfaces them, which is why this half stayed invisible while the string
   half was being investigated.
   So when writing back, collapse non-null node_key duplicates to ONE entry:
   keep the OLDEST (it carries the real `created` and the accumulated
   sessions_deferred, so ageing is preserved) and take the HIGHEST counter
   values seen. Never key on the whole record. Entries with node_key null
   are NOT deduped — null is the designed majority shape (step 2) and
   collapsing them would merge unrelated debts.
```

RMW caveat (CLOSED 2026-09-02, g-115-8667): the sweep is wm-read → mutate →
wm-set across separate calls, so the per-request WM lock cannot span it — a
lock covers ONE request and this is three. A concurrent `wm-append
knowledge_debt` between them USED to be lost silently, with rc=0 at every step.
Step 5's `--expect-update-count` closes that: the loser is REFUSED (rc 9) and
re-applies onto a fresh read instead of clobbering. This is no longer an
accepted limitation, and consolidate Step 2.25 inherits the fix by routing
through this step rather than carrying its own copy.

## Section F — Chunked Encoding (E13)

Long executions (>4000 chars output OR >30 min wall-clock) may contain
multiple distinct learnings. The chunk schema lets both lanes emit N
encoding payloads per single execution instead of bundling everything
into one Key Insight paragraph.

Chunk schema (appended to WM sensory_buffer):

```json
{
  "source_goal": "<goal_id-or-session-marker>",
  "chunk_idx": <0-based>,
  "chunk_total": <N>,
  "chunk_text": "<the segment's text, ≤2000 chars>",
  "content_type": "<finding | decision | code-change | observation>",
  "scores": {
    "novelty": <0-1>,
    "outcome_impact": <0-1>,
    "surprise": <0-1>,
    "goal_relevance": <0-1>,
    "repetition_strength": <0-1>
  },
  "target_article": "<node-key or null>",
  "replay_priority": "<replay-priority>"
}
```

Producer (aspirations-execute Phase 4.05 long-execution branch — or
encode-session Lane 1 when chat span exceeds threshold):

```
IF result_size_chars > 4000 OR phase_4_duration_sec > 1800:
    Segment result by natural boundaries (### headings, distinct tool-output
    blocks, paragraph breaks). Cap at 5 chunks.
    For each chunk:
        Score it independently per Section A's component definitions.
        Append to WM sensory_buffer using the chunk schema above.
```

Consumer (Phase 8 / encode-session Lane 1, on reading sensory_buffer):

```
For each chunk tagged with the current goal/session:
    Run Section A gate against chunk.scores
    IF passes: build a per-chunk precision payload, run Section B + C
               steps independently
    IF fails: drop (overflow queue handles re-consideration)

# Net effect: high-scoring chunks land as distinct tree updates; low-
# scoring chunks drop. The summarized bundle is replaced by N independent
# encoding decisions, each evaluated on its own merit.
```

## Section G — Mode + Trigger Coverage

| Lane | Trigger | Mode | Frequency |
|---|---|---|---|
| Phase 8 (T1) | Every deep-outcome goal completion | Au | Per-goal |
| Phase 8r (T2) | `achievedCount % 5 == 0` on recurring goal | Au | Per-5-routines |
| Consolidation (T4-T8) | Session end | Au | Per-session |
| /encode-session (T10) | User invocation | A, Au | On demand |
| /encode-session Lane 1.6 (E17 / T17) | Inside /encode-session | A, Au | On demand |

The two skills' DIFFERENT cadence is intentional: autonomous mode encodes
continuously inside the loop, chat-mode encodes on user invocation. Both
follow this digest's Sections A-F identically.

## Cross-references

- `core/config/memory-pipeline.yaml` — gate config (single source of truth
  for numeric thresholds; this digest cross-references but does not
  duplicate the numbers)
- `core/config/conventions/encoding-triggers.md` — trigger catalog
  (Txx active, Exx gaps)
- `core/config/conventions/precision-encoding.md` — precision manifest
  schema + extraction heuristics
- `core/config/conventions/learning-routing.md` — which store does this
  learning go to (T vs R vs G vs P vs E)
- `core/config/conventions/decision-rules.md` — Decision Rules format,
  dedup, and the staleness marker
