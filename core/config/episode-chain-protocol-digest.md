# Episode Chain Protocol — MR-Search Retry with Reflection

Extracted from `.claude/skills/aspirations-execute/SKILL.md`. Loaded on-demand via
`load-episode-chain-protocol.sh` when Phase 4-post classification yields a
deep-outcome failure eligible for retry.

After Phase 4-post outcome classification, before proceeding to Phase 4.0/4.1,
check if this goal should be retried with accumulated reflection context.
Inspired by MR-Search (arXiv 2603.11327): chaining N attempts with structured
self-reflection between each episode enables the agent to learn *through*
failure within the same problem context.

## Protocol

```
# Read episode chaining config
Read core/config/aspirations.yaml → episode_chaining section

# Determine if chaining should activate
# GUARD: Never chain infrastructure failures — Phase 4.0 owns the blocker protocol.
chain_trigger = false
IF result is INFRASTRUCTURE_UNAVAILABLE or RESOURCE_BLOCKED:
    chain_trigger = false  # Let Phase 4.0 handle infrastructure failures
ELIF outcome_class == "deep" AND NOT goal_succeeded:
    IF "failed" in episode_chaining.chain_on_outcomes:
        chain_trigger = true

IF chain_trigger:
    # Check context budget zone for max episodes.
    # SINGLE SOURCE OF TRUTH: read zone directly from context-budget.json — the
    # file written by the status line on every prompt. Do NOT mirror zone into
    # working memory (guard-157: mirror anti-pattern). Any "context_zone" WM
    # slot is phantom. If this read fails, the status line never fired —
    # surface that loudly, do NOT synthesize a default zone.
    # Zones are distance-to-autocompact (pct_to_autocompact), NOT raw usage.
    # See core/scripts/context-budget-status.py classify_zone for source of truth.
    Bash: `bash core/scripts/context-budget-banner.sh`   # required — quote this line in your text before the chain decision
    Bash: `cat agents/<agent>/session/context-budget.json`
    zone = parsed zone field
    max_episodes = episode_chaining.context_zone_override[zone]
        # Overrides MUST cover all three zones (fresh|normal|tight) — if a zone
        # is missing from config, this crashes loud by design. Do not add a
        # fallback to max_episodes_per_goal; that hides config drift.

    # Check current episode count
    Bash: wm-read.sh episode_chain --json
    IF episode_chain exists AND episode_chain.goal_id == goal.id:
        current_episode = episode_chain.current_episode
    ELSE:
        current_episode = 0

    IF current_episode < max_episodes:
        # ── Archive this attempt as an episode ────────────────────────
        episode_entry = {
            episode: current_episode + 1,
            approach: "1-2 sentence summary of approach taken",
            outcome: result.outcome_summary or "failed",
            key_observations: [
                "Key observation 1 from execution",
                "Key observation 2 (what was unexpected)"
            ],
            reflection: null,   # Populated below
            timestamp: "$(date +%Y-%m-%dT%H:%M:%S)"
        }

        # ── Structured mini-reflection between episodes ───────────────
        # MR-Search's core insight: explicit reflection between attempts
        # enables cross-episode exploration improvement.
        # The agent asks itself four questions:
        #   1. What went wrong or was unexpected?
        #   2. What assumptions were violated?
        #   3. What should I try differently next time?
        #   4. For violated assumptions: are there deeper inherited assumptions in
        #      my framing of this goal? Strip to ground truth and rebuild approach.
        episode_entry.reflection = generate_mini_reflection(
            goal, result, episode_entry.key_observations
        )

        # ── Update episode chain in working memory ────────────────────
        IF episode_chain exists:
            Append episode_entry to episode_chain.episodes
            episode_chain.current_episode = current_episode + 1
        ELSE:
            episode_chain = {
                goal_id: goal.id,
                max_episodes: max_episodes,
                current_episode: 1,
                episodes: [episode_entry]
            }
        echo '<episode_chain_json>' | Bash: wm-set.sh episode_chain

        # ── Update goal with episode history ──────────────────────────
        Bash: aspirations-update-goal.sh --source {source} <goal-id> episode_history '<episodes_array_json>'

        # ── Re-execute with accumulated context ───────────────────────
        # The execution preamble for the next attempt includes ALL prior
        # episodes + reflections. This is MR-Search's context accumulation:
        #   goal → episode₀ → reflection₀ → episode₁ → reflection₁ → ...
        Output: "▸ EPISODE CHAIN: Attempt {current_episode + 1}/{max_episodes} — retrying with reflection context"
        Log: "Episode {current_episode + 1}: Reflection: {episode_entry.reflection}"
        # Breadcrumb for compact recovery — postcompact-restore surfaces last 10 diary entries
        echo '{"entry_type":"approach_change","goal_id":"<goal.id>","content":"Episode {current_episode + 1}/{max_episodes}: <reflection.summary>"}' | bash core/scripts/execution-diary.sh append

        # Reset goal status for re-execution
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status in-progress

        # Re-invoke Phase 4 execution with episode chain as context preamble
        → re-execute goal.skill with episode_chain.episodes as execution context
        → return to Phase 4-post with new result (episode chain protocol re-evaluates)

    ELSE:
        # Max episodes reached — proceed normally with the final outcome
        Output: "▸ EPISODE CHAIN: Max attempts ({max_episodes}) reached — accepting final outcome"
        # Clear episode chain from working memory
        echo 'null' | Bash: wm-set.sh episode_chain

ELSE:
    # No chaining needed — clear any stale episode chain
    Bash: wm-read.sh episode_chain --json
    IF episode_chain exists AND episode_chain.goal_id == goal.id:
        echo 'null' | Bash: wm-set.sh episode_chain
```
