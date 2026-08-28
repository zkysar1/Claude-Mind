---
name: encode-session
description: "Runs a structured learning pass on the current chat session: encode reusable insights to the knowledge tree / reasoning bank / guardrails / patterns / experience archive; file Maintain goals for inline completions; re-probe blockers our work may have falsified; surface new aspirations / goals / forge gaps; propose /verify-learning checks for framework changes; check meta-strategy and Self for evolution signals. Use only when the user explicitly types /encode-session or says 'encode this chat session' — does NOT auto-fire on conversational close-out language. Hybrid: user-invocable AND agent-callable."
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
   # Working-tree scope is the single source of truth for chat sessions (no
   # HEAD@{1} range — chat sessions usually have not committed). THREE BLIND
   # SPOTS, all guard-1947 class (an instrument that cannot see is not one
   # that saw nothing):
   # - world/ + meta/ are EXTERNAL gitignored (g-115-3392): these probes emit
   #   NO line for them, ever. Step 4's in-context memory is the ONLY scope
   #   source there — an empty git status must not short-circuit step 4. And
   #   do NOT reach for `git -C "$WORLD_PATH" status`: it resolves to the
   #   PARENT repo and reports 0 lines at rc=0 on the ignored subtree
   #   (measured 2026-08-02) — reads exactly like a clean tree.
   # - STASHED work is invisible (g-115-6265; measured cost: 226 lines of
   #   tested framework code reported as an almost-empty session). Shared
   #   tree, one index: a partner clearing the tree stashes YOUR tracked
   #   changes. Fingerprint: a `??` file whose implementation exists in
   #   neither HEAD nor the working tree ("I wrote a test for code that does
   #   not exist"). Run `git stash list` BEFORE concluding work was never
   #   written; recover with `git stash apply` (3-way, keeps the stash),
   #   never `git apply`. Full protocol: guard-3796.
1b. Bash: bash core/scripts/mirror-integrity-check.sh --no-drift
   # OWN-CLOUD ONLY (exits 0 "n/a" on every other backend). The git probes
   # above cannot see world/ at all, and the loop's MirrorWedgeProbe fires
   # only from the watchdog tick — an assistant session on an own-cloud box
   # has NEITHER instrument. Measured 2026-08-27 (rb-9443): 11 tree nodes, a
   # design SSOT among them, sat both-diverged FROZEN for ~2 days while every
   # encode pass printed ENCODED tree:<key> for edits that never left the box
   # (silent merge-handler refusal, guard-4778 — owncloud-flush reports
   # conflicts=0, so nothing else surfaces it). rc=1 WEDGED → repair BEFORE
   # Lane 1 writes more edits into a frozen file (class-B files:
   # /reconcile-owncloud-conflicts; tree nodes: guard-4778's fenced mirror_put
   # union — worked example in rb-9443). rc=2 = blind, not clean (guard-1947).
   # The script's exit code is the gate (guard-399), not this comment.
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

THEN — Tree Freshness Sweep (digest Section C2; added 2026-08-21 after a
measured miss: a node's stale "Open (unfiled)" claim survived every encode
pass for ~3 weeks until the user manually prompted a tree review). New
content is only half the tree lane — the other half keeps EXISTING nodes
truthful. Over (a) every node Lane 1.0 retrieved and (b) the catalog node
of any subsystem where this session ran a MAJOR OPERATION (promotion,
plant, migration, incident, measurement campaign — probe
tree-find-node.sh --text "<subsystem>" --top 3), run C2's four checks:
  1. CLAIM RECONCILIATION — standing claims ("Open", "pending", "not yet",
     "no X exists", dated as-of) that this session COMPLETED or OVERTOOK →
     rewrite IN PLACE ("RESOLVED (g-NNN): ..."). Evidence only, never age.
  2. CONTRADICTION CHECK — session evidence contradicting a node assertion
     → correct the wrong sentence WHERE IT STANDS, citing the evidence
     (guard-1710: never append a correction below text still asserting the
     old conclusion).
  3. LIVE-FIRE ADDENDUM — the operation's catalog node keeps dated addenda
     → append this session's event in the node's OWN format.
  4. BOLSTER AGAINST REPEAT — a mistake this session made that an existing
     node could have prevented but lacked the warning for → add the
     gotcha/decision rule to that node so future-self fails differently.
     (Node HAD it but wasn't retrieved → that's Lane 4.2's blind-spot lens,
     not a node edit.)
  Print per action: RECONCILED|CORRECTED|ADDENDUM|BOLSTERED tree:<key>
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
    id: "<experience_id>"        # REQUIRED — same slug as the .md above
    type: "chat_session"
    category: "<topic-category>" # REQUIRED — omitting it fails validation
    content_path: "agents/<agent>/experience/<experience_id>.md"  # REQUIRED
    summary: <one-line>
    tree_nodes_related: <nodes touched in 1.1>
    tags: ["chat-derived", ...]
    # ALL FIVE of id/type/category/content_path/summary are REQUIRED, and a
    # validation failure is EASY TO MISS (g-115-2847): the error object
    # parses as JSON, so reading `.id` off it yields None and "ENCODED
    # experience: None" prints while NOTHING was written. ALWAYS confirm
    # before reporting:
    #   Bash: grep -c "<experience_id>" agents/<agent>/experience.jsonl → 1
    # verbatim_anchors elements must be {"key","content"} OBJECTS, never bare
    # strings. The JSON `type` must be a schema value — "chat_session" for
    # chat-mode (vs goal_execution = single-goal trace, user_interaction =
    # Q&A); full type list + schema in the experience convention (loaded via
    # front matter). The .md front matter type is free-form; the JSON is not.
  Print: ENCODED experience:<experience_id>  (only after the grep confirms 1)
ELSE (pure Q&A, trivial chat): SKIP.
```

### 1.6 Knowledge-Debt Sweep (E17)

Chat-mode counterpart of `aspirations-consolidate` Step 2.25 — the resolver
for WM `knowledge_debt` entries filed by `/respond` (in an assistant-only
session nothing else consumes them, so they accumulate indefinitely). Runs
AFTER 1.1–1.5 so debts whose target node this pass just edited auto-resolve
on the date check. Co-located with encoding rather than a top-level lane
because it writes the same stores via the same machinery.

```
Bash: bash core/scripts/wm-read.sh knowledge_debt --json
IF empty or null:
  Print: "DEBT SWEEP: no outstanding entries — skip."  → Lane 2.

Apply digest Section E's algorithm VERBATIM (HIGH-first, then oldest):
  auto-resolve on node update (date-only >= compare, both sides [:10])
  → null-key goal-routing (the MAJORITY shape; derive asp-<NNN> from the
    goal id; NEVER resolve null-key debt on age)
  → inline resolution when HIGH or sessions_deferred >= 2
    (last_update_trigger: {type: "debt-reconciliation"})
  → durable-drop at ceiling 10: execution-diary BEFORE removal, and a
    HIGH debt at ceiling ALSO files a MEDIUM Investigate
  → self-filtered write-back via wm-set (wm-prune cannot reach this slot).

The digest is the SSOT — do NOT re-inline the algorithm here. The two
copies drifted once (digest said >=5 while this lane said >=2, and the
null-key lane existed only here; reconciled 2026-08-21).

Report:
  "DEBT SWEEP: {auto} auto-resolved, {inline} resolved inline,
               {carried} carried forward, {dropped} dropped of {total}."
```

**Cross-reference**: `core/config/conventions/encoding-triggers.md` E17 row;
`core/config/encoding-protocol-digest.md` Section E (algorithm SSOT).

## Lane 2: Out-of-Cycle Work (Maintain Cognitive Primitive)

Inline completions that bypassed the goal pipeline get filed as Maintain goals
so the standard encoding pipeline runs on them per CLAUDE.md "Cognitive
Primitives".

```
For each substantive inline completion in this chat:
  # DEPLOYMENT-ROUTED DEFAULT (g-001-195). The framework-hygiene catch-all
  # aspiration DIFFERS PER DEPLOYMENT — one world's catch-all ID may be
  # retired or entirely absent in another — so a hardcoded ID here silently
  # mis-files (or errors on a retired/absent target) after every seed plant.
  # Resolve per-deployment, in order:
  #   1. current-focus aspiration (working memory) when set;
  #   2. else THIS world's catch-all per
  #      world/conventions/deployment-routing.md (the domain overlay owns
  #      the concrete asp ID + --source value; each world carries its own);
  #   3. else probe live aspirations (aspirations-read.sh) for the active
  #      framework-hygiene/maintenance catch-all. Do NOT guess from memory,
  #      and NEVER target a retired/archived aspiration.
  target_asp = current focus aspiration (from working memory) OR <this world's catch-all per deployment-routing.md>
  # origin_signal MUST come from the canonical list enforced by
  # core/scripts/origin-signal-gate.py. "user_directive" is the right value
  # when the user invoked /encode-session — that IS the directive.
  # aspirations-add-goal.sh reads JSON from STDIN (BODY="$(cat)" line 103);
  # positional JSON args are silently discarded. --source picks the JSONL:
  # world (asp-NNN in world/aspirations.jsonl) vs agent (agent-local
  # aspirations) — deployment-routing.md records which applies here.
  # Priority stays MEDIUM (cognitive-primitives map — Maintain is never HIGH);
  # Lane 4.1's title bar applies to every goal-filing call site in this skill.
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

# *** OUTPUT SHAPE (g-115-2847, 2026-07-21): the goal fields are NESTED. ***
# --blocked returns a flat LIST of WRAPPER objects, NOT goal objects:
#     [{"aspiration_id": "asp-NNN",
#       "aspiration_title": "...",
#       "goal": { id, title, status, defer_reason, blocked_by, ... }}, ...]
# So `row["id"]` is None for EVERY row — read `row["goal"]["id"]` instead.
# Observed failure: a naive parse printed 36 rows of "(no-id) | (none)" and
# would have silently concluded there was nothing to re-probe.
#     for row in rows:
#         g = row["goal"]; asp = row["aspiration_id"]

For each row in the returned list:
  g = row.goal
  Read g.defer_reason (and g.blocked_by — a goal can be blocked by EITHER an
    unmet defer_reason OR a blocked_by dependency list; only defer_reason is
    clearable here, blocked_by clears when its upstream goals complete)
  Determine goal.source: world if row.aspiration_id is a world aspiration,
    else agent.
  IF the conversation produced evidence that falsifies the defer reason
    (new file exists, infrastructure works, capability provisioned, etc.):
      # The clear sentinel is the literal string `null`, NOT "" (verified
      # 2026-07-27, cost one failed call). aspirations-update-goal.sh guards
      # its positionals with `[ -z "$VALUE" ]` and exits 1 on an empty string
      # ("Error: goal_id, field, and value are all required."); only `null`
      # reaches the parse_value branch that writes JSON null (script L130).
      Bash: aspirations-update-goal.sh --source <goal.source> <g.id> defer_reason null
      Print: UNBLOCKED <g.id> — defer cleared (falsified by <evidence>)
If nothing matches: print "No blockers falsified by this session."
# Beware keyword-only matching: filtering blocked goals by topic keywords
# ("session", "account", "key") over-matches badly — 25 of 36 rows matched an
# identity-work filter while none were actually falsifiable. Read the actual
# defer_reason before concluding relevance.
```

## Lane 4: Work Discovery (skipped in --quick mode)

Apply the sq-013 / sq-002 / sq-c07 / sq-008 lenses to the chat session.

### 4.1 New Work (sq-013 lens)

```
Did the conversation reveal actionable work not yet tracked?
For each:
  classification = requirement | dependency | follow-up | fix | capability_gap | opportunity
  # PRIORITY IS DERIVED FROM CLASSIFICATION — never chosen on felt urgency.
  # Field incident 2026-08-26 (sera/serene): a day-old deployment filed a
  # capability_gap as HIGH with a hedge-word title; MEDIUM was the mapped
  # value. (Cognitive-primitive prefix goals map per CLAUDE.md "Cognitive
  # Primitives": Unblock=HIGH, Investigate/Idea/Maintain=MEDIUM.)
  priority = HIGH (fix/dependency/requirement) | MEDIUM (follow-up/capability_gap) | LOW (opportunity)
  # TITLE BAR: the title states the concrete deliverable. Hedge/filler
  # adjectives ("robust", "potentially", "comprehensive", "improved") are
  # barred unless the description carries concrete verification criteria.
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
                      procedure_name, estimated_value,
                      type: <utility|analytical>   # REQUIRED (g-115-3131)
    # `type` gates the forge developmental bar — absent hands that decision to
    # a default instead of you. Per core/config/skill-gaps.yaml gap_types:
    # utility = mechanizes an ALREADY-DERIVED procedure -> CALIBRATE;
    # analytical = the OUTPUT needs domain-mature judgment -> EXPLOIT (higher
    # bar). Unsure -> `utility`; it IS the default, so stating it costs nothing.
  # meta-set.sh is a DOTPATH setter — `meta-set.sh <file> <dotpath> <value>`.
  # It does NOT accept a whole-file YAML rewrite; passing one exits 1 with the
  # bare usage line (verified 2026-07-27, cost two failed attempts). Find the
  # gap's list index first, then set the fields individually. Bracket and dot
  # index forms are equivalent (meta-yaml.py:110 normalizes `[N]` -> `.N`), and
  # a JSON array value round-trips as a real YAML list (parse_value, g-115-1263)
  # — see guard-661 for the verified contract and the one residual caveat
  # (read-modify-write across two daemon calls is a TOCTOU race; use
  # _fileops.locked_modify_yaml when a concurrent writer is possible).
  Bash: meta-set.sh skill-gaps.yaml "gaps[<i>].times_encountered" <n+1> --reason "<goal-id> encounter"
  Bash: meta-set.sh skill-gaps.yaml "gaps[<i>].encounter_log" '<full JSON array incl. the new entry>'
  # WRITE-INTEGRITY READ-BACK (g-115-3177 — mirrored from aspirations-spark's
  # sq-008 handler, which gained it while this lane drifted without it.
  # Measured 2026-08-26 (sera/serene): a run reported "1 forge gaps" while
  # skill-gaps.yaml carried no new entry — the claim was printed from the
  # write attempt, not the store; same class as Lane 1.5's g-115-2847.
  # meta-read.sh is a synchronous YAML read, so read-back is valid here — the
  # guard-4631 spool caveat applies to utilization-sidecar increments only.)
  IF meta-set.sh exit code != 0:
    Log the FULL stderr — do NOT print the FORGE GAP line.
    Re-read via meta-read.sh; if the gap is genuinely absent, file
    "Investigate: skill-gaps.yaml write failed — <error>" (HIGH, participants
    [agent]), then continue (never block the pass on it).
  ELSE (rc == 0):
    # GATE THE VERDICT ON rc, NOT THE READ-BACK (g-115-3522). They answer
    # different questions; conflating them files a false HIGH goal on a write
    # that actually succeeded.
    Bash: meta-read.sh skill-gaps.yaml → gap id present with a non-null `type`?
    PRESENT → print the FORGE GAP line. ABSENT → UNKNOWN, never "failed", and
    never the rc!=0 branch: rc=0 IS the write verdict, so print the line marked
    UNCONFIRMED and continue. Do NOT re-run the write — encounter_log is
    append-only, so the retry succeeds and duplicates silently (guard-1578).
    A same-box read here is synchronous (see above), so ABSENT is rare; when it
    happens suspect a cross-box mirror read-through (guard-980), not a failure.
  Print: FORGE GAP <gap-id> — <procedure_name>   [+ " (UNCONFIRMED)" on ABSENT]
Do NOT auto-forge — that requires curriculum permission and the /forge-skill flow.
```

## Lane 5: Verify-Learning Maintenance — sq-018 lens (skipped in --quick mode)

Did the chat-session changes touch framework files where regressions need a check?

```
5.1. SCOPE FILTER
   Bash: git status --short
   # Single source of truth: working-tree (uncommitted) scope. Same reasoning
   # as Phase 1 — chat sessions usually have not committed yet, so any
   # commit-relative range would be empty.
   # (Corrected 2026-07-30, g-115-3539: this comment used to say the
   # aspirations-spark sq-018 handler uses HEAD@{1}..HEAD "because it runs
   # post-commit in the loop". BOTH halves were wrong — guard-1320 fires that
   # spark inline BEFORE state-update commits, and the range was a defect
   # rather than a design: it spanned iteration-push merges and attributed a
   # PARTNER's files to the goal. It now resolves the goal's own commits and
   # falls back to this same working-tree scope. See guard-2001 / rb-5942.)
   Filter to framework-relevant paths:
     core/scripts/**, core/config/**, mind_api/src/**, .claude/skills/**,
     .claude/rules/**, .claude/settings.json
   # world/ and meta/ are deliberately NOT listed: they are external gitignored
   # paths (.gitignore `/world/`, `/meta/`, `/.mind-data/`), so `git status
   # --short` can never report them. Listing them would look like coverage
   # while matching nothing — the same dead arm removed from sq-018 in
   # g-115-3539 (rb-5942).
   #
   # WHAT COVERS THEM INSTEAD (g-115-3392): nothing here does, and nothing
   # here can. A convention or knowledge-tree edit made this session reaches
   # Lane 5 ONLY through your own in-context memory of having made it —
   # Phase 1 step 4's conversation-scope pass is the sole path. So the SKIP
   # below means "no TRACKED framework file changed", NOT "no framework-
   # relevant work happened": if you edited a convention or a tree node this
   # session and Lane 5 would have proposed a check for it, propose that
   # check anyway. An empty `git status` is not permission to skip.
   #
   # Do NOT try to close this with `git -C "$WORLD_PATH" status` — MEASURED
   # and it fails SILENTLY (2026-08-02, cc-03): `--show-toplevel` from
   # inside the world dir resolves to the PARENT repo, which has that path
   # ignored, so it returns 0 lines at rc=0 and reads exactly like a clean
   # tree. An mtime sweep is unsound for a different reason — under
   # own-cloud the local tree is a read-through cache, so a PARTNER's synced
   # edit moves local mtimes and cannot be told from your own. The mtime
   # sweep is only sound where an authorship filter exists: 1269/1335 (95%)
   # of tree nodes carry `last_update_trigger.session`, but only 6 of 66
   # conventions carry ANY front matter, so no such filter exists for the
   # conventions half.
   #
   # THE TREE HALF IS NOW PROBED FOR REAL (g-115-4714). That asymmetry used
   # to be the reason NO probe was built here; it is now the reason the probe
   # is tree-ONLY. `--list` returns the nodes this session encoded, filtered
   # by `last_update_trigger.session` — the authorship filter that makes an
   # mtime sweep sound under own-cloud. It reuses the boolean detector's own
   # attribution function rather than reimplementing it, so the two cannot
   # drift apart.
   Bash: sess=$(bash core/scripts/wm-read.sh session_start 2>/dev/null)
   Bash: py -3 core/scripts/tree-edit-since.py "$sess" --list
   # rc 0 = attributable nodes on stdout, one relative path per line.
   # rc 1 = none. Nodes another session stamped are counted out and reported
   # on stderr, so a peer's synced edit cannot be read as your own.
   #
   # ITS SCOPE IS TREE-ONLY AND MUST BE REPORTED THAT WAY. Conventions stay
   # in-context-only — there is no authorship filter to run on 6-of-66 front
   # matter, and none can be built from data that does not exist. Calling
   # this output "world/ coverage" would rebuild this lane's original defect
   # in a new place: a probe covering half the surface reading as coverage of
   # all of it. Say "tree nodes"; never "world files".
   #
   # AN UNSET session_start MEANS BLIND, NOT CLEAN (guard-1947 — the class
   # Phase 1 already names). Empty $sess = the probe could not run, so the
   # tree half falls back to in-context memory exactly like conventions do.
   # Report it as BLIND; it is not evidence that nothing was encoded.
   IF no framework files changed AND the tree list is empty:
     Print: "encode-session: no framework files changed, no tree nodes encoded this session — no verify-learning candidates."
     SKIP rest of Lane 5.
   # EITHER source non-empty → continue to 5.2. A session that encoded tree
   # nodes but touched no tracked file is precisely the case the old
   # git-status-only SKIP dropped in silence.

5.2. PROPOSE CHECKS
   For each changed framework file, encoded tree node, or new behavior:
     Identify a check that catches the same regression next time:
       - New script invariant   → grep-based check
       - New file expected      → existence check (test -f / test -d)
       - New behavior           → command_check + expected output
       - New convention         → assertion that the documented rule holds
     PROPOSE adding to .claude/skills/verify-learning/SKILL.md Step 3
     (the AUTHORITATIVE CHECK SOURCE per Step 3's comment).
     Do NOT auto-edit verify-learning/SKILL.md without showing the user the
     diff first — verify-learning is a high-trust file.
     File the proposal as a Maintain-style goal under this world's
     framework-hygiene catch-all (resolve via
     world/conventions/deployment-routing.md — same protocol as Lane 4;
     g-001-195) so it's tracked even if the user doesn't accept inline
     (JSON via STDIN):
       echo '{"title":"Maintain: add verify-learning check for <file>",
          "description":"<what changed, why a check is needed, suggested check form>",
          "status":"pending","priority":"MEDIUM","category":"framework-hygiene",
          "participants":["agent"],
          "origin_signal":"maintain:sq-018-verify-learning"}' \
         | bash core/scripts/aspirations-add-goal.sh --source <world|agent> <catch-all per deployment-routing.md>
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
7.4. IF 7.2 = YES:
     # AUDIT-MACHINERY GATE (field incident 2026-08-26, sera/serene): a thin
     # day-old deployment declared a MATERIAL change while the evolution audit
     # stream did not exist there — a Material claim with no audit artifact
     # behind it. The honest branch was WEAK/deferred. Probe BEFORE classifying:
     Bash: test -x core/scripts/evolution-complete.sh        # machinery present?
     AND after the self.md Edit: confirm the evolution-prepare hook captured a
     stub — an awaiting_completion record for this revision in
     world/self-evolution.jsonl.
     IF either probe fails (script absent, or no stub captured):
       Do NOT classify COSMETIC or MATERIAL — no audit artifact, no claim.
       Print: SELF SIGNAL (weak) — deferred: audit machinery unavailable
              (<which probe failed>). Signal was: <what 7.2 found>
       File "Idea: revisit deferred self-evolution signal — <the signal>"
       (MEDIUM) so the content survives until the machinery lands, then SKIP
       to 7.5. Do NOT edit self.md on this branch.
     Apply guard-380 classification:
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
         Print: SELF EVOLUTION (cosmetic, audit artifact: <revision-id>) — <one-line summary>
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
         Print: SELF EVOLUTION (material, audit artifact: <revision-id> +
                decisions-board post + user email per evolution-complete.sh
                Phase 5) — <summary>
         # No named artifact = no Material claim. If evolution-complete.sh
         # errored, fall back to the WEAK-branch wording of the gate above.
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

**Root-cruft check (runs first — feeds the Proposals block).** The L1 hook
governs only Write/Edit; Bash mkdir/heredoc/redirects create repo-root files
invisibly. Field incident 2026-08-26 (sera/serene): a session left
temp_*/list_* ground-truth scripts at PROJECT_ROOT and the encode pass ended
without noticing them. From Phase 1's `git status --short`: any top-level
untracked file (`?? <name>` with no slash) matching session-temp shapes
(temp*, tmp*, scratch*, list_*, *ground_truth*) — or any root file this
session itself created — gets ONE Proposals entry naming the files, the
sanctioned homes (`agents/<agent>/temp/` for lifecycle files,
`agents/<agent>/sessions/<SID>/scratch/` for scratch —
`.claude/rules/no-scratchpad.md`), and the proposed move/delete. PROPOSE
ONLY — never auto-delete (`archive-before-delete.md`; the user may want one
kept).

```
═══ ENCODE-SESSION COMPLETE ═══════════════════════
Lane 1 (Encoding):       <N> tree updates (<N> reconciled / <N> corrected /
                         <N> addenda / <N> bolstered — the C2 freshness sweep),
                         <N> rb entries, <N> guardrails, <N> pattern candidates,
                         <N> experiences, <N> debt resolved (<N> auto, <N>
                         inline, <N> carried, <N> dropped)
Lane 2 (Maintain):       <N> goals filed
Lane 3 (Unblocks):       <N> defer reasons cleared
Lane 4 (Discovery):      <N> work goals filed, <N> blind spots, <N> assumptions, <N> forge gaps
Lane 5 (Verify-Learn.):  <N> check candidates filed (sq-018)
Lane 6 (Meta):           <N> meta proposals
Lane 7 (Self):           <evolution-classification or "no change">

Proposals (genuinely-open items only — see reconciliation rule below):
  <for each: DEFER-REASON + MECHANISM=PROBED|INFERRED — the stated reason,
   then the exact tool call needed to accept>
═══════════════════════════════════════════════════
```

**The Proposals block lists only genuinely-open items.** Before printing it,
reconcile each candidate against this run's OWN action lines: anything already
materialized this pass (an ENCODED / RECONCILED / CORRECTED / FILED /
FORGE GAP line above) is an action, not a proposal — a proposals entry
contradicting the run's own output is itself the defect (field incident
2026-08-26, sera/serene: a tree node listed as a pending proposal minutes
after its ENCODED line printed). Every surviving proposal carries its
deferral reason (needs user OK / over-encode risk / machinery unavailable)
alongside the MECHANISM mark below.

**Mark every proposal's stated MECHANISM as PROBED or INFERRED.** A proposal
carries two separable claims: the ACTION and the MECHANISM justifying it.
The mechanism is a factual assertion about how the system behaves — it can
be FALSE while the action is still right, so it needs evidence in its own
right. If it was not probed this session, write `INFERRED` and name the one
probe that would settle it; never present an unprobed mechanism in the same
voice as a measured one. Canonical incident + relation to
communication-clarity rule 6:
`core/config/rationale/proposal-mechanism-marking.md`.

The terminal Bash call (per Return Protocol below) also resets the
`assistant_turn_count` working-memory slot — see Return Protocol for the
exact command. Why the reset: `/respond` Step 7.6 increments this slot on
every substantive assistant-mode turn and surfaces a nudge at multiples of
10. An encode-session run flushes in-flight learning to the tree, so the
counter must restart at 0 — otherwise the nudge re-fires on every
subsequent turn (10, 20, 30, ...) instead of waiting for a fresh window.

### Phase Final.5: Session-Close Commit (g-115-6126)

Runs AFTER the summary block and proposals, BEFORE the Return Protocol
terminal call. This is the chat-mode analogue of the loop's iteration-close
commit, and it exists because assistant mode otherwise has NO commit/push
obligation anywhere: `core/config/modes/assistant.md`, `respond/SKILL.md`,
and `stop/SKILL.md` contain zero git protocol (measured 2026-08-13/14), the
graceful-stop D6.65 push-flush runs only on the RUNNING-loop path, and
`iteration-push.sh` self-heal-commits ONLY `agents/<self>/*` churn
(g-115-2249 pathspec) — so a framework edit (`core/**`, `.claude/**`,
`CLAUDE.md`) made in assistant mode is committed by NOTHING and sits
uncommitted until a hand-sweep (measured: the g-115-6098 edit sat 01:05 →
~10:20 on 2026-08-13 until swept as 77a8bd80a; three such hand-sweeps on
08-11/12/13 are exactly this step, done manually).

```
# 1. Commit ALL tracked churn (framework + agent) with standard attribution.
#    iteration-commit.sh no-ops on a clean tree, filters sensitive patterns
#    (.env*, *.key, ...) and partner WIP (cross-agent mtime filter) — run it
#    unconditionally. --outcome deep is REQUIRED: the script no-ops on
#    routine by design, and a session that reached this skill has learning
#    writes worth committing.
#    THE `source` IS LOAD-BEARING, NOT DECORATION (measured 2026-08-19): a
#    bare Bash tool call has $PROJECT_ROOT UNSET, so `--repo "$PROJECT_ROOT"`
#    passes an EMPTY value and the script exits 1 with
#    "missing required flag(s): --goal-id, --title, --outcome, --repo are all
#    required" — naming all four flags you definitely passed. That error reads
#    as a broken script, and an LLM that believes it skips the commit, which
#    restores the exact assistant-mode gap g-115-6126 added this step to close.
Bash: source core/scripts/_paths.sh && bash core/scripts/iteration-commit.sh \
        --goal-id encode-session \
        --title "session-close learning flush" --outcome deep \
        --type chore --repo "$PROJECT_ROOT"

# 2. Integrate + push (fetch, merge --no-edit — i.e. pull --no-rebase — then
#    push). Same shared component the loop and D6.65 use; fail-soft by
#    contract, so a network blip degrades to "committed locally, push next
#    session" and is logged loudly. Never let a push failure block the
#    terminal call. This is ALSO the session-END pull: fetch is throttled
#    (~10-min interval) but integrate runs every call, even when the push
#    defers. Session-START freshness is owned by assistant.md
#    "Session-Start Sync" (iteration-push.sh --no-push, g-115-3871) — do
#    not add a second pull path here.
Bash: bash core/scripts/iteration-push.sh

# 3. OWN-CLOUD ONLY — read back what this session encoded (exits 0 "n/a"
#    elsewhere). Step 2 pushed the git-tracked half; nothing above proves the
#    world/ half LANDED. This HEADs every tree node this session attributed
#    to itself (tree-edit-since.py) against the authoritative object and
#    re-reads the streak verdict AFTER the flush has had its chance. rc=1
#    (DRIFT or WEDGED) means an "ENCODED tree:<key>" line printed above was
#    FALSE — repair now (rb-9443 recipe), re-run until OK, and say so in the
#    summary. rc=2 (blind) is not clean. Never lets a failure block the
#    terminal call. Sibling of Phase 1 step 1b (entry) — entry catches a
#    wedge before Lane 1 builds on it, exit catches one this session caused.
#    Cost: one remote HEAD per attributed node, ~3-4s each (measured
#    2026-08-27: 53 nodes in 3m18s) — run it in the foreground, it is the
#    only step that proves the tree half of the flush landed.
Bash: bash core/scripts/mirror-integrity-check.sh
```

**MIRROR ASYMMETRY — do NOT copy this step to `/aspirations-spark`.** The
header rule ("the two skills must evolve together") covers the ENCODING
lanes. This step is deliberately encode-session-ONLY: the autonomous loop
already commits at iteration-close (`iteration-commit.sh` from
state-update/iteration-close wiring) and pushes at `do_productivity_check`,
so mirroring this step into the loop path would double-commit every
iteration. The asymmetry is the fix, not an oversight. Step 3 (and Phase 1
step 1b) are likewise encode-session-ONLY: the loop already runs
MirrorWedgeProbe from the watchdog tick every iteration. The remaining gap is
the hook-level gate for chat mode (SessionStart banner / PreToolUse[Edit]
advisory on a frozen node) — tracked by g-115-8029, not by this skill.

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
  `curriculum-contract-check.sh`, `git status / diff / log`,
  `iteration-commit.sh` + `iteration-push.sh` (Phase Final.5 session-close
  commit — g-115-6126).
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
