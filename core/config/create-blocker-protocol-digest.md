# CREATE_BLOCKER Protocol — Single Source of Truth

Extracted from `.claude/skills/aspirations-execute/SKILL.md`. Loaded on-demand via
`load-create-blocker-protocol.sh`. This is the canonical path for blocker
creation — invoked by Phase 4.0 (fast-path SKIP), Phase 4.1e (unfixable
infrastructure failure), and Phase 0.5b (pre-selection sweep).

The protocol enforces structural correctness (blocker-create-gate.py) and
participant correctness (capability-gate.py) with explicit gate branches.
Skipping either gate has produced false-positive blockers that put the agent
to sleep on non-problems — the gates are non-negotiable.

Cross-references:
- `.claude/rules/capability-before-user.md` — capability scan rationale
- `.claude/rules/verify-before-assuming.md` — multi-signal + statistical rules
- `.claude/rules/probe-with-canonical-code-path.md` — canonical probe rule
- `core/config/conventions/negative-conclusions.md` — recording protocol
- `rb-226`, `rb-246`, `rb-258`, `rb-245` — reasoning bank lineage

## Protocol

```
CREATE_BLOCKER(failure_skill, failure_reason, goal, aspiration_id, diagnostic_context):

  1. Bash: wm-read.sh known_blockers --json
  2. Check for existing blocker with same skill + null resolution
     IF existing:
       Append goal.id to existing.affected_goals
       Update diagnostic_context with new info
       (unblocking goal already exists — no new goal)
     ELSE:
       2.5. CAPABILITY SCAN (mandatory — see .claude/rules/capability-before-user.md):
            a. Check .claude/skills/ for skill matching failure_skill
            b. Check world/forged-skills.yaml for matching triggers
            c. Bash: world-cat.sh conventions/capability-routing.md
               → check agent-provisionable services list
            d. Apply decision rule from the capability-before-user rule:
               - If ANY check finds agent-capable path → [agent]
               - If needs BOTH agent and human → [agent, user]
               - ONLY if genuinely human-only → [user]

       2.55. BLOCKER-CREATE GATE (mandatory — structural correctness check):
            Run blocker-create-gate.py BEFORE the capability gate. The blocker
            gate catches the four structural failure modes that have repeatedly
            produced false-positive blockers:
              (1) non-canonical probe (synthetic ssh/curl vs. companion_script)
              (2) single-signal negation (verify-before-assuming rule 1)
              (3) statistical negation without schema probe (rb-245 / rb-258)
              (4) infrastructure blocker without infra-health-check evidence

            Build a blocker_json payload with the fields the gate expects:
              {
                "type": "infrastructure" | "resource" | "user_action" | ...,
                "affected_skills": [<failure_skill>, ...],
                "failure_reason": "<short reason>",
                "evidence": [
                  {"tool":"...","command":"<exact cmd>","output":"...",
                   "evidence_type":"command_exit|http_status|..."},
                  ... (need ≥2 independent entries)
                ],
                "schema_probe_evidence": {<one-record schema verification>},  # if statistical
                "infra_health_check": {<infra-health.sh output>}               # if infrastructure
              }

            Invoke:
              echo '<blocker_json>' | bash core/scripts/blocker-create-gate.sh \
                --probe-command "<exact command used to diagnose>" \
                [--override-blocker-gate "<justification>"] \
                --output json

            Branch on exit code:
              - Exit 0 → structural checks passed. Proceed to Step 2.6.
              - Exit 1 → gate blocks. Read the JSON `reason` and `checks[]` fields, then:
                  (a) If the probe used was synthetic: retry with the skill's canonical
                      companion_script (see SKILL.md `companion_scripts:` front matter)
                      and re-run the gate with the new probe evidence.
                  (b) If only one signal exists: run an independent second probe
                      (different tool, endpoint, or evidence_type) and re-submit.
                  (c) If the failure_reason is a statistical claim: run the schema
                      probe (read ONE live record, verify the claimed field exists),
                      populate `schema_probe_evidence`, and re-submit.
                  (d) If infrastructure but no health probe: run `infra-health.sh
                      check <component>`, populate `infra_health_check`, re-submit.
                  (e) If the block is a genuine false positive: re-run with
                      `--override-blocker-gate "<one-sentence justification>"`. The
                      override is append-logged to `world/blocker-gate-overrides.jsonl`
                      for later audit.

            NEVER silently ignore an exit-1 from this gate. Creating a blocker
            on insufficient evidence puts the agent to sleep on non-problems.

       2.56. RECORD NEGATIVE CONCLUSION (fail-quiet — judgment-quality audit):
            The evidence has just been structurally validated by Step 2.55, so
            it's exactly the right moment to record the conclusion in the
            `conclusions` WM slot for later audit (consolidate Step 2.7 sweeps
            the slot for judgment-quality stats — correct/wrong/pending).

            Per core/config/conventions/negative-conclusions.md "Recording
            Conclusions", one entry per blocker. Pipe the SAME blocker_json
            that Step 2.55 accepted — conclusion-record.py reuses the
            `failure_reason` as the claim text and re-weights evidence
            (silent-flag commands score 0, real commands score 1).

            Bash: echo '<blocker_json>' | bash core/scripts/conclusion-record.sh \
                    --blocks-goals "<goal.id>" \
                    --reverify-minutes 30

            Fail-quiet rule: this write MUST NOT block blocker creation.
            Exit 1 (WM unavailable, disk error, NO_AGENT race) just means
            the judgment audit will miss one entry — the blocker itself is
            still persisted downstream. Do not retry or escalate. The gate
            script prints to stderr on failure; that's the only surface.

       2.6. AUTOMATED CAPABILITY GATE (mandatory — enforcement of Step 2.5):
            Run capability-gate.py as an independent cross-check of Step 2.5's decision.
            The gate scans forged-skills.yaml, SKILL.md triggers, and capability-routing.md
            for keyword matches against failure_reason. It is the safety net for Step 2.5.

            Bash: bash core/scripts/capability-gate.sh \
                    --failure-reason "<failure_reason>" \
                    --intended-participants <chosen from 2.5: agent|user|hybrid> \
                    [--evidence '[{"type":"rb","id":"rb-NNN","claim":"..."}]'] \
                    [--override-agent-match "<justification>"] \
                    --output json

            Branch on exit code:
              - Exit 0 → gate agrees with Step 2.5 (or approval was supplied). Proceed to Step 3.
              - Exit 1 → gate blocks. The decision to use participants:[user] conflicts with
                a matched agent-provisionable capability. Read the JSON `reason` and
                `matches[0]` fields, then:
                  (a) If the match is correct and the LLM missed it: revise participants
                      to [agent] (or [agent, user] for hybrid), re-run the gate with
                      --intended-participants updated, and proceed to Step 3.
                  (b) If the agent has empirical evidence the goal can be self-handled
                      despite the [user] tag (e.g., a prior rb-NNN / pipeline resolution /
                      metric showing the capability is already available): re-run the gate
                      with --evidence '[{"type":"rb|pipeline|metric|goal|guardrail|tree|experience",
                      "id":"<identifier>","claim":"<why this evidence applies>"}]'. Valid
                      entries log an 'evidence-approval' record to
                      world/blocker-gate-overrides.jsonl. Prefer --evidence over
                      --override-agent-match when the agent has structured support —
                      structured > free-text for the user's audit trail.
                  (c) If the match is a genuine false positive (no evidence to cite,
                      just a wrong keyword match): re-run the gate with
                      --override-agent-match "<one-sentence justification>".
                      The justification is echoed to stderr for audit and MUST name the
                      specific reason the matched skill doesn't apply.

            NEVER silently ignore an exit-1 from this gate. NEVER write the blocker goal
            with participants:[user] without either (a) revising participants, (b) evidence-
            based approval, or (c) an explicit override justification. This is the
            enforcement Step 2.5 was always meant to have.

       3. Create unblocking goal (born with the blocker)
          participants = result of Step 2.5 CAPABILITY SCAN, validated by Step 2.6 gate
          title = "Unblock: {failure_reason (50 chars)}"
          priority = HIGH, skill = null
          description includes: reason, diagnostic context, affected goals, what was tried
          → echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <aspiration_id>
          → capture new goal ID

       4. Create blocker entry
          blocker_id = "infra-{skill-slug}-{date}"
          type = infrastructure | resource | user_action
          unblocking_goal = <new goal ID>
          diagnostic_context = { error_alerts, cascade_chain, attempted_fix }
          resolution = null

  5. Cascade-block same-skill goals in queue
     Append to affected_goals
  6. Notify the user about the blocker.
     (Check world/forged-skills.yaml for a skill whose triggers match
     "notify the user" and invoke it with a blocker-category payload:
       subject: "Blocked: <skill-slug> — <N> goals cascaded"
       message: what's blocked, why, unblocking goal ID, cascade count,
                diagnostic_context summary
     If no matching skill is registered, fall back to a `participants: [agent, user]`
     goal via aspirations-add-goal.sh. Never block blocker creation on
     notification failure — the blocker itself is already persisted.)
  7. echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
  8. Write journal entry about blocker creation + cascade chain
```
