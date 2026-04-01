# Stop Conditions

The loop ONLY stops for these reasons:
1. **Agent state changed** — agent-state file is no longer "RUNNING" (user ran /stop)
2. **Critical error** — unrecoverable file corruption, authentication permanently revoked

These are NOT stop conditions (the loop MUST continue through them):
- Context filling up → autocompact handles it
- No agent-executable goals → gap analysis → generate new goals
- All aspirations complete → evolve → create new aspirations
- All goals blocked → constraint-aware aspiration generation → evolution gap analysis → research → reflection → wait 5 min only as last resort (precheck reprobes blockers each cycle)
- API rate limit → cooldown 60s → retry
- Running out of ideas → reflect, replay, research
- Wanting to ask a question → pending-questions.yaml, execute default_action, continue
- Unsure whether to push/deploy → just do it
- "What should I focus on next?" → the loop selects. Never ask.

# Skip Conditions

| Condition | Action |
|---|---|
| API rate limit hit | Log, wait 60s, skip to next goal |
| Duplicate work detected | Mark goal completed, log reason |
| External dependency not met | Mark goal blocked, create prep task |
| Cooldown period active | Skip aspiration, try next one |
| Goal already in-progress | Resume or skip based on context |
| Completion check passes already | Auto-complete, no execution needed |
