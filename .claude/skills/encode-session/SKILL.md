---
name: encode-session
description: "Runs a structured learning pass on the current chat session: encode reusable insights to the knowledge tree / reasoning bank / guardrails / patterns / experience archive; file Maintain goals for inline completions; re-probe blockers our work may have falsified; surface new aspirations / goals / forge gaps; propose /verify-learning checks for framework changes; check meta-strategy and Self for evolution signals. Invoked explicitly via /encode-session or 'encode this chat session' — does NOT auto-fire on conversational close-out language. Hybrid: user-invocable AND agent-callable."
user-invocable: true
triggers:
  - "/encode-session"
  - "encode this chat session"
tools_used: [Bash, Read, Edit, Write, Grep, Glob]
conventions: [aspirations, tree-retrieval, reasoning-guardrails, spark-questions, experience, goal-schemas, learning-routing]
minimum_mode: assistant
revision_id: "skill-bootstrap-encode-session-8bee4d"
previous_revision_id: null
---

# /encode-session — Session Learning Consolidation

The chat-context analogue of the autonomous loop's Phase 6.5 (`/aspirations-spark`)
and Phase 8 (`/aspirations-state-update`): runs the same structured learning
lanes, but on the just-completed chat session rather than a single goal execution.

**Why this exists**: in assistant mode the autonomous loop is not running, so the
post-goal sparks and state-update lanes never fire. Without a deliberate trigger,
chat-mode learning is lost — files get edited, the user moves on, and no encoding
happens. /encode-session is that deliberate trigger.

This skill is intentionally a **thin orchestrator**: it reuses the same scripts
and same protocol blocks as `/aspirations-spark` Phase 6.5, just adapted for
conversation input rather than goal-record input. The two skills must evolve
together — when one's encoding logic changes, the other should mirror it.

## JSON Construction Policy (MANDATORY)

Every encoding sub-lane below invokes an `*-add.sh` script that accepts JSON
on stdin. Build payloads **inline** — do NOT materialize them to disk first:

```
printf '%s' '{"title":"...","type":"success",...}' \
  | bash core/scripts/reasoning-bank-add.sh
```

This applies uniformly to `reasoning-bank-add.sh`, `guardrails-add.sh`,
`aspirations-add-goal.sh`, `experience-add.sh`, and every other encoding
entry point.

**If a payload is too complex to inline** (rare — encoding records are
almost always < 2 KB), the only sanctioned scratch home is
`agents/<name>/sessions/<SID>/scratch/` (Phase 2.6 — see
`.claude/rules/path-resolution.md` "L1 Cruft Prevention"). Two prohibitions:

- **NEVER `PROJECT_ROOT/.scratch-encode-session/`** or any other
  invented top-level dir at the repo root. The L1 hook only governs
  Write/Edit, so a `mkdir` + heredoc via Bash bypasses it silently —
  the prohibition lives at the SKILL.md layer to close that gap.
- **Always delete scratch at end-of-skill** in the same turn that
  applies the encodings. Orphan staging files are cruft even when the
  encodings landed successfully.

The 2026-05-20 incident that motivated this policy: alpha session
materialized 8 JSON payloads under `PROJECT_ROOT/.scratch-encode-session/`,
applied all 8 successfully, then exited without cleanup. All payloads
were correct; the only failure was the scratch-location choice.

## Sub-commands

```
/encode-session              — Full pass (Lanes 1-7)
/encode-session --quick      — Encoding lanes only (Lanes 1, 2, 3)
                               Skips Discovery, Verify-Learning, Meta, Self lanes
```

## Phase 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 1: Establish Session Context

The skill operates on the current conversation. The LLM uses its in-context
conversation memory PLUS fresh state probes to identify what changed during this
session — never reconstructed from prior-session memory (per
`.claude/rules/verify-before-assuming.md` Positive File-State Claims).

```
1. Bash: git status --short
   Bash: git log --oneline -10
   Bash: git diff --stat
   # Single source of truth for chat-session scope: uncommitted working-tree
   # changes. Do NOT add a HEAD@{1}..HEAD probe here — chat sessions usually
   # have not committed yet, so the reflog scope would be empty and the
   # working-tree scope is the only one that captures session work.
2. Read agents/<agent>/session/working-memory.yaml
3. Bash: aspirations-read.sh --summary
4. Identify scope from in-context conversation memory:
   - Which files were edited / created / deleted this session?
   - What domain topics were discussed?
   - What problems were diagnosed and resolved?
   - What inline fixes happened that bypassed the goal pipeline?

5. Triage:
   IF the chat session was purely Q&A (no writes, no diagnostics, no state
   changes):
     Print: "encode-session: pure Q&A session — nothing substantive to encode."
     SKIP to Phase Final and emit empty summary.
     (Do NOT generate spurious encodings just to fill a checklist.)
```

## Lane 1: Encoding (Knowledge Tree, Reasoning Bank, Guardrails, Patterns, Experience)

Same protocol as `/aspirations-spark` Phase 6.5 (Operational Gotcha
Auto-Detection + reasoning bank + guardrails). Reuses the same scripts.
Routing decisions follow `core/config/conventions/learning-routing.md`.

The shared mechanics (encoding-gate scoring, curator gate, tree-write
steps, cross-agent coordination, knowledge-debt consumption, chunked
encoding) live in `core/config/encoding-protocol-digest.md` (E16). This
skill is the chat-mode consumer of that digest; `aspirations-state-update`
Step 8 is the autonomous-mode consumer. **Edit the digest BEFORE editing
Lane 1 — drift between the two skills is what the digest exists to prevent.**

### 1.0 Pre-Encoding Retrieval (G13 / R16)

Before any sub-lane fires, pull a unified retrieval snapshot per encoding
topic. `tree-find-node` (substring-only) and `reasoning-bank-read --category`
(unfiltered category dump) are weaker than `retrieve.sh` — they miss entries
filed under sibling categories AND can't TF-IDF-rank within a category.
Per `.claude/rules/retrieve-before-deciding.md` decision point 5 ("adding a
new aspiration") — encoding writes are the same shape: dedup decisions on
new artifacts.

```
For each distinct topic the session produced encodable content for:
  Bash: retrieve.sh --category "{topic}" --depth medium --read-only --quiet

  Stash the returned JSON for this topic as encoding_snapshot[{topic}] —
  the sub-lanes below consult this snapshot before falling back to
  narrower tools:
    - 1.1 (tree): consult encoding_snapshot.tree_nodes for the matching
      leaf BEFORE running tree-find-node.sh (which is substring-only and
      misses semantic matches)
    - 1.2 (RB): consult encoding_snapshot.reasoning_bank for semantic
      overlap BEFORE running reasoning-bank-read.sh --category. Engine
      details: retrieve.sh filters by `_entry_matches` (strict category +
      token-overlap fallback) and sorts by utility score, capping at
      SUPPLEMENTARY_CAPS[medium]=40. The category-read returns ALL
      entries in the exact category, unranked — useful as fallback when
      the topic does NOT match a category cleanly, but worse for dedup
      precision when it does.
    - 1.3 (guardrails): consult encoding_snapshot.guardrails BEFORE
      running guardrails-read.sh --category. Same filter+sort+cap as RB.
    - 1.4 (patterns): consult encoding_snapshot.pattern_signatures
    - 1.5 (experience): consult encoding_snapshot.experiences

  --read-only flag prevents this snapshot from bumping retrieval counters
  during a chat-mode session (which would distort utility_ratio for the
  topic; encoding-time consultation is not a "real" retrieval signal).

  Fail-open: if retrieve.sh errors, log and fall back to the narrower
  per-sub-lane probes below. Encoding must not block on a snapshot error.
```

### 1.1 Knowledge Tree

```
For each domain topic discussed with substantive new content:
  IF encoding_snapshot[{topic}].tree_nodes has a leaf match (Lane 1.0):
    Use that node; skip tree-find-node.sh
  ELSE:
    Bash: tree-find-node.sh --text "<topic>" --leaf-only --top 1
  IF a node exists AND content is genuinely new (not already covered):
    Read the node file
    Edit the node — append to "Key Insights" or relevant section
    Update front matter: last_updated = today,
                        last_update_trigger = {type: "encode-session"}
    # last_update_trigger MUST be the dict form {type: "..."}, NOT a bare
    # string. T21 (tree-front-matter-sync.py) REFUSES to sync a string trigger
    # and silently leaves _tree.yaml's last_updated stale — the 2026-06-04
    # drift incident (node windows-maxpath-pathresolution lagged 5 months) was
    # THIS lane writing the string form. The inline {type: ...} form is fine:
    # T21 bumps last_updated for both the .md FM and _tree.yaml; it skips the
    # session/source auto-fill for inline form (Layer B /tree edit fills those).
    # Matches Lane 1.6's {type: "debt-reconciliation"} form.
    # No explicit tree-update.sh --set last_updated call — the PostToolUse
    # hook (T21) fires on every tree-node Edit and, given the dict trigger,
    # atomically bumps BOTH the .md front matter AND _tree.yaml's
    # nodes[key].last_updated + top-level last_updated. A redundant explicit
    # call would just duplicate the hook's work.
    Print: ENCODED tree:<node.key> — "<one-line of what was added>"
  IF no node exists for a substantive new topic:
    PROPOSE creating it (do NOT auto-create from chat-context — too easy to
    over-encode). Print proposal: parent, key, summary.
```

### 1.2 Reasoning Bank

```
For each diagnostic insight, heuristic, or causal pattern that emerged:
  IF encoding_snapshot[{topic}].reasoning_bank has entries (Lane 1.0):
    Use the utility-ranked snapshot list; skip the category dump
  ELSE:
    Bash: reasoning-bank-read.sh --category <topic-category>
  IF semantic overlap with an existing entry:
    Bash: reasoning-bank-increment.sh <id> utilization.times_helpful
    Print: STRENGTHENED rb:<id>
  ELIF contradicts an existing entry:
    Bash: reasoning-bank-update-field.sh <id> status retired
    Bash: reasoning-bank-add.sh with new entry (supersedes)
    Print: SUPERSEDED rb:<old-id> → rb:<new-id>
  ELSE:
    # `id` is auto-allocated by reasoning-bank-add.sh inside the file lock.
    # Omit `id` from the JSON; capture the assigned id from the printed
    # record's `id` field (stdout is the full record JSON). The previous
    # "look up max + 1" recipe was racy under concurrent agent writes.
    Bash: reasoning-bank-add.sh
      title: <concise>
      type: success | failure
      category: <topic-category>
      content: the insight
      applies_to: <any|framework|domain|specific>  # REQUIRED. any=cross-cutting methodology; framework=this framework's skills/scripts/gates; domain=this agent's deployment domain (its specific services, products, integrations); specific=single-incident
      when_to_use: when this insight applies
      source_goal: <id of the Lane 2 Maintain goal that bundled this session, e.g. g-NNN-NN>
      tags: ["chat-derived"]
    Print: ENCODED rb:<id from stdout> — "<title>"
```

### 1.3 Guardrails (Operational Gotcha Auto-Detection)

Apply the same structural keyword triggers as aspirations-spark Phase 6.5 to
the conversation transcript:

PLACEMENT CHECK (before creating a guardrail in `.claude/rules/`): verify the
rule is domain-agnostic. Domain-specific operational rules (specific
endpoints, service names, product workflows) belong in `world/conventions/`
or as a `domain`-scoped guardrail entry, NOT in a core rule file. Core rules
must remain domain-agnostic per `.claude/rules/domain-free-examples.md`.

```
Signals (scan in-context conversation):
  error_then_fix:    (error|exception|traceback|failed|refused|permission denied|not found)
                     AND (fixed by|resolved by|workaround|solution|the fix|root cause|turned out)
  explicit_gotcha:   (must use|always use|never use|don't forget|gotcha|caveat|pitfall|footgun)
  environment_issue: (environment|env var|export|path|config|permission|port|firewall)
                     AND (issue|problem|wrong|missing|incorrect|unexpected)

For each signal hit:
  IF lesson is prescriptive (always|never|must|do not):
    IF encoding_snapshot[{topic}].guardrails has entries (Lane 1.0):
      Use the utility-ranked snapshot list; skip the category dump
    ELSE:
      Bash: guardrails-read.sh --category <category>
    IF semantic overlap: guardrails-increment.sh <id> utilization.times_active
    ELSE:
      # `id` is auto-allocated by guardrails-add.sh inside the file lock.
      # Omit `id` from the JSON; capture the assigned id from stdout's
      # full-record JSON. Previous max+1 recipe was racy under concurrent
      # writes.
      Bash: guardrails-add.sh
        rule: the prescriptive lesson
        category: <category>
        trigger_condition: when this gotcha applies
        source: <id of the Lane 2 Maintain goal that bundled this session, e.g. g-NNN-NN>
        tags: ["ops-gotcha", "chat-derived"]
      Print: ENCODED guard:<id from stdout> — "<rule first 60 chars>"
  ELSE (diagnostic):
    Same path as 1.2, with tags ["ops-gotcha", "chat-derived"]
```

### 1.4 Pattern Signatures

```
IF the conversation reveals a recurring procedure shape (multi-step pattern
that has shown up across goals or invocations):
  IF encoding_snapshot[{topic}].pattern_signatures has entries (Lane 1.0):
    Use the utility-ranked snapshot list; skip the category dump
  ELSE:
    Bash: pattern-signatures-read.sh --active | py -3 -c "import json,sys; [print(json.dumps(s)) for s in json.load(sys.stdin) if s.get('category')=='<category>']"
  IF no matching signature: PROPOSE adding (do not auto-create — pattern
    signatures are high-value, low-volume; surfaces best with user input).
  Print: PATTERN CANDIDATE: <description>
ELSE: SKIP silently.
```

### 1.5 Experience Archive

```
IF the conversation contained substantial work (debugging, multi-file design,
non-trivial diagnosis) that future readers would benefit from re-reading:
  experience_id = "exp-encode-session-<YYYY-MM-DD>-<slug>"
  Write agents/<agent>/experience/<experience_id>.md with:
    - Conversation summary
    - Decisions made and rationale
    - Verbatim evidence excerpts that drove decisions
  Bash: echo '<experience-json>' | bash core/scripts/experience-add.sh
    type: "chat_session"
    # Schema-valid types: goal_execution, hypothesis_formation, research,
    # reflection, user_correction, user_interaction, execution_reflection,
    # chat_session (added g-115-425, 2026-05-08). Use "chat_session" for
    # chat-mode encoding — semantically distinct from goal_execution
    # (single-goal trace) and user_interaction (Q&A). The .md front matter
    # may use descriptive type values (not validated), but the JSON body to
    # experience-add.sh MUST use a schema type or the call exits 1.
    summary: <one-line>
    tree_nodes_related: <nodes touched in 1.1>
  Print: ENCODED experience:<experience_id>
ELSE (pure Q&A, trivial chat): SKIP.
```

### 1.6 Knowledge-Debt Sweep (E17)

Chat-mode counterpart of `aspirations-consolidate` Step 2.25. `/respond` Step 6
appends entries to WM `knowledge_debt` when a user correction has broader
implications than the immediate tree nodes the `_tree.yaml` scan finds. In a
pure assistant-only session, the autonomous-loop consolidation never fires, so
those debt entries accumulate indefinitely with no resolver. This lane is that
resolver.

Run AFTER 1.1–1.5 so any debt whose target node was just edited by this
session's encoding pass auto-resolves via the mtime check.

```
Bash: bash core/scripts/wm-read.sh knowledge_debt --json

IF returned array is empty or null:
  Print: "DEBT SWEEP: no outstanding entries — skip."
  PROCEED to Lane 2.

For each debt entry (process in order: HIGH-priority first, then oldest first):

  # AUTO-RESOLVE: node was updated on or after the day the debt was filed.
  # Date-only compare (not timestamp) because node.last_updated is stored
  # date-only (YYYY-MM-DD) while debt.created is a full ISO timestamp.
  # DO NOT change to `>` or to a full-timestamp compare — same-day node
  # edits (the common case when /encode-session runs immediately after
  # an autonomous Phase 8 or Lane 1.1 touched the node) would be missed.
  Bash: bash core/scripts/tree-read.sh --node <debt.node_key> 2>/dev/null | py -3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('last_updated',''))"
  Capture returned value as node_last_updated.
  node_date = node_last_updated[:10]   # YYYY-MM-DD
  debt_date = debt.created[:10]        # YYYY-MM-DD
  IF node_date >= debt_date AND node_last_updated is non-empty:
      Mark entry.resolved = true, entry.resolution_method = "auto_resolved_by_node_update"
      Log: "AUTO-RESOLVED debt for {debt.node_key} — node updated {node_date} (debt filed {debt_date})"
      continue

  # INLINE RESOLUTION for HIGH-priority OR aged debts
  IF entry.priority == "HIGH" OR (entry.sessions_deferred ?? 0) >= 2:
    Read the target node .md file
    Decide: can this debt be resolved from in-context conversation knowledge
      (the chat session may have produced the missing content), OR from
      a quick probe (Bash, Read, grep) using domain-provisionable scripts?
    IF resolvable inline:
      Edit the node — append the missing/corrected content to the
        relevant section (Key Insights, Verified Values, etc.) AND set
        last_update_trigger in the front matter:
          last_update_trigger: {type: "debt-reconciliation"}
        (T21 will auto-fill .session and .source; last_updated also bumps.)
      Mark entry.resolved = true, entry.resolution_method = "inline_chat_resolution"
      Log: "RESOLVED debt for {debt.node_key}: <one-line summary of what was added>"
    ELSE:
      entry.sessions_deferred = (entry.sessions_deferred ?? 0) + 1
      Log: "CARRIED forward debt for {debt.node_key} (sessions_deferred={N})"
  ELSE:
    # Low-priority + young: just carry forward
    entry.sessions_deferred = (entry.sessions_deferred ?? 0) + 1

  # MAX-DEFER CEILING: drop stale debts that never resolve (mirrors Step 2.25)
  IF entry.sessions_deferred >= 10:
    Mark entry.resolved = true, entry.resolution_method = "max_defer_dropped"
    Log: "DROPPED debt for {debt.node_key} — deferred {sessions_deferred} sessions, ceiling reached"

# Build the filtered array — DROP every entry where resolved == true.
# DO NOT rely on wm-prune.sh to clean these later: knowledge_debt is NOT
# listed in `item_stale_minutes` (memory-pipeline.yaml + wm.py:249-253
# defaults), so the array-item-prune gate at wm.py:685 is unreachable for
# this slot. The protected-slot rule at wm.py:704 only fires if the outer
# gate passes. Resolved entries would otherwise live forever.
new_array = [e for e in array if not e.get("resolved")]
echo '<new_array as JSON>' | bash core/scripts/wm-set.sh knowledge_debt

Report:
  "DEBT SWEEP: {auto_resolved} auto-resolved (node already updated),
                {inline_resolved} resolved inline,
                {carried} carried forward,
                {dropped} dropped (max-defer ceiling)
                of {total_entries} total."
```

**Why "Lane 1.6" and not a top-level Lane**: knowledge-debt resolution writes
to the same stores (T, mostly) as Lane 1.1–1.5 and uses the same `tree-update.sh`
machinery. Co-locating with encoding keeps Lane 2+ focused on cognitive
primitives + discovery + meta. It also means a single Lane 1 summary line in
the Phase Final block covers both new encoding and debt resolution.

**Cross-reference**: `core/config/conventions/encoding-triggers.md` E17 row.

## Lane 2: Out-of-Cycle Work (Maintain Cognitive Primitive)

Inline completions that bypassed the goal pipeline get filed as Maintain goals
so the standard encoding pipeline runs on them per CLAUDE.md "Cognitive
Primitives".

```
For each substantive inline completion in this chat:
  # asp-001 is RETIRED (status: retired, archived: true). NEVER use it as a
  # target. Default to asp-115 (the framework-hygiene catch-all) when no
  # current-focus aspiration is set.
  target_asp = current focus aspiration (from working memory) OR asp-115
  # origin_signal MUST come from the canonical list enforced by
  # core/scripts/origin-signal-gate.py. "user_directive" is the right value
  # when the user invoked /encode-session — that IS the directive.
  # aspirations-add-goal.sh reads JSON from STDIN (BODY="$(cat)" line 103);
  # positional JSON args are silently discarded. --source picks the JSONL:
  # world (for asp-115, asp-NNN in world) vs agent (for agent-local aspirations).
  echo '{"title":"Maintain: <one-line summary of inline work>",
     "description":"Completed inline during chat session on <date>: <details>",
     "status":"completed",
     "completed_date":"<today>",
     "priority":"MEDIUM",
     "category":"<inferred>",
     "participants":["agent"],
     "origin_signal":"user_directive"}' \
    | bash core/scripts/aspirations-add-goal.sh --source <world|agent> <target-asp>
  Print: FILED MAINTAIN g-NNN-NN — "<title>"

If no inline completions: SKIP, no output.
```

## Lane 3: Unblock Re-probe

Did our chat-session work falsify any existing blocker's defer_reason?

```
Bash: aspirations-read.sh --blocked
For each blocked goal:
  Read defer_reason and goal.source (world or agent)
  IF the conversation produced evidence that falsifies the defer reason
    (new file exists, infrastructure works, capability provisioned, etc.):
      Bash: aspirations-update-goal.sh --source <goal.source> <goal-id> defer_reason ""
      Print: UNBLOCKED g-NNN-NN — defer cleared (falsified by <evidence>)
If nothing matches: print "No blockers falsified by this session."
```

## Lane 4: Work Discovery (skipped in --quick mode)

Apply the sq-013 / sq-002 / sq-c07 / sq-008 lenses to the chat session.

### 4.1 New Work (sq-013 lens)

```
Did the conversation reveal actionable work not yet tracked?
For each:
  classification = requirement | dependency | follow-up | fix | capability_gap | opportunity
  priority = HIGH (fix/dependency/requirement) | MEDIUM (follow-up/capability_gap) | LOW (opportunity)
  target_asp = current focus / matching category / new aspiration
  # origin_signal is gate-required. Pick by classification (use the prefix
  # form with a short tag, e.g. "idea:tree-decompose-helper"):
  #   requirement | dependency | fix → "unblock:<tag>" or "maintain:<tag>"
  #   follow-up   | capability_gap   → "investigate:<tag>" or "idea:<tag>"
  #   opportunity                    → "idea:<tag>"
  # JSON is read from STDIN (see Lane 2 comment).
  echo '{"title":"<concise>","description":"<what + why>","status":"pending",
     "priority":"<priority>","category":"<category>","participants":["agent"],
     "discovered_by":"encode-session-<YYYY-MM-DD>",
     "discovery_type":"<classification>",
     "origin_signal":"<see comment above>"}' \
    | bash core/scripts/aspirations-add-goal.sh --source <source> <target-asp>
  Print: FILED <classification> g-NNN-NN — "<title>"
```

### 4.2 Blind Spots (sq-002 lens)

```
Did the user surface something the agent didn't know — and SHOULD have known
from existing knowledge?
For each:
  Bash: tree-find-node.sh --text "<topic>" --top 3
  IF knowledge present in tree but agent didn't retrieve it:
    Print: BLIND SPOT (retrieval miss): <topic> exists at <node-key> but was
           not consulted. Candidate guardrail: "before answering about X,
           retrieve.sh --category Y".
  ELIF knowledge absent:
    Print: BLIND SPOT (knowledge gap): <topic> not in tree. Candidate tree
           node: <proposed-key>.
```

### 4.3 Inherited Assumptions (sq-c07 lens)

```
Did the chat reveal an assumption the agent applied without verifying?
For each:
  Same handler as aspirations-spark sq-c07: reasoning-bank-add.sh with tags
  ["first-principles", "inherited-assumption"], type: failure.
  IF the assumption was UNTESTED AND the work succeeded:
    echo '{"claim":"...","confidence":0.40,...}' | wm-append.sh micro_hypotheses
  Print: ASSUMPTION SURFACED rb:<id>
```

### 4.4 Forge Gaps (sq-008 lens)

```
Did the chat reveal a manual multi-step procedure that should be a forged skill?
IF yes:
  Bash: meta-read.sh skill-gaps.yaml
  IF gap exists for this procedure:
    Increment times_encountered, append to encounter_log
  ELSE:
    Register new gap: id: gap-{next}, status: registered, times_encountered: 1,
                      procedure_name, estimated_value
  Bash: meta-set.sh skill-gaps.yaml <updated-yaml>
  Print: FORGE GAP <gap-id> — <procedure_name>
Do NOT auto-forge — that requires curriculum permission and the /forge-skill flow.
```

## Lane 5: Verify-Learning Maintenance — sq-018 lens (skipped in --quick mode)

Did the chat-session changes touch framework files where regressions need a check?

```
5.1. SCOPE FILTER
   Bash: git status --short
   # Single source of truth: working-tree (uncommitted) scope. Same reasoning
   # as Phase 1 — chat sessions usually have not committed yet, so HEAD@{1}
   # would be empty. The aspirations-spark sq-018 handler uses HEAD@{1}..HEAD
   # because it runs post-commit in the loop; encode-session does not.
   Filter to framework-relevant paths:
     core/scripts/**, core/config/**, .claude/skills/**, .claude/rules/**,
     world/conventions/**, .claude/settings.json
   IF no framework files changed:
     Print: "encode-session: no framework files changed — no verify-learning candidates."
     SKIP rest of Lane 5.

5.2. PROPOSE CHECKS
   For each changed framework file or new behavior:
     Identify a check that catches the same regression next time:
       - New script invariant   → grep-based check
       - New file expected      → existence check (test -f / test -d)
       - New behavior           → command_check + expected output
       - New convention         → assertion that the documented rule holds
     PROPOSE adding to .claude/skills/verify-learning/SKILL.md Step 3
     (the AUTHORITATIVE CHECK SOURCE per Step 3's comment).
     Do NOT auto-edit verify-learning/SKILL.md without showing the user the
     diff first — verify-learning is a high-trust file.
     File the proposal as a Maintain-style goal under asp-115 (framework hygiene)
     so it's tracked even if the user doesn't accept inline (JSON via STDIN):
       echo '{"title":"Maintain: add verify-learning check for <file>",
          "description":"<what changed, why a check is needed, suggested check form>",
          "status":"pending","priority":"MEDIUM","category":"framework-hygiene",
          "participants":["agent"],
          "origin_signal":"maintain:sq-018-verify-learning"}' \
         | bash core/scripts/aspirations-add-goal.sh --source world asp-115
     Print: VERIFY-LEARNING CANDIDATE g-NNN-NN — <description>
            Suggested form: Check: <assertion> | Bash: <command> → <expected>
```

## Lane 6: Meta-Strategy Check (skipped in --quick mode)

```
6.1. Did the conversation explicitly improve a meta strategy
     (process-level, not domain-level — e.g., "we should always X before Y")?
     Candidate files:
       meta/goal-selection-strategy.yaml
       meta/reflection-strategy.yaml
       meta/aspiration-generation-strategy.yaml
       meta/encoding-strategy.yaml
       meta/improvement-instructions.md
6.2. PROPOSE the edit (do not auto-write meta files — high-trust).
     Log the meta-spark:
       Bash: echo '{"date":"<today>","event":"meta_spark","insight":"<insight>","source":"encode-session"}' | bash core/scripts/meta-log-append.sh
     Print: META PROPOSAL <file>: <one-line of suggested change>
6.3. IF nothing meta-relevant emerged: SKIP, no output.
```

## Lane 7: Self-Evolution — sq-012 lens (skipped in --quick mode)

Same handler as `/aspirations-spark` sq-012, but on chat-session input.

```
7.1. Read agents/<agent>/self.md
7.2. Did the conversation reveal a refinement, expansion, or course correction
     to the agent's core purpose / role / agent-provisionable actions?
7.3. Bash: curriculum-contract-check.sh --action allow_self_edits
     IF exit code 1: print "Self edit blocked by curriculum stage <stage>"
                     SKIP to 7.5.
7.4. IF 7.2 = YES, apply guard-380 classification:
     # Both `last_updated` and `last_update_trigger` MUST be set in the SAME
     # Edit so the audit trail stays accurate. Mirror sites that MUST stay
     # in sync: aspirations-spark/SKILL.md (sq-012 handler), respond/SKILL.md
     # (user-correction directive), felt-sense-checkin/SKILL.md (Material
     # lane). After Phase 7b collapse, this site no longer has a manual
     # forged-notification invocation — evolution-complete.py (Phase 5) handles
     # decisions-board posting AND user email for material self edits automatically.
     - COSMETIC change (wording, typo, formatting only):
         Edit agents/<agent>/self.md — update body AND front matter:
           last_updated: <today (YYYY-MM-DD)>
           last_update_trigger: self_evolution
         # The Phase 2 hooks (evolution-prepare -> evolution-record) captured the
         # Edit as a self-evolution.jsonl stub with status=awaiting_completion.
         # Finalize via the canonical primitive (cosmetic edits auto-skip email):
         Bash: bash core/scripts/evolution-complete.sh \
             --revision-id <stub-rev-from-self-evolution.jsonl> \
             --reasoning "<>=80-char rationale citing encode-session cosmetic signal>" \
             --signal-source encode-session \
             --signal-evidence '[{"type":"encode_session_lane","id":"lane-7","outcome":"cosmetic"}]'
         Print: SELF EVOLUTION (cosmetic, audited via self-evolution stream) — <one-line summary>
     - MATERIAL change (new/removed drive, principle, role,
       agent-provisionable action, or multi-paragraph rewrite):
         Edit agents/<agent>/self.md — update body AND front matter:
           last_updated: <today (YYYY-MM-DD)>
           last_update_trigger: self_evolution
         # The Phase 2 hooks (evolution-prepare -> evolution-record) captured the
         # Edit as a self-evolution.jsonl stub with status=awaiting_completion.
         # Finalize via the canonical primitive (Phase 5 auto-posts decisions board
         # AND auto-emails user for material self edits — no manual forged-skill
         # invocation needed here; see bible §2.4.3 — 4 mirror sites collapsed
         # in Phase 7b):
         Bash: bash core/scripts/evolution-complete.sh \
             --revision-id <stub-rev-from-self-evolution.jsonl> \
             --reasoning "<>=80-char rationale citing the encode-session signal source — sq-012, ABC drift, fresh-eyes pattern, etc — that prompted this material Self change>" \
             --signal-source encode-session \
             --signal-evidence '[{"type":"encode_session_lane","id":"lane-7","outcome":"material-applied"}]'
         Print: SELF EVOLUTION (material, audited via self-evolution stream) — <summary>
     - WEAK / uncertain signal:
         Print: SELF SIGNAL (weak) — deferred to /reflect-on-self
         Do NOT edit self.md.
7.5. Bash: spark-questions-increment.sh sq-012 sparks_generated
     # Unconditional — mirrors aspirations-spark sq-012 step 4. The metric
     # counts spark-question FIRINGS (every time the lane is considered),
     # not Self-changes. Increment fires whether 7.2 was YES/NO, whether
     # curriculum blocked in 7.3, or whether 7.4 produced a branch. Do NOT
     # gate this on "if 7.4 fired" — that under-counts vs. sq-012's metric.
```

## Phase Final: Summary

```
═══ ENCODE-SESSION COMPLETE ═══════════════════════
Lane 1 (Encoding):       <N> tree updates, <N> rb entries, <N> guardrails,
                         <N> pattern candidates, <N> experiences,
                         <N> debt resolved (<N> auto, <N> inline, <N> carried, <N> dropped)
Lane 2 (Maintain):       <N> goals filed
Lane 3 (Unblocks):       <N> defer reasons cleared
Lane 4 (Discovery):      <N> work goals filed, <N> blind spots, <N> assumptions, <N> forge gaps
Lane 5 (Verify-Learn.):  <N> check candidates filed (sq-018)
Lane 6 (Meta):           <N> meta proposals
Lane 7 (Self):           <evolution-classification or "no change">

Proposals (require user OK to write):
  <list each proposal here, with the exact tool call needed to accept>
═══════════════════════════════════════════════════
```

The terminal Bash call (per Return Protocol below) also resets the
`assistant_turn_count` working-memory slot — see Return Protocol for the
exact command. Why the reset: `/respond` Step 7.6 increments this slot on
every substantive assistant-mode turn and surfaces a nudge at multiples of
10. An encode-session run flushes in-flight learning to the tree, so the
counter must restart at 0 — otherwise the nudge re-fires on every
subsequent turn (10, 20, 30, ...) instead of waiting for a fresh window.

## Chaining

- **Called by**: User (`/encode-session`); agent (rare — only at end of complex
  chat-mode work-blocks). NOT called by `/aspirations` loop directly — that path
  uses `/aspirations-spark` per goal.
- **Calls**: `tree-update.sh`, `tree-find-node.sh`, `reasoning-bank-add.sh`,
  `reasoning-bank-increment.sh`, `reasoning-bank-update-field.sh`,
  `guardrails-add.sh`, `guardrails-increment.sh`, `experience-add.sh`,
  `aspirations-add-goal.sh`, `aspirations-update-goal.sh`,
  `pattern-signatures-read.sh`, `spark-questions-increment.sh`,
  `meta-set.sh`, `meta-log-append.sh`, `wm-append.sh`,
  `curriculum-contract-check.sh`, `git status / diff / log`.
- **Modifies**: `world/knowledge/tree/`, `world/reasoning-bank.jsonl`,
  `world/guardrails.jsonl`, `world/aspirations.jsonl`,
  `agents/<agent>/aspirations.jsonl`, `agents/<agent>/experience/`,
  `agents/<agent>/experience.jsonl`, `agents/<agent>/self.md` (sq-012 only),
  `meta/skill-gaps.yaml` (forge gap), `meta/meta-log.jsonl` (meta-spark log).
- **Does NOT modify**: agent-state, agent-mode, persona-active, session-state files,
  `verify-learning/SKILL.md` (proposes checks via Maintain goals; user accepts).

## Concurrency Note

In observer sessions (alongside a RUNNING autonomous loop), this skill writes to
shared world stores AND to per-agent working memory. Prefer running
`/encode-session` only after `/stop`, or accept that concurrent writes to
`world/aspirations.jsonl`, `world/reasoning-bank.jsonl`,
`world/guardrails.jsonl`, AND `agents/<agent>/session/working-memory.yaml`
(via Lane 1.6 knowledge_debt sweep) may collide with the autonomous loop's
writes. The framework's lockfiles handle ordering, but the LLM may briefly
see "file is locked" retries — that's expected, not a failure.

For Lane 1.6 specifically: the sweep does wm-read → mutate → wm-set across
multiple Bash invocations, releasing the WM advisory lock between them.
A concurrent autonomous-loop `wm-append knowledge_debt` between the read
and the wm-set is clobbered (lost). This is the same RMW pattern
`aspirations-consolidate` Step 2.25 uses — accepted limitation, not a bug
introduced here.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action combines the E4 counter reset with the summary echo in a
single Bash call (avoids two trailing Bashes; the reset MUST happen — see
"After the summary block" note above — and the echo provides the terminal
tool call the protocol requires):

```
Bash: echo "0" | bash core/scripts/wm-set.sh assistant_turn_count && echo "encode-session complete"
```
