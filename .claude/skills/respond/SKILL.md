---
name: respond
description: "Handles every user message in assistant and autonomous modes: activates persona, runs mandatory 3-tier retrieval escalation (tree → codebase → web), detects and routes user directives (new aspirations, corrections, preferences), performs knowledge-freshness reconciliation, and captures interaction-level learning into reasoning bank and guardrails. Use whenever the user sends a message — every user turn MUST route through /respond, otherwise Claude may answer from amnesia instead of retrieving learned knowledge."
user-invocable: false
triggers: [user-message, directive, user-chat, user-reply, user-response-routing]
conventions: [aspirations, tree-retrieval, retrieval-escalation, session-state, experience, reasoning-guardrails, pipeline, journal]
minimum_mode: reader
revision_id: "skill-bootstrap-respond-8bab1c"
previous_revision_id: null
---

# /respond — User Message Handler

> **CRITICAL**: Step 4 (Knowledge Search) is NOT optional. You MUST attempt retrieval
> before answering ANY domain question. Follow the 3-tier escalation (tree → codebase → web)
> defined in Step 4. Never say "I don't have context" without attempting all eligible tiers.

Handles all user messages across agent states. Loaded on-demand when a user message arrives (routed from `.claude/rules/user-interaction.md` stub).

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Persona Gate

Bash: `session-persona-get.sh` → read output:
- If `false`: skip all persona/knowledge/surfacing behavior — act as a standard Claude Code assistant. Still process directives (Step 5).
- If `true` (default): apply full persona behavior below.
- If `unset`: default to `true`.

## Step 2: Determine Agent State

Bash: `session-state-get.sh` → read output:
- `RUNNING` → Step 3a
- `IDLE` → Step 3b
- `UNINITIALIZED` → Step 3c

## Step 3a: RUNNING State Response

1. Acknowledge user immediately — respond before any other work
2. Read `agents/<agent>/profile.yaml` → use persona settings (tone, verbosity)
3. **MANDATORY**: Execute knowledge search (Step 4) — do NOT skip this step
4. Surface 1-2 pending questions or user goals (Step 4b)
5. Process directives (Steps 5-7.5) if applicable
6. **NEVER ask the user a question and wait for a response.** If you need user
   input, write a pending question to `agents/<agent>/session/pending-questions.yaml`
   (with `default_action` describing what you'll do autonomously) or create a
   goal with `participants: [user]`. Then immediately continue to step 7.
7. **Re-enter the loop**: `Skill('aspirations') with args='loop'`
   This is NOT optional. This Skill() call MUST be your FINAL action.
   NEVER produce text-only output after steps 4-6 without calling this.
   NEVER say "should I continue?" — just continue.

## Step 3b: IDLE State Response

1. Read `agents/<agent>/profile.yaml` → use persona settings (tone, verbosity)
2. **MANDATORY**: Execute knowledge search (Step 4) — do NOT skip this step
3. Surface pending questions and user goals (Step 4b)
4. On first message only: mention `/start` is available to resume the autonomous loop (do not repeat)

## Step 3c: UNINITIALIZED State Response

1. Warm welcome: "I'm a continual learning agent. To get started, you'll define The Program (the world's shared purpose) and my Self (my identity as an agent), plus initial aspirations. The Program tells the world WHY it exists. My Self tells me WHO I am. Aspirations are the goals I'll work toward."
2. Show commands:
   - `/start` — Define The Program, my Self, and aspirations, then begin the autonomous learning loop
3. Prompt: "Type `/start` to get going! I'll ask you for The Program, your agent's identity, and aspirations."
4. If user seems confused, suggest example Programs, agent identities, and aspirations or offer a guided walkthrough
5. Answer any questions about the system naturally

## Step 4: Knowledge Search (Escalated Retrieval)

Applies in all states when persona is active. Follows the retrieval escalation convention
(`core/config/conventions/retrieval-escalation.md`). Stop at the first tier that provides
sufficient knowledge. Context manifests and quality ratings are NOT needed for conversational responses.

### Tier 1 — Knowledge Tree

- Bash: `session-mode-get.sh` → if `reader`, add `--read-only` flag below
- Bash: `retrieve.sh --category {topic} --depth medium --include-framework [--read-only]` (default depth)
- Escalate to `--depth deep` if user asks about a specific topic (preserve `--include-framework`)
- retrieve.sh returns tree nodes, reasoning bank, guardrails, pattern signatures, experiences, **and framework rules/conventions** (via `--include-framework`, G8 / R13 — the actual rule files surface alongside tree nodes when query tokens overlap rule titles or section headers; load is O(ms), returns [] when no token match, so safe to leave on for all Tier 1 queries)
- **Sufficiency check**: Does this answer the user's question confidently?
  - **YES** → use tree knowledge, proceed to Step 4b
  - **PARTIAL or NO** → note what's missing, proceed to Tier 2

### Tier 2 — Codebase Exploration

- Read `agents/<agent>/self.md` to identify the primary workspace path (if not already known)
- If no workspace configured: skip Tier 2
- Use targeted searches related to the user's question:
  - **Grep**: search for function names, config values, error messages, patterns
  - **Glob**: find relevant files by name pattern
  - **Read**: examine key files identified by Grep/Glob
- Keep searches targeted — 2-3 specific queries, not exhaustive
- **Sufficiency check**: Does tree + codebase answer the question?
  - **YES** → use combined knowledge, proceed to Step 4b
  - **NO** → proceed to Tier 2.5

### Tier 2.5 — Peer Worlds

- Bash: `peer-retrieve.sh --json "{topic}"` — a peer Mind deployment may already hold what this world does not, so only `status: empty` at `completeness: complete` (rc=0) supports "nobody has this"; rc=3 / `partial` (or `status: unreachable`) means a lane was BLIND and its emptiness is not evidence
- **Sufficiency check**: **YES** → proceed to Step 4b · **NO** → check mode for Tier 3 eligibility

### Tier 3 — Web Search (assistant/autonomous only)

- Bash: `session-mode-get.sh` → if `reader`, SKIP Tier 3 entirely
- WebSearch: targeted query for the specific knowledge gap
- Optional: WebFetch top result if it looks authoritative
- Use combined knowledge from all tiers, proceed to Step 4b

### If All Eligible Tiers Exhausted

- Be transparent about what was tried: "I searched my knowledge tree, explored the codebase,
  [and checked the web,] but don't have information on that yet."
- If IDLE or UNINITIALIZED: suggest "Run `/start` and I can begin learning about this!"

## Step 4.5: Tier-Escalation Encoding Debt (E1)

If Step 4 had to escalate past Tier 1 (knowledge tree) to answer, that IS
evidence the tree had a gap. Queue a `knowledge_debt` entry naming the topic
so the next encoding pass (Phase 4.5 in autonomous, `/encode-session` Lane 1.6
in assistant) can resolve it. Without this, every Tier-2/Tier-3 answer leaks —
the agent re-discovers the same gap on the next question.

Skip entirely in reader mode (no writes). Skip if all Tier-1 retrieval
suffficed (the common case). Skip if the question was UNINITIALIZED-state
small-talk (no world to write to).

```
1. Bash: session-mode-get.sh
   IF mode == "reader": SKIP entire Step 4.5 (no writes allowed).

2. Self-classify the answer source:
   - tier_used = "tier-1" | "tier-2" | "tier-3" | "exhausted"
     "tier-1": the tree (T+R+G+P+E+B+X+F via retrieve.sh) provided
               sufficient knowledge — no debt to file.
     "tier-2": had to Grep/Glob/Read the primary workspace because tree was
               thin or silent on the topic.
     "tier-3": had to WebSearch/WebFetch because tree AND codebase were
               silent.
     "exhausted": all eligible tiers tried, no answer — file the GAP, not
                  a tier-escalation debt (still useful).

   IF tier_used == "tier-1": SKIP rest of Step 4.5 (common path — no debt).

3. Identify the topic — one-line description of what the question was about
   (e.g., "Operator API health-check endpoint URL", "how knowledge-tree
   encoding gate scores combine").

4. Optional: suggest a target tree node where the missing content SHOULD
   live. Run `Bash: bash core/scripts/tree-find-node.sh --text "<topic>"
   --top 1` — if the top match has a high score AND covers the right
   subject, use its key. Otherwise leave node_key=null and let the
   consumer (Lane 1.6 / Phase 4.5) decide.

5. Build the debt entry. The wm-append schema gate (wm.py:484-306) requires
   either a valid node_key OR a non-empty `reason` for null node_key; the
   form below satisfies both branches.

   Inspect Tier-1's retrieve.sh result `meta.empty_with_populated_siblings`
   (E12 signal). If set, the tree has scattered coverage of the topic but
   no dedicated node. Promote the priority to HIGH regardless of tier and
   embed the populated_token + sample_node_keys directly in the reason
   text — those help Lane 1.6 / Phase 4.5 pick the right reparenting
   target. Do NOT duplicate the info into a structured field; the reason
   text is the single source of truth.

   echo '<JSON>' | bash core/scripts/wm-append.sh knowledge_debt

   JSON shape:
   {
     "node_key": "<resolved key from step 4, or null>",
     "reason": "tier-{2|3}-escalation: <topic>" + (E12 suffix below if set),
     "source_goal": "respond-step-4.5",
     "priority": "<MEDIUM if tier-2, HIGH if tier-3/exhausted/E12-signal>",
     "created": "<today ISO>",
     "tier_used": "<tier-2|tier-3|exhausted>",
     "question_excerpt": "<first 80 chars of user question>"
   }

   E12 reason suffix (when set):
     " [coverage-gap: token '<populated_token>' appears in <N> nodes
        including <sample_node_keys[:3]> — no dedicated node]"

   priority rationale:
   - tier-2 = MEDIUM (codebase explanation can be re-grepped cheaply)
   - tier-3 = HIGH  (web content is volatile; encode now or lose it)
   - exhausted = HIGH (no answer means a real gap)
   - E12-signal = HIGH (positive evidence that the topic warrants a
     dedicated node — promotes whatever tier was used)

6. Log to execution diary so the audit trail shows the debt was filed:
   echo '{"entry_type":"observation","content":"Step 4.5 tier-escalation debt: tier={tier_used} topic=[{topic}] node_key={key|null}"}' | bash core/scripts/execution-diary.sh append

   Fail-open: if wm-append errors (validation, lock contention), log to
   execution-diary anyway. The debt may be lost, but the user-facing answer
   is unaffected. Never block the response on a debt-filing failure.
```

**Why this is not Step 6**: Step 6 fires on user CORRECTIONS — the user says
"X is actually Y." Step 4.5 fires on agent-detected GAPS — the tree didn't
have it and the agent had to look elsewhere. Different signal, different
encoding routing.

**Cross-reference**: `core/config/conventions/encoding-triggers.md` E1 row.

## Step 4b: Surfacing

### Reader-Mode Observation Surfacing (E5)

Rationale (WHY reader mode surfaces observations in response text rather than writing): `core/config/rationale/respond.md`

```
Bash: session-mode-get.sh → MODE

IF MODE != "reader": SKIP this section (other modes handle the
                     observation via Step 6/6.5/7.5e directly).

During Step 4 retrieval, if the agent noticed any of:
  - Tier-1 result is older than the file's actual mtime (tree.last_updated
    earlier than the source file's last edit date)
  - Two retrieved nodes contradict each other on the same fact
  - Tier-2 (codebase) result contradicts Tier-1 (tree) on a specific value
  - A node's `confidence` field is stale-looking (high confidence on a
    topic the codebase appears to have rewritten)

THEN append a one-line note to the response, in the form:
  "(reader-mode observation: <node_key> / <what's odd>. To capture this,
   switch to assistant via `/start <agent> --mode assistant` then run
   `/encode-session`.)"

Surface AT MOST ONE observation per turn — repeated reader-mode nudges
become noise. Pick the highest-impact one.

IF no observation noticed: omit the note (default — most turns have none).
```

This is conversational only; no scripts called, no files written. Reader
mode's no-write contract is preserved.

### Priority Review Surfacing (check first — surface before other items)

Bash: `bash core/scripts/pending-questions-read.sh --type priority-review --status pending`
(Shape-tolerant reader — flattens the dict-wrapper / list-with-wrapper / bare /
 mixed on-disk shapes via _load_questions, rb-1786. Do NOT hand-roll a naive
 top-level scan of the raw YAML: it silently SKIPS wrapper-nested entries,
 g-115-3039.)
IF the returned JSON array is non-empty (a `type: priority-review`, `status: pending` entry exists):
  Surface prominently:
  "I've been creating aspirations autonomously and would value your input on
   priorities — say '/priority-review' or just tell me what matters most."
  (Surface once per conversation — skip if already shown this session)

### Fresh-Eyes Review Surfacing (check second — distinct pull from priority-review)

Bash: `bash core/scripts/pending-questions-read.sh --type fresh-eyes-review --status pending`
(Shape-tolerant reader, rb-1786 / g-115-3039 — see the note above; never
 hand-roll a naive top-level scan.)
IF the returned JSON array is non-empty (a `type: fresh-eyes-review`, `status: pending` entry exists; use its `id` as {pq.id}):
  Surface prominently (once per conversation):
  "I have a periodic fresh-eyes check waiting for you (id {pq.id}):
    1. Is the current Self still right?
    2. Are we working on the right problems?
   Full briefing at `agents/<agent>/reports/{pq.id}.md`. Reply in this chat or via email;
   a 'Self is still right' / 'shift focus to X' answer is enough — I'll route it."
  (The fresh-eyes cadence gate fires every 25 completed goals. While this pq
   is pending, the next cycle is blocked — resolving it unblocks the cadence.
   See `.claude/skills/fresh-eyes-review/SKILL.md`.)

### Pending Questions

- Bash: `bash core/scripts/pending-questions-read.sh --status pending`
  (Shape-tolerant reader, rb-1786 / g-115-3039 — see the note above; never
   hand-roll a naive top-level scan of the raw YAML.)
- For the returned entries (excluding `type: priority-review` already surfaced above),
  weave 1-2 into conversation naturally
- Or append: "By the way, I had a question: {question}"
- When user answers, update status to `answered` and record the answer.
  After status transitions to `answered` or `superseded`: `Bash: session-signal-set.sh pq-resolved`
  (wakes any aspirations loop currently in blocked-sleep backoff; non-blocking)
- **Stale user-gate clear (rb-2516 / g-115-1693).** If the answered pq was a
  user-DECISION question that gated a parked goal -- its `context` or
  `source_goal` names a goal whose `participants` still contains `user` (parked
  per guard-571) -- AND the user's answer AUTHORIZES the agent portion (an
  unambiguous go-ahead, NOT a "no" / "keep it parked" / partial-or-conditional
  answer), drop `user` from that goal so it re-enters the agent selector lane:
  `Bash: aspirations-update-goal.sh --source {goal.source} {goal.id} participants '["agent"]'`
  (also set `handoff_to` if a specific agent should pick it up). This is a
  JUDGMENT step and MUST live here at answer-time, NOT in a mechanical precheck
  sweep: only here is the authorization semantics in context -- a "no/keep
  parked" answer must leave the gate intact, which a participants-contains-user
  + pq-answered sweep could not distinguish (it would wrongly unpark declined
  work). Skip the drop whenever the answer declines, defers, or only partially
  authorizes. Canonical miss: g-309-18 sat pending+unclaimed 3 days after the
  gating pq was answered because the resolution left `participants:[agent,user]`
  intact.

### User Goal Reminders

- Run `load-aspirations-compact.sh` → IF path returned: Read it
  (compact data has IDs, titles, statuses, priorities, participants — no descriptions/verification)
  Filter for goals with `participants` containing `user`
- Mention relevant ones in context
- Occasionally: "There are {N} items waiting for your input — ask me about them anytime."

## Step 4c: Partner Context Refresh (G16)

Fires ONLY when the user's message references a partner agent, coordination,
board activity, or team state — OR when the assistant turn count since last
refresh exceeds a threshold (~10 turns). Simple Q&A turns skip this entirely.

```
1. Determine if refresh is needed (judgment — no script call):
   a. Does the user's message mention a partner agent by name, or reference
      coordination, board posts, team activity, partner status, or
      cross-agent work?
      → YES: fire refresh.
   b. ELSE: Read assistant_turn_count from working memory:
      Bash: bash core/scripts/wm-read.sh assistant_turn_count 2>/dev/null
      IF value is numeric AND value >= 10 since last partner refresh:
        → fire refresh.
      ELSE: SKIP entire Step 4c.

2. Team state probe:
   Bash: bash core/scripts/team-state-read.sh --field agent_status --json

3. Recent coordination board:
   Bash: bash core/scripts/board-read.sh --channel coordination --since 2h

4. Incorporate returned context into the response:
   - If partner status or board posts are relevant to the user's question,
     weave into the answer naturally.
   - If not directly relevant, hold in working context for Step 5 directive
     routing (partner-aware decisions).

Fail-open: if either probe errors, log and proceed. Partner context is
supplementary — never block the user-facing response on a refresh failure.
```

### Mode Gate (Directive & Learning Steps)
Bash: `session-mode-get.sh`
- If mode is `reader`: SKIP Steps 5, 6, 6.5, 7, 7.5 entirely (read-only mode, no directive processing or learning). RETURN after Step 4c (Step 4c's partner-context probes are read-only, so reader mode runs them).
- If mode is `assistant` or `autonomous`: PROCEED with Steps 5-7.5.
- **RUNNING state only**: After ALL steps complete (including 7.5), execute Step 3a.7 (loop re-entry). This is the LAST thing that happens — always.

## Step 5.0: Pre-Write Retrieval (G11 / R14)

Step 5 routes a directive into a state-changing write (Self update, new
aspiration, paused aspiration, remembered fact, recurring task, idea,
investigation, focus shift, etc.). Per
`.claude/rules/retrieve-before-deciding.md`, every consequential write is a
decision that should retrieve first. Step 4 already retrieved for the
user's question topic — when the directive is about the same subject,
reuse it. When the directive subject diverges, re-retrieve.

```
IF the user message contains NO directive intent (Q&A only):
    Skip — no write coming, no pre-write check needed.

ELSE:
    Compare the directive subject to Step 4's --category query:
      - SAME subject (e.g., user asked about X and now says "remember X is Y"):
          Reuse Step 4's tree_nodes / reasoning_bank / guardrails / beliefs
      - DIFFERENT subject (e.g., user asked weather, now says "remember I prefer Python"):
          Bash: retrieve.sh --category "{directive subject + best-fit category}" --depth shallow

    From the retrieved set, scan for:
      1. CONTRADICTIONS — existing rb/guard/tree-node/belief that opposes
         the proposed write. Examples:
           - user says "remember X is Y" but rb-NNN says X is Z
           - user says "stop learning about politics" but guard-NNN
             requires politics monitoring
      2. CONSTRAINTS — guardrails that limit this class of write right now
         (e.g., a guardrail forbidding rapid Self changes within N hours)
      3. DUPLICATES — recent similar writes that would create churn
         (same self.md change yesterday; same aspiration created last week)

    Surface findings to the user as ASSERTIONS, not questions — RUNNING state
    requires no-block-on-user-input (user-interaction.md). User is authoritative,
    so the directive proceeds; the surfaced finding lets the user retract
    in their NEXT message if they want.
      - On contradiction: "Recording X. Note: rb-NNN says the opposite —
        {rb summary one-liner}. Reply if you want the rb retired or this
        recording reverted." (then PROCEED with the directive)
      - On constraint: "Proceeding with {action}. guard-NNN ({rule one-liner})
        applies but is non-blocking in this case." (then PROCEED)
      - On duplicate: "Already recorded this {when}. Re-recording to refresh
        timestamp / overwriting prior wording." (then PROCEED with overwrite)
    In IDLE state, the same assertion form applies — questions add friction
    for no gain when the user already gave a directive.

    Diary breadcrumb:
      echo '{"entry_type":"observation","content":"Pre-write retrieval: rb=<N> guard=<M> for directive [{subject}]"}' | bash core/scripts/execution-diary.sh append

    Fail-open: if retrieve.sh errors, log and proceed to Step 5. A pre-write
    retrieval failure must NOT block the user-directed write — user is
    authoritative.
```

Carry the surfaced rb/guard IDs into Step 7.5's interaction-learning records
so the user's override (if any) becomes a first-class learning artifact.

## Step 5.0a: Self-Escalation Chokepoint (guard-33 enforcement)

Runs at the very start of directive processing, BEFORE Step 5 acts on anything
and BEFORE any pending-question is written. Catches the one directive class
that must NOT be self-authorized: a **self-escalating delegation** --- an action
that expands the agent's OWN authority AND reverses a prior resolved user
decision. The four `escalation_type`s (per
`world/conventions/self-escalation-confirmation.md`):

- **curriculum-graduation** --- promoting the agent's curriculum stage (e.g.,
  cur-02 -> cur-03), unlocking gated capabilities
- **capability-unlock** --- enabling a capability the agent was barred from
  (e.g., bidding before Stage 1 graduation, sending external outreach
  autonomously)
- **scope-expansion** --- widening the operating scope beyond what a prior
  resolved decision permitted
- **other** --- any other authority expansion that reverses a resolved decision

```
IF the directive/decision is NOT a self-escalation (the common case --- new
   aspiration, remember-fact, priority change, persona, idea, observation,
   ordinary Self-update governed by guard-380): SKIP this step -> Step 5.

IF it IS a self-escalation:
    # guard-33 anti-pattern: a pending-questions answer is agent-writable and
    # does NOT corroborate the escalation. Do NOT write a bare pending-question
    # to self-authorize, and do NOT perform the action now. Route through the
    # email round-trip instead.

    0. Domain-overlay precondition: the self-escalation transport
       (`world/scripts/self-escalation-register.sh` + the domain inbox-poll
       skill) is domain-supplied. If `world/scripts/self-escalation-register.sh`
       does NOT exist, guard-33 still holds --- do NOT self-authorize. Tell the
       user the action needs a confirmation this world cannot route
       automatically, file a participants:[agent, user] goal via
       aspirations-add-goal.sh, and STOP (skip steps 1-4).

    1. Register the escalation (mints the correlation-id, computes the 48h
       fail-closed timeout, appends an awaiting_reply record --- the store owner
       is the single writer):
         Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/self-escalation-register.sh" \
             --action "<human-readable action, e.g. graduate curriculum cur-02 -> cur-03>" \
             --escalation-type <curriculum-graduation|capability-unlock|scope-expansion|other> \
             --session "$MIND_SID"
       Parse the JSON: {correlation_id, outbound_subject, expires_at, action,
       body_correlation_line}.

    2. Email the user the decision-needed confirmation.
       (Check world/forged-skills.yaml for a skill whose triggers match
        "notify the user" and invoke it with:
          subject: <outbound_subject from step 1>
          message: a short body stating the action, its consequence, then
                   "Reply YES to approve or NO to deny.", and a FINAL line that
                   is exactly <body_correlation_line> from step 1 (the
                   redundant carrier the parser falls back to if the subject
                   token is edited).
        If no matching skill is registered, fall back to a
        participants:[agent, user] goal via aspirations-add-goal.sh. Never
        block on notification failure.)
       This notification send is an INTERNAL agent-to-principal message, NOT
       external outreach --- guard-12 / guard-29 per-send sign-off does NOT
       apply to it.

    3. Do NOT perform the escalating action this turn. It stays pending until
       the user replies. Resolution is automatic: the domain inbox-poll skill
       (the reply parser) runs on the next hourly drain and, when a reply carrying
       the correlation-id arrives from USER_EMAIL, sets the record to
       `confirmed` (YES) or `denied` (NO); 48h of silence -> `timed_out`
       (fail-closed DENY --- absence of approval is not approval, mirroring
       guard-12 / guard-29).

    4. In THIS response, tell the user you have emailed a confirmation request
       for the escalation and will proceed only on their YES.
```

**guard-33 invariant (MUST NOT weaken):** the escalating action proceeds ONLY
when its pending-escalation record reaches status `confirmed` --- which requires
an email-round-trip YES from USER_EMAIL, a tracked, non-agent-writable channel.
A pending-questions-only authorization NEVER suffices. On a later turn, before
acting on a previously-registered escalation, re-read its status from the store
(`pending_escalations.py get --cid <id>`) and act ONLY on `confirmed`. This
chokepoint is the positive mechanism that satisfies guard-33; the bare
pending-questions write it replaces was the anti-pattern guard-33 forbids.

## Step 5: Directive Detection & Routing

Applies in ALL states, persona on or off. When a user message contains a directive (not just a question/comment), detect the type and act:

**Origin-signal rule (applies to every goal-creation row below):** any goal
written to `aspirations-add-goal.sh` from this step MUST include
`"origin_signal": "user_directive"` in the JSON payload. The user's message
is the triggering signal. The origin-signal-gate
(`core/scripts/origin-signal-gate.py`) rejects goals without this field.
For `/create-aspiration from-user`, the skill itself sets the aspiration's
`origin_signal: user_directive` — child goals inherit via
`parent_aspiration:<asp-id>`.

| Directive Type | Example | Action |
|---------------|---------|--------|
| Self update | "Change your purpose to..." / "You're actually a..." | Edit `agents/<agent>/self.md` body. In the same Edit, update front matter: `last_updated: <today (YYYY-MM-DD)>` AND `last_update_trigger: user-correction` (both fields mandatory — mirror sites: aspirations-spark sq-012, felt-sense-checkin Material lane, encode-session Lane 7, all collapsed per world/conventions/self-program-evolution.md to call the same evolution-complete primitive). The Phase 2 PostToolUse hook captured the Edit as a stub in `world/self-evolution.jsonl`. Finalize via `bash core/scripts/evolution-complete.sh --revision-id <stub-rev> --reasoning "<≥80-char rationale quoting the user directive verbatim and citing it as the trigger>" --signal-source user-directive --signal-evidence '[{"type":"user_correction","id":"<turn-id-or-respond-step>","outcome":"applied"}]'`. The primitive auto-posts to decisions board AND triggers a confirmation email (cross-agent visibility surface). Confirm change to user in your response. |
| New aspiration | "Learn about cooking" | Invoke `/create-aspiration from-user` with the user's description |
| Remove/pause aspiration | "Stop learning about politics" | Pause the aspiration via `aspirations-update.sh <asp-id> status paused`, then mark each of its goals `skipped` via `aspirations-update-goal.sh <goal-id> status skipped` |
| Priority review | "Focus more on X than Y" / "show me priorities" / "reorder aspirations" / "asp-125 is most important" / response to priority-review pq | Invoke `/priority-review` with user's input. If a `type: priority-review` pending question exists with `status: pending`, pass its context to the skill. For simple single-aspiration priority changes (e.g., "make asp-125 HIGH"), update directly via `aspirations-update.sh <asp-id> priority <value>` (positional field-merge) without invoking the full skill. |
| Fresh-eyes reply | "Self is still right" / "Self should be X" / "We're working on wrong problems — focus Y" / any reply referencing the two meta-questions from an open `type: fresh-eyes-review` pq | Split the user's reply into Self-portion and portfolio-portion. **Self-portion**: if the user says Self is still right, no self.md change — just record the confirmation. If they give a correction ("Self should be X" / "Update purpose to Y"), treat it as a Self update directive (row above) — edit `agents/<agent>/self.md` with `last_update_trigger: fresh-eyes-review`, then finalize the Phase 2 stub via `bash core/scripts/evolution-complete.sh --revision-id <stub-rev> --reasoning "<≥80-char rationale quoting the user's fresh-eyes correction verbatim>" --signal-source fresh-eyes-review --signal-evidence '[{"type":"fresh_eyes_reply","id":"<pq-id>","outcome":"correction-applied"}]'`, confirm. **Portfolio-portion**: if the user gives any priority guidance ("focus on X", "deprioritize Y", "Z is most important"), route to the Priority review row above (invoke `/priority-review` with the fresh-eyes pq context). Then mark the fresh-eyes-* pq `status: answered`, append a `resolution:` field with the full reply text, and `Bash: session-signal-set.sh pq-resolved`. The fresh-eyes cadence gate (`fresh-eyes-cadence-check.sh`) reads `skip_if_pending`, so flipping this pq to answered immediately unblocks the next 25-goal cycle. If the reply is ambiguous (only vague praise or critique), surface a follow-up clarifier in this same response before marking answered. |
| Persona change | "Be more casual" | Update persona settings in `agents/<agent>/profile.yaml` |
| Remember fact/preference | "Remember I prefer Python" | PLACEMENT CHECK: route domain-specific stable values (endpoints, paths, service names, account IDs, brand names) to `world/conventions/*.md` per `.claude/rules/encode-stable-facts.md`. Route universal behavioral preferences to working memory or a guardrail. Only create or edit a `.claude/rules/*.md` file for universal behavioral rules that apply regardless of domain — never for a domain-specific operational fact. Then write to the chosen target (`/tree add`, `wm-set.sh domain_data`, or Edit on the appropriate convention file). NEVER use platform auto-memory. |
| Recurring task | "Check news every week" | Add as recurring goal to an appropriate aspiration |
| Skill creation request | "Make a skill for X" / "Create a skill" / "Forge a skill for Y" | Route through forge pipeline. Read `meta/skill-gaps.yaml`. If a gap matches the user's description, create goal: title `"Forge skill: {gap.procedure_name}"`, `skill: "/forge-skill"`, `args: "skill {gap-id}"`, priority MEDIUM, in best-fit aspiration via `aspirations-add-goal.sh`. If no matching gap exists, register a new gap in `meta/skill-gaps.yaml` (id: `gap-{next}`, status: `registered`, times_encountered: 1, procedure_name from user description, estimated_value: `medium`, `type`: `utility` or `analytical` — REQUIRED, g-115-3131: `type` gates the forge developmental bar, so an absent value hands that decision to a default rather than to you; per `core/config/skill-gaps.yaml` gap_types, utility = mechanizes an ALREADY-DERIVED procedure → CALIBRATE, analytical = the OUTPUT needs domain-mature judgment → EXPLOIT, the higher bar; unsure → `utility`, which IS the default, so stating it costs nothing), then create the forge goal targeting it. If user was generic ("make a skill" with no specifics), create goal with `skill: "/forge-skill"`, `args: "list"`. Forge-skill gates (curriculum, threshold, stage) apply at execution time — do NOT pre-check here. Confirm: "I'll queue a skill forge for {description}." In UNINITIALIZED state, acknowledge verbally only. |
| Idea/suggestion | "What if we...?" / "I had an idea..." | Create idea goal: title `"Idea: {user's suggestion}"`, priority MEDIUM, in best-fit aspiration via `aspirations-add-goal.sh` |
| Observation / problem report | "The processor is running on CPU" / "Logs show errors" / "This is really slow" / "X isn't working" | User observations are implicit investigation requests. Create goal: `"Investigate: {user's observation (50 chars)}"`, priority **HIGH**, in best-fit aspiration via `aspirations-add-goal.sh`. Dedup against existing goals first. Capture user's exact words in description. No confirmation needed — acknowledge and act. In UNINITIALIZED state (no world/), acknowledge verbally only. |
| Focus | "Focus on coding" / "explore more" / "save tokens" / "go back to normal" | Update `focus` in `agents/<agent>/profile.yaml` (null clears focus) |

### Processing Rules

1. Detect directive intent naturally from conversation (no special syntax needed)
2. Confirm what you're about to change before doing it: "I'll create a new aspiration to learn about cooking — sound right?"
3. Execute the state change using existing conventions (asp-NNN IDs, goal structure, etc.)
4. Confirm completion: "Done — added aspiration asp-003: Explore Cooking. I'll start working on this in my next loop cycle."
5. In RUNNING state: directive takes effect on next aspirations loop iteration
6. In IDLE state: state is updated, takes effect when user runs `/start`
7. In UNINITIALIZED state: do NOT write files (world/ and agent dir don't exist yet). Acknowledge conversationally: "Got it — once you run `/start`, I'll set that up." Process the directive immediately after `/start` creates world/ and agent dir.

## Step 5.5: Mid-Directive Drift Check (G15)

Assistant-mode analogue of autonomous Phase 4.05 (aspirations-execute). When
a directive turn has produced substantial output, the Step 4 retrieval snapshot
may be stale — re-retrieve and reconcile before continuing to Step 6.

SKIP entirely if this turn was simple Q&A (no directive routed in Step 5).

```
1. Count this turn's write volume:
   edit_count = number of Edit/Write/MultiEdit tool calls THIS turn
   bash_output_chars = total chars of Bash stdout THIS turn

   IF edit_count <= 3 AND bash_output_chars <= 3000:
     SKIP entire Step 5.5 — turn is lightweight, drift unlikely.

2. Re-retrieve at shallow depth, read-only:
   Bash: bash core/scripts/retrieve.sh --category "{current directive subject}" --depth shallow --read-only

3. Reconcile against Step 4's retrieval snapshot:
   - New reasoning_bank entries added since this turn started? (check `created` field)
   - New guardrails added that constrain the directive just executed?
   - Tree nodes touched since Step 4 ran? (check `last_updated`)

   IF any drift detected:
     Surface in response: "Note: new context arrived mid-turn — {one-line summary}."
     If drift contradicts what Step 5 just wrote, flag for the user rather than
     silently proceeding.

   ELSE:
     # No drift — proceed with Step 4's snapshot for downstream steps.
     pass

Fail-open: if retrieve.sh errors, log and proceed. The drift check must not
block the user-facing response or downstream learning steps.
```

## Step 6: Knowledge Freshness Check

After responding, assess: did the user just tell me something that corrects or
extends knowledge in my tree?

1. If the user provided a correction (e.g., "that's not how X works", fixing a
   misconception, providing updated information):
   a. Identify affected tree nodes via _tree.yaml scan
   a'. **Broad re-retrieve (G12 / R15)** — a `_tree.yaml` scan only finds
       affected TREE NODES; reasoning-bank entries, guardrails, beliefs, and
       pattern signatures that depend on the now-falsified knowledge are
       invisible to that scan. Sister mechanism to G3 surprise→broad-retrieve
       (review-hypotheses Step 3.5):
         Bash: retrieve.sh --category "{correction topic}" --depth deep
       From the returned JSON, identify entries that depend on or restate
       the corrected belief. Append IDs to a reconciliation_candidates set:
         { tree_nodes: [...], reasoning_bank: [rb-NNN, ...],
           guardrails: [guard-NNN, ...], beliefs: [bel-NNN, ...],
           pattern_signatures: [sig-NNN, ...] }
       Pass this set into steps b-f below so the update covers ALL stores,
       not just the tree nodes the scan found.
       Fail-open: retrieve.sh error → empty reconciliation_candidates, proceed
       with tree-only update. User correction must not block on retrieval.
   b. Read those nodes
   c. Update immediately — user corrections are authoritative
   d. Set last_update_trigger: {type: "user-correction", session: N}
   e. Log: "KNOWLEDGE UPDATE (user correction): {node_key} — {summary}"
   e'. For each rb-NNN / guard-NNN / bel-NNN / sig-NNN in
       reconciliation_candidates whose content directly opposes the
       correction, mark for review:
         - rb-NNN: `bash core/scripts/reasoning-bank-update-field.sh <id> status retired`
         - guard-NNN: `bash core/scripts/guardrails-update-field.sh <id> status retired`
         - bel-NNN: Edit `world/knowledge/beliefs.yaml` directly — set the
           belief's `status: weakened` (or `retired` if strongly contradicted)
           and append a note referencing the user correction's experience id.
           No bash wrapper exists; beliefs are managed by direct YAML edits.
         - sig-NNN: re-evaluate trigger fit; retire if no longer valid via
           `bash core/scripts/pattern-signatures-set-status.sh <id> retired`
       The agent uses judgment — not every candidate IS contradicted; review
       individually. Log retirements: "RETIRED via user-correction: {id}".
   f. Archive user correction as experience:
      experience_id = "exp-user-correction-{node_key}-{date}"
      Write agents/<agent>/experience/{experience_id}.md with:
          - Exact user statement (verbatim)
          - What was corrected (prior belief/knowledge)
          - Which tree nodes were updated
          - Impact assessment
      echo '<experience-json>' | bash core/scripts/experience-add.sh
      Experience JSON:
          id: "{experience_id}"
          type: "user_correction"
          created: "{ISO timestamp}"
          category: "{node category}"
          summary: "User corrected: {brief description}"
          tree_nodes_related: ["{affected node keys}"]
          verbatim_anchors:
              - key: "user-statement"
                content: "{exact user message}"
              - key: "prior-belief"
                content: "{what the agent previously believed}"
          content_path: "agents/<agent>/experience/{experience_id}.md"

2. If the user provided new information that extends but doesn't contradict:
   a. Append insight to relevant node
   b. Update front matter: last_update_trigger
   # T21 PostToolUse hook auto-syncs last_updated to _tree.yaml on the Edit.

3. If broader implications exist (other nodes may also be affected):
   echo '<debt-json>' | wm-append.sh knowledge_debt  # for those nodes

This step is lightweight — skip if the user message was a simple question,
command, or directive with no knowledge-bearing content.

## Step 6.5: Post-Edit Tree Reconciliation (E2)

Rationale (WHY Step 6.5 exists separately from Step 6, and the missing-without-this-step failure mode): `core/config/rationale/respond.md`

```
1. Self-report: did THIS TURN involve any Edit/Write/MultiEdit tool calls
   that landed on disk (NOT failed)? Use in-conversation memory — this is
   judgment, not a script flag.

   IF no edits this turn: SKIP entire Step 6.5.
   IF Step 6 already updated all affected tree nodes: SKIP entire Step 6.5.

2. Collect the set of edited file paths. Filter out paths that don't need
   tree reconciliation:

   SKIP file (no debt needed) if path matches any of:
   - world/knowledge/tree/** — the tree itself; T21 PostToolUse hook
     already handles its own mtime sync, and Lane 1.1 of /encode-session
     covers content updates
   - world/board/** — message board, not knowledge
   - world/changelog.jsonl, world/.history/** — audit trails, not docs
   - <agent>/** — ALL agent-private state (self.md, profile.yaml,
     curriculum.yaml, aspirations.jsonl, experience, journal, session,
     reports, etc.). Tree nodes describe SHARED/world things, not
     per-agent state — filing debt for an agent's own self.md edit is
     spurious. DO NOT add carve-outs here (e.g., "but self.md should
     trigger reconciliation"): if a Self edit needs tree-side reflection,
     that's Step 5's job via the user directive that drove it, not a
     post-edit scan.
   - **/*.tmp, **/*.log, **/*.cache, **/*.lock — ephemeral

   FLAG file (probably tree-documented) if path matches any of:
   - core/scripts/** — tree typically documents script behavior
   - core/config/** — tree typically documents config schemas
   - .claude/skills/** — skill semantics often in tree
   - .claude/rules/** — rule effects often in tree
   - world/conventions/** — conventions referenced from tree
   - world/scripts/** — domain scripts often documented in tree
   - Any product workspace path from agents/<agent>/self.md (e.g.,
     `AGENT_WRITE_PATH` or any domain-specific code-path env var the
     deployment defines)

   For unclassified paths: agent judges based on whether the file is
   plausibly described anywhere in the tree.

   IF no flagged files remain: SKIP entire Step 6.5.

3. For each flagged file, file ONE debt entry. Do NOT try to identify
   specific tree nodes here — that's the consumer's job (Lane 1.6 or
   Phase 4.5). This step is a cheap signal-emitter, not a scanner.

   echo '<JSON>' | bash core/scripts/wm-append.sh knowledge_debt

   JSON shape (node_key=null + reason= form passes the wm-append schema
   gate per wm.py:286):
   {
     "node_key": null,
     "reason": "post-edit reconciliation: <file path>",
     "source_goal": "respond-step-6.5",
     "priority": "MEDIUM",
     "created": "<today ISO>",
     "edited_path": "<exact path>",
     "edit_summary": "<one-line description of what changed>"
   }

   Why node_key=null: a single edited file may map to 0, 1, or N tree
   nodes. Determining the mapping requires `grep -l <path>
   world/knowledge/tree/` — expensive enough that we defer it to the
   consumer. The consumer (Lane 1.6 inline-resolution branch or Phase 4.5
   in autonomous) runs the scan when it has the time budget.

4. Log to execution diary:
   echo '{"entry_type":"observation","content":"Step 6.5 post-edit debt filed for <N> flagged paths"}' | bash core/scripts/execution-diary.sh append

   Fail-open: wm-append errors log to diary; never block the user-facing
   response on a debt-filing failure.
```

Rationale (WHY Step 6.5 runs after Step 6 and WHY it is not part of Step 5): `core/config/rationale/respond.md`

**Cross-reference**: `core/config/conventions/encoding-triggers.md` E2 row.

## Step 7: Discovery Check (RUNNING Only)

SKIP if: agent state is not RUNNING.

After responding and processing directives, evaluate: did this interaction
reveal something worth acting on that is NOT already captured by Step 5
directives or existing goals?

This captures **agent-initiated** discoveries — things the agent noticed while
searching knowledge (Step 4), checking system state, or formulating the response.
User-reported observations are already handled by Step 5's directive table.

1. Quick assessment (mental pass — no script calls):
   - Did I notice something anomalous, unexpected, or broken?
   - Did this interaction spark an improvement idea?
   - If NEITHER: skip. Most responses produce no agent discoveries.

2. Dedup: `Bash: load-aspirations-compact.sh` → IF path returned: Read it
   (compact data has IDs, titles, statuses — no descriptions/verification)
   Check goal titles for semantic overlap with proposed discovery.
   IF a similar goal already exists: skip creation.

3. Create goal(s) — max 2 per response, in best-fit aspiration:
   `echo '<JSON>' | bash core/scripts/aspirations-add-goal.sh <aspiration_id>`

   Every goal JSON below MUST include `"origin_signal": "user_directive"` — the
   user's message is the triggering signal, and the origin-signal-gate
   (core/scripts/origin-signal-gate.py) rejects goals without this field.

   Investigation format:
   ```json
   {
       "title": "Investigate: {observation (50 chars)}",
       "status": "pending",
       "priority": "MEDIUM",
       "skill": null,
       "participants": ["agent"],
       "category": "{relevant category}",
       "description": "Noticed during user response: {observation}\n\nContext: {what retrieval or reasoning revealed}",
       "verification": { "outcomes": ["Root cause understood and documented"], "checks": [] },
       "blocked_by": [],
       "origin_signal": "user_directive"
   }
   ```

   Idea format:
   ```json
   {
       "title": "Idea: {creative insight (50 chars)}",
       "status": "pending",
       "priority": "MEDIUM",
       "skill": null,
       "participants": ["agent"],
       "category": "{relevant category}",
       "description": "Idea from user response: {full description}\n\nExpected benefit: {why this matters}",
       "verification": { "outcomes": ["Idea evaluated — implemented, formed hypothesis, or retired"], "checks": [] },
       "blocked_by": [],
       "origin_signal": "user_directive"
   }
   ```

4. Log: `"DISCOVERY: Created {goal_id}: {title} in {asp_id}"`

This step is fire-and-forget — no experience archival, no spark check.
The goal enters the aspirations queue and gets full treatment when executed.

## Step 7.5: Interaction Learning (All Initialized States)

SKIP if: agent state is UNINITIALIZED (no world/ directory).
SKIP if: persona is false (standard Claude assistant mode, no learning).

After completing the response, directives, knowledge freshness, and discovery checks,
evaluate whether this interaction warrants persistent learning artifacts.

### Notability Assessment

Quick assessment of the user's message — same pattern as Step 7's mental pass.
The agent uses its judgment to answer four questions:

```
1. Did the user share DOMAIN KNOWLEDGE worth preserving?
   (Causal reasoning, architectural insight, "how things actually work",
   lessons from experience, explanations of WHY — not just questions or commands)
   → If yes: note as INSIGHT

2. Did the user give FEEDBACK on how the agent should operate?
   (Process corrections, behavioral guidance, "do this differently",
   approval/criticism of approach — distinct from Step 6 FACT corrections)
   → If yes: note as FEEDBACK

3. Did the user express a TESTABLE BELIEF or PREDICTION?
   (Uncertain claims about system behavior, theories about why something
   happens, predictions about what will happen — something the agent
   could later confirm or correct)
   → If yes: note as HYPOTHESIS

4. Did the user share an OPERATIONAL LESSON or warning?
   (Error patterns, environment quirks, "gotcha" knowledge, debugging tips,
   "always do X when Y", "watch out for Z" — procedural friction knowledge
   that causes repeated pain across sessions)
   → If yes: note as OPS_GOTCHA

IF none apply: RETURN — most interactions are simple Q&A. No script calls needed.
```

The agent's natural language understanding determines notability — not keyword matching.
User messages are freeform; rigid keyword gates would miss novel phrasings and
subtle insights. Trust the model to distinguish "the way X works is Y" (insight)
from "how does X work?" (question).

### Sub-step 7.5a: Reasoning Bank / Guardrail Creation

**From INSIGHT → consider reasoning bank entry:**

1. Determine relevant category from conversation topic
2. Dedup: `Bash: reasoning-bank-read.sh --category {category}` — scan for semantic overlap
3. If existing entry covers it: `Bash: reasoning-bank-increment.sh {entry.id} utilization.times_helpful` → done
4. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
   ```
   JSON structure (`id` and `created` are auto-set by the script — omit
   both; capture the assigned id from stdout, which prints the full
   record JSON):
   ```json
   {
       "title": "User insight: {brief title}",
       "type": "user_provided",
       "category": "{relevant category}",
       "content": "{user's insight with agent interpretation}",
       "applies_to": "<any|framework|domain|specific>",
       "when_to_use": {
           "conditions": ["{when this applies}"],
           "category": "{category}"
       },
       "tags": ["user-provided"]
   }
   ```
   `applies_to`: pick by content scope — `any` for cross-cutting methodology,
   `framework` for lessons about THIS framework's skills/scripts/gates, `domain`
   for this agent's deployment-domain specifics (services, products, workflows
   it interacts with), `specific` for single-incident.

**From FEEDBACK → consider guardrail:**

1. Dedup: `Bash: guardrails-read.sh --category {category}` — scan for semantic overlap
2. If existing guardrail covers it: `Bash: guardrails-increment.sh {guard.id} utilization.times_active` → done
3. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/guardrails-add.sh
   ```
   JSON structure (`id` and `created` are auto-set by the script — omit
   both; capture the assigned id from stdout):
   ```json
   {
       "rule": "{what to do or avoid, derived from user's guidance}",
       "category": "{relevant category}",
       "trigger_condition": "{when this guardrail applies}",
       "source": "user-interaction"
   }
   ```

**From OPS_GOTCHA → reasoning bank entry (and optionally guardrail):**

Operational gotchas are procedural friction knowledge that causes repeated pain
across sessions (error patterns, environment quirks, debugging lessons). These
get MANDATORY encoding — the whole point is to stop re-discovering them.

1. Determine relevant category from conversation topic
2. Dedup: `Bash: reasoning-bank-read.sh --category {category}` — scan for semantic overlap
3. If existing entry covers it: `Bash: reasoning-bank-increment.sh {entry.id} utilization.times_helpful` → done
4. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
   ```
   JSON structure (`id` and `created` are auto-set; capture the assigned
   id from stdout):
   ```json
   {
       "title": "Gotcha: {brief title}",
       "type": "user_provided",
       "category": "{relevant category}",
       "content": "{user's operational lesson with context and resolution}",
       "applies_to": "<any|framework|domain|specific>",
       "when_to_use": {
           "conditions": ["{error pattern, symptom, or trigger scenario}"],
           "category": "{category}"
       },
       "tags": ["user-provided", "ops-gotcha"]
   }
   ```
   `applies_to`: ops gotchas about external services/domain infra → `domain`;
   framework-internal gotchas → `framework`; cross-cutting → `any`.
5. IF the gotcha is prescriptive ("always do X" / "never do Y") → ALSO create a guardrail:
   ```
   echo '<JSON>' | bash core/scripts/guardrails-add.sh
   ```
   ```json
   {
       "rule": "{the prescriptive rule from the user's gotcha}",
       "category": "{relevant category}",
       "trigger_condition": "{when this gotcha applies}",
       "source": "user-interaction",
       "tags": ["ops-gotcha"]
   }
   ```

Not every insight needs a reasoning bank entry and not every piece of feedback needs
a guardrail. Use judgment — create artifacts for things that are **reusable across
future interactions**, not one-off clarifications. OPS_GOTCHA is the exception:
always encode these — the user shared them specifically because they keep getting lost.

### Sub-step 7.5b: Hypothesis Formation

**From HYPOTHESIS → consider pipeline record:**

1. Dedup: `Bash: pipeline-read.sh --stage active` and `Bash: pipeline-read.sh --stage discovered`
2. If existing hypothesis covers it → skip
3. Otherwise create:
   ```
   echo '<JSON>' | bash core/scripts/pipeline-add.sh
   ```
   JSON structure:
   ```json
   {
       "id": "{today}_{slug-from-prediction}",
       "title": "{user's prediction (80 chars max)}",
       "stage": "discovered",
       "horizon": "{short|long — based on when testable}",
       "type": "{high-conviction|exploration}",
       "confidence": 0.6,
       "position": "{user's stated belief}",
       "formed_date": "{today}",
       "category": "{relevant category}",
       "rationale": "User shared this prediction during interaction: {user's words}"
   }
   ```
   - Type: `high-conviction` if user was certain, `exploration` if uncertain
4. If RUNNING and a best-fit aspiration exists: also create evaluation goal via
   `echo '<JSON>' | bash core/scripts/aspirations-add-goal.sh <asp-id>` with
   `skill: "/review-hypotheses --hypothesis {hypothesis_id}"`

Only form hypotheses from claims that are genuinely testable — "I think X causes Y"
qualifies, but "I think we should do Z" is a directive (Step 5), not a hypothesis.

### Sub-step 7.5c: Experience Archival

If any artifacts were **created** (not just strengthened) → archive the interaction:

1. Write content to `agents/<agent>/experience/exp-interaction-{date}-{slug}.md`:
   ```markdown
   ---
   type: user_interaction
   date: {today}
   agent_state: {RUNNING|IDLE}
   ---
   # User Interaction: {brief topic}

   ## User Message
   {relevant user message text — the parts that triggered learning}

   ## Agent Response Summary
   {1-2 sentence summary of what the agent responded}

   ## Artifacts Created
   {list: rb-NNN, guard-NNN, hypothesis ID, or "strengthened existing {id}"}
   ```

2. Index: `echo '<JSON>' | bash core/scripts/experience-add.sh`
   ```json
   {
       "id": "exp-interaction-{date}-{slug}",
       "type": "user_interaction",
       "created": "{today}",
       "category": "{relevant category}",
       "summary": "Interaction learning: {what was learned}",
       "content_path": "agents/<agent>/experience/exp-interaction-{date}-{slug}.md",
       "tree_nodes_related": ["{nodes consulted in Step 4}"],
       "verbatim_anchors": [{"key": "user-statement", "content": "{user's key statement}"}]
   }
   ```

Skip archival for interactions that only strengthened existing artifacts — that's
routine, not notable enough for a full experience record.

### Sub-step 7.5d: Journal Entry

If any learning occurred (artifacts created OR strengthened):

Append to journal via `echo '<JSON>' | bash core/scripts/journal-add.sh` or
`bash core/scripts/journal-merge.sh` if session entry exists:

```
## {timestamp} — Interaction Learning
Topic: {conversation topic}
Artifacts: {list of created/strengthened artifact IDs}
```

### Sub-step 7.5e: Tool-Result Surprise (E3)

Step 7.5's other sub-steps inspect USER messages. This sub-step inspects
TOOL RESULTS from this turn — Bash, Read, Grep outputs that contradicted
what the tree or prior expectation said. Without this lane, drift surfaces
in conversation memory and dies at session end (same shape as Phase 4.5
probe-outcome detection in autonomous mode, just chat-mode-side).

SKIP if: mode == "reader" (no writes allowed).
SKIP if: no Bash/Read/Grep tool calls fired in this turn.

```
1. Scan this turn's tool outputs. For each Bash/Read/Grep result, ask:
   - Did the result contradict a specific tree-documented value
     (a port, URL, schema field, file path, count, or fact)?
   - Did the result reveal a field the tree should describe but doesn't?

   IF no contradiction noticed: SKIP rest of Sub-step 7.5e.

2. For each noticed contradiction, score surprise per the same rubric
   as Phase 4.5 Probe-Outcome Surprise (E7) so the two surfaces use
   one ruler:
     - port/URL mismatch       → surprise = 8
     - schema mismatch         → surprise = 7
     - missing field           → surprise = 6
     - silent empty / no-data  → surprise = 5
     - other observable drift  → surprise = 5

3. Filter: surprise < 6 → SKIP this finding (below the noise floor;
   not worth dual-write overhead).

4. For each surviving finding, dedup against Step 7.5a-d:
   - If an INSIGHT rb entry was just created on the same topic: SKIP
     (double-write avoided — 7.5a already captured the learning)
   - If Step 6.5 already filed a knowledge_debt entry for the same
     file path: SKIP (post-edit reconciliation covers the same node)

5. For each surviving finding, dual-write:
   a. knowledge_debt entry (consumer: /encode-session Lane 1.6 OR
      autonomous Phase 4.5):
      echo '<JSON>' | bash core/scripts/wm-append.sh knowledge_debt
      JSON shape:
        {
          "node_key": "<tree node key if identified, else null>",
          "reason": "tool-result-surprise: <topic> — observed <X>, tree said <Y>",
          "source_goal": "respond-step-7.5e",
          "priority": "HIGH" if surprise >= 7 else "MEDIUM",
          "created": "<today ISO>"
        }
   b. sensory_buffer entry (consumer: consolidation replay queue):
      echo '<JSON>' | bash core/scripts/wm-append.sh sensory_buffer
      JSON shape (same encoding_observation schema as E7):
        {
          "source_goal": "respond-step-7.5e",
          "observation": "<one-paragraph description of the drift>",
          "encoding_score": 0.0,
          "scores": {
            "novelty": 0.6,
            "outcome_impact": 0.5,
            "surprise": <surprise/10.0>,
            "goal_relevance": 0.5,
            "repetition_strength": 0.1
          },
          "target_article": "<node key if identified, else null>",
          "replay_priority": "high_surprise"
        }

6. Bash: echo '{"entry_type":"observation","content":"Step 7.5e tool-result-surprise: <N> findings dual-written"}' | bash core/scripts/execution-diary.sh append
```

Fail-open: append errors log to execution-diary but never block the response.

### Sub-step 7.5f: Agent-Produced Review Findings (E6)

When the user asks for a review, audit, or critique, the agent's response
often contains multiple findings — each one a candidate for reasoning bank
or guardrail encoding. Without this sub-step, findings vanish unless the
user remembers to invoke `/encode-session` next. Sub-step 7.5a covers
user-shared insights, not agent-produced findings.

SKIP if: mode == "reader" (no writes allowed).

```
1. Heuristic detection: is THIS response a review / audit / findings list?
   Signals (any one is enough):
     a. User's message contained any of:
        review, audit, check, inspect, critique, fresh-eyes, code review,
        look over, go over, examine
     b. Agent's response contains 3+ separated finding-style paragraphs
        — e.g., "Finding 1:", "Issue:", "Bug:", "Observation:",
        bullet-list headings with "**Bug**", "**Issue**", "**Drift**"
     c. Agent's response cites 3+ file paths with line numbers as evidence
        (`path:line` shape)

   IF NO signal matches: SKIP rest of Sub-step 7.5f.

2. Extract findings (max 5 per response — beyond that, the encoding
   value-per-write drops; the user can re-ask for specifics).
   For each finding:
     a. Classify into ONE of: bug | drift | code-smell | architecture | doc-gap
        (single-label, pick the most specific)
     b. Identify the relevant category from the file path (e.g.,
        `core/scripts/foo.sh` → infrastructure-architecture;
        `.claude/skills/foo/` → framework-skills; etc.)

3. For each finding, dedup-then-write to reasoning bank:
   a. Bash: bash core/scripts/reasoning-bank-read.sh --category <category>
   b. Scan returned entries for semantic overlap with this finding
   c. IF an existing entry covers it:
      Bash: bash core/scripts/reasoning-bank-increment.sh <entry.id> utilization.times_helpful
   d. ELSE create:
      echo '<JSON>' | bash core/scripts/reasoning-bank-add.sh
      JSON shape (`id` and `created` auto-set):
        {
          "title": "Review finding: <one-line summary>",
          "type": "failure",                   // most review findings document a problem
          "category": "<category>",
          "content": "<finding body with file path + line ref if any>",
          "applies_to": "framework" if path under .claude/|core/, else "domain",
          "when_to_use": {
            "conditions": ["when modifying <file or pattern from finding>"],
            "category": "<category>"
          },
          "tags": ["agent-review", "user-requested", "<classification>"]
        }

4. For findings classified as PRESCRIPTIVE ("always check X before Y",
   "never call Z without W"), ALSO file as a guardrail:
   a. Dedup: bash core/scripts/guardrails-read.sh --category <category>
   b. IF covered: bash core/scripts/guardrails-increment.sh <guard.id> utilization.times_active
   c. ELSE create via guardrails-add.sh (same JSON shape as 7.5a feedback path)

5. For each unique file path mentioned across findings, file ONE
   knowledge_debt entry so the post-edit reconciliation lane (E2)
   fires the next time anyone edits that file:
   echo '<JSON>' | bash core/scripts/wm-append.sh knowledge_debt
   JSON shape:
     {
       "node_key": null,
       "reason": "review-finding: <file_path> — see rb-<id> from step 7.5f",
       "source_goal": "respond-step-7.5f",
       "priority": "MEDIUM",
       "created": "<today ISO>"
     }

6. Bash: echo '{"entry_type":"observation","content":"Step 7.5f review-findings: <N> rb entries + <M> guardrails + <P> debt entries from <K> findings"}' | bash core/scripts/execution-diary.sh append
```

Fail-open: append errors log to execution-diary but never block the response.

Note: this fires AFTER 7.5a/b/c/d. If 7.5a already created a rb entry for
the SAME finding (user shared an insight that the agent then expanded into
a review), 7.5f's dedup step skips the duplicate.

### Ownership Boundaries

- **Step 4.5** (respond, this skill) owns tier-escalation encoding debt — Tier-2/Tier-3 retrieval = tree gap signal → `knowledge_debt` entry (E1)
- **Step 6** (respond) owns fact corrections to knowledge tree nodes (user says "X is actually Y")
- **Step 6.5** (respond, this skill) owns post-edit reconciliation — agent-performed Edit/Write may invalidate tree nodes → `knowledge_debt` entry (E2)
- **Phase 6.5** (aspirations-spark, different skill) owns goal-execution-derived guardrails and reasoning bank
- **Step 7.5** (respond) owns user-interaction-derived guardrails, reasoning bank, hypotheses, and experiences
- **Step 7.5e** (respond, this skill) owns tool-result-surprise dual-writes — assistant-mode chat-side analog of Phase 4.5 Probe-Outcome Surprise (E7), filters out anything already covered by 7.5a-d or Step 6.5 (E3)
- **Step 7.5f** (respond, this skill) owns agent-produced review-finding encoding — when the agent itself produces a multi-finding review/audit, each finding routes to rb (+ guardrail if prescriptive) + a per-file knowledge_debt entry (E6); dedup against 7.5a
- No overlap: if Step 6 already handled a user correction for a given node, Step 6.5 does not re-flag the same file; if Step 6 already handled the topic, Step 7.5 does not re-process it; if 7.5a-d created an artifact for a topic, 7.5e does not dual-write the same topic; if 7.5a covered a finding, 7.5f skips it

## Step 7.6: Mid-Session Cadence Nudge (E4)

Rationale (WHY Step 7.6 uses a cadence nudge rather than forced encoding, and the failure mode without it): `core/config/rationale/respond.md`

### Mode and state gate

```
Bash: session-mode-get.sh → MODE
Bash: session-state-get.sh → STATE

IF MODE == "reader": SKIP entire Step 7.6 (no writes allowed).
IF MODE == "autonomous" AND STATE == "RUNNING":
  SKIP — the loop already encodes per Phase 8 / consolidation; the user
  doesn't need a nudge. (An observer session — assistant mode alongside a
  RUNNING runner — DOES get the nudge: it can invoke /encode-session
  even while the loop is running, though concurrency caveats apply per
  /encode-session's Concurrency Note.)
```

### Counter increment

```
1. Decide if this turn was "substantive":
   substantive = ANY of the following fired this turn:
     - Step 5 routed a directive
     - Step 6 reconciled a user correction
     - Step 6.5 filed a post-edit debt entry
     - Step 7 created a discovery goal
     - Step 7.5 created/strengthened an rb/guard/hypothesis/experience
     - Step 7.5e dual-wrote a tool-result-surprise (E3)
     - Step 7.5f encoded agent-produced review findings (E6)
     - Step 4 had to escalate to Tier 2 or Tier 3 (Step 4.5 fired)

   IF NOT substantive: SKIP rest of Step 7.6. (Pure Q&A turns don't count.)

2. Read current counter:
   Bash: bash core/scripts/wm-read.sh assistant_turn_count 2>/dev/null

   IF null/empty: current = 0
   ELSE: current = int(returned value)

3. Increment + persist:
   new_count = current + 1
   echo "$new_count" | bash core/scripts/wm-set.sh assistant_turn_count
```

### Cadence check + nudge

```
4. Cadence = 10 substantive turns (tunable — see "Tuning" below).
   IF new_count % 10 != 0: SKIP rest. (Counter is below the cadence trip.)

5. Compute encoding-debt summary for the nudge body:
   Bash: bash core/scripts/wm-read.sh knowledge_debt 2>/dev/null
   debt_count = length of returned array (0 if null/empty)

6. Surface the nudge as the LAST paragraph of the response, before the
   terminal Bash call. One-liner — do not derail the response. Form:

   "▸ {new_count} substantive turns this session. {debt_summary}.
    Consider `/encode-session` to flush in-flight learning to the tree."

   debt_summary cases:
   - debt_count == 0:  "No knowledge_debt queued"
   - debt_count == 1:  "1 knowledge_debt entry queued"
   - debt_count >= 2:  f"{debt_count} knowledge_debt entries queued"

7. Diary breadcrumb:
   echo '{"entry_type":"observation","content":"Step 7.6 cadence nudge surfaced at turn {new_count} with {debt_count} debt entries"}' | bash core/scripts/execution-diary.sh append

8. Counter reset on /encode-session: when the user invokes /encode-session,
   that skill's Phase Final clears assistant_turn_count back to 0. See the
   /encode-session SKILL.md Phase Final section — counter reset is the
   tail-end of the encoding pass so the next cadence starts fresh.
```

### Tuning

The cadence value (default 10) lives inline. To change it, edit step 4
above. A future refinement: read from `core/config/aspirations.yaml`
(creating an `assistant_cadence` section if needed) so the value can be
tuned without editing this skill. Not done yet — premature config
plumbing.

**Cross-reference**: `core/config/conventions/encoding-triggers.md` E4 row.

## Persona Configuration Reference

- Persona framework defaults defined in `core/config/profile.yaml` under `persona:`
- Live persona state in `agents/<agent>/profile.yaml` under `persona:` (seeded from config by `/boot`)
- Tone options: `direct`, `friendly`, `formal`, `casual`
- Verbosity options: `terse`, `concise`, `detailed`, `thorough`
- `personality_notes`: free-form string for additional persona guidance (empty by default)
- `use_knowledge_in_chat`: whether to call retrieve.sh for conversational responses
- `surface_pending_questions`: whether to weave pending questions into responses
- `surface_user_goals`: whether to mention user-participant goals
- Persona state: `session-persona-get.sh` returns `true`, `false`, or `unset` (default `true`)
- Only `/start`, `/stop`, and `/boot` may set persona via `session-persona-set.sh`
- User directives like "be more casual" update `agents/<agent>/profile.yaml` persona fields directly

## Pending Questions Queue Format

When the agent needs user input during autonomous operation, it writes to `agents/<agent>/session/pending-questions.yaml` instead of blocking:

```yaml
questions:
  - id: pq-001
    date: "YYYY-MM-DD"
    context: "goal or skill context"
    question: "What the agent needs to know"
    default_action: "What the agent will do autonomously"
    status: pending  # pending | answered | superseded
    answer: null
Bash: echo "respond phase documented"
```

- **pending**: awaiting user response (agent proceeds with default_action)
- **answered**: user provided an answer (agent incorporates on next loop iteration)
- **superseded**: agent resolved the situation before user answered
- ID format: `pq-NNN` (zero-padded 3-digit, monotonically increasing)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
When /respond is invoked mid-loop (autonomous mode), the final action MUST be a Bash
call, not the chat reply itself. Minimum terminal call: `Bash: echo "respond complete"`.
