---
name: aspirations-strategic-scan
description: "Performs a strategic environmental scan: reads world signals, recurring-goal outputs (infra health, email, audit trails), knowledge-tree frontier, portfolio health, and intrinsic motivation — then generates new aspirations from external observation rather than introspection. Use whenever the aspirations loop hits the strategic-scan cadence (every N iterations), the goal pipeline looks thin, or the orchestrator needs fresh work driven by real-world signal instead of self-generated ideas."
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, goal-schemas, tree-retrieval, infrastructure]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-aspirations-strategic-scan-f6bdae"
previous_revision_id: null
---

# /aspirations-strategic-scan -- Strategic Environmental Scan

Periodically "step back and look at the world." Unlike sparks (reactive to one goal)
or evolution (watching agent learning curves), this skill reads the ENVIRONMENT --
recurring goal outputs, knowledge freshness, portfolio balance, and unexplored territory
-- and generates work from what it observes.

**Design principle**: Sparks ask "what did I just learn?" Evolution asks "how am I growing?"
Strategic scan asks "what does the world need?" This is the intrinsic motivation engine.

## Inputs

- `scan_trigger`: Why the scan was triggered ("goal_cadence", "recurring_settling", "time_cadence")
- `source`: Source identifier for goal/aspiration creation

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front matter.
Read only the paths returned (files not yet in context). If output is empty, proceed.

## Phase S1: Recurring Goal Output Review

Read recent execution history for each recurring goal. Recurring goals are the agent's
"sensors" -- they periodically observe the world and produce data. This phase reads
that data and looks for signals that demand new work.

```
Bash: load-aspirations-compact.sh -> IF path returned: Read it
recurring_goals = [g for asp in compact for g in asp.goals
                   if g.get("recurring", False) and g.get("achievedCount", 0) >= 2]

signals = []
FOR EACH rg in recurring_goals (cap at 10 most-recently-achieved):
    # Read last 3 experience entries for this recurring goal
    Bash: experience-read.sh --goal {rg.id}
    entries = parse result

    IF entries is empty or len(entries) < 2: continue  # need 2+ for trend

    # S1a: Regression detection
    # Are metrics, health indicators, or quality measures getting WORSE
    # across recent entries? The LLM interprets the experience text --
    # this is domain-agnostic because any recurring goal's output works.
    # Look for: error counts increasing, success rates decreasing,
    # response times increasing, data quality declining, scores dropping.
    IF entries show worsening trend across 2+ consecutive executions:
        signals.append({
            type: "regression",
            source_goal: rg.id,
            aspiration: rg.parent_asp_id,
            description: "Recurring goal '{rg.title}' shows worsening trend: {what_is_declining}",
            severity: "HIGH",
            evidence: [concise entry summaries]
        })

    # S1b: Anomaly detection
    # Did the most recent execution produce results significantly
    # different from the prior pattern? Not necessarily worse -- just different.
    # Anomalies are worth investigating because they signal change.
    IF latest entry is significantly different from prior entries:
        signals.append({
            type: "anomaly",
            source_goal: rg.id,
            aspiration: rg.parent_asp_id,
            description: "Anomaly in '{rg.title}': {what_changed}",
            severity: "MEDIUM",
            evidence: [concise entry summaries]
        })

    # S1c: Stagnation detection
    # Has this recurring goal produced identical/near-identical results
    # for 3+ consecutive executions? If so, the monitoring may need to
    # look at something different, or the thing being monitored is stuck.
    IF all entries are semantically identical for 3+ executions:
        signals.append({
            type: "stagnation",
            source_goal: rg.id,
            aspiration: rg.parent_asp_id,
            description: "Recurring goal '{rg.title}' producing identical results for {N} executions -- monitoring may need to evolve or the subject is stuck",
            severity: "LOW",
            evidence: [latest entry summary]
        })
```

## Phase S2: Knowledge Frontier Scan

Read the knowledge tree and identify areas where knowledge is aging, thin,
or has open questions. These are not problems to fix -- they are opportunities
to explore.

```
Read core/config/aspirations.yaml -> strategic_scan config

# S2a: Stale nodes -- knowledge that may have drifted from reality
Bash: tree-read.sh --summary
tree_summary = parse result
stale_nodes = [node for node in tree_summary
               if node.last_updated and days_since(node.last_updated) > strategic_scan.knowledge_staleness_days
               and node.capability_level not in ("MASTER",)]

IF len(stale_nodes) > 3:
    signals.append({
        type: "stale_knowledge",
        description: "{len(stale_nodes)} tree nodes not updated in {strategic_scan.knowledge_staleness_days}+ days: {[n.key for n in stale_nodes[:5]]}",
        severity: "MEDIUM",
        nodes: [n.key for n in stale_nodes[:5]]
    })

# S2b: Thin nodes -- knowledge areas with minimal depth
thin_nodes = [node for node in tree_summary
              if node.get("article_count", 0) < 2
              and node.capability_level not in ("MASTER", "EXPERT")
              and node.depth >= 2]  # only leaf/near-leaf nodes

IF len(thin_nodes) > 3:
    signals.append({
        type: "thin_knowledge",
        description: "{len(thin_nodes)} tree nodes have minimal coverage: {[n.key for n in thin_nodes[:5]]}",
        severity: "LOW",
        nodes: [n.key for n in thin_nodes[:5]]
    })
```

## Phase S3: Aspiration Portfolio Health

Beyond the precheck's pipeline depth check, look at the DIVERSITY and BALANCE
of the aspiration portfolio. A healthy portfolio has work across multiple
categories aligned with Self priorities.

```
active_asps = [asp for asp in compact if asp.status == "active"]
categories = {}
FOR EACH asp in active_asps:
    FOR EACH g in asp.goals WHERE g.status in ("pending", "in-progress"):
        cat = g.get("category", "uncategorized")
        categories[cat] = categories.get(cat, 0) + 1

# S3a: Category concentration
IF categories:
    total = sum(categories.values())
    IF total > 0:
        max_cat = max(categories, key=categories.get)
        max_cat_pct = categories[max_cat] / total
        IF max_cat_pct > strategic_scan.concentration_threshold:
            signals.append({
                type: "concentration",
                description: "Work concentrated: {max_cat_pct:.0%} of pending goals in category '{max_cat}' -- other areas may be neglected",
                severity: "LOW",
                category: max_cat
            })

# S3b: Self priority coverage
# Check if Self's stated priorities have corresponding active work.
Read agents/<agent>/self.md
Extract the key responsibilities/priorities from Self
Compare against active aspiration titles and goal categories.
uncovered = [priority for priority in self_priorities
             if no active aspiration or goal addresses it]

IF uncovered:
    signals.append({
        type: "uncovered_priorities",
        description: "{len(uncovered)} Self priorities without active work: {uncovered[:3]}",
        severity: "MEDIUM",
        priorities: uncovered[:5]
    })

# S3c: Portfolio health signal (consumed by evolve Step 2.75)
# Lightweight detection — evolve does the actual cleanup.
high_count = sum(1 for a in active_asps if a.priority == "HIGH")
high_pct = high_count / len(active_asps) if active_asps else 0

completed_unarchived = sum(1 for a in active_asps
    if all(g.status in ("completed","skipped","expired") for g in a.goals if not g.get("recurring"))
    and any(g for g in a.goals if not g.get("recurring")))

IF high_pct > 0.70 OR completed_unarchived >= 2:
    echo '{"priority_inflation":<true if high_pct exceeds 0.70>, "high_pct":<high_pct>, "completed_unarchived":<completed_unarchived>, "detected_at":"<now>"}' | wm-set.sh portfolio_health_signal
```

## Phase S4: Curiosity and Novelty Seeking (Intrinsic Motivation)

This is the "intrinsic motivation" engine. Rather than reacting to problems
(S1-S3), this phase proactively seeks novelty. It implements the agent's
drive to explore and discover, not just maintain and fix.

```
# S4a: Unexplored territory
# Identify tree categories that have zero or minimal recent work.
explored_cats = set(categories.keys())  # from S3
all_L2_cats = set(node.key for node in tree_summary if node.depth <= 2)
unexplored = all_L2_cats - explored_cats

IF unexplored:
    signals.append({
        type: "unexplored_territory",
        description: "Knowledge tree has {len(unexplored)} L2 categories with no recent work: {list(unexplored)[:3]}",
        severity: "LOW",
        categories: list(unexplored)[:5]
    })

# S4b: Cross-pollination opportunities
# Look for recent reasoning bank insights from one category that
# might apply to other categories -- transfer learning opportunities.
Bash: reasoning-bank-read.sh --recent 10
recent_insights = parse result
FOR EACH insight in recent_insights:
    IF insight.category != max_cat AND insight.utilization.times_helpful < 2:
        signals.append({
            type: "cross_pollination",
            description: "Insight '{insight.title}' from {insight.category} may transfer to other domains -- used only {insight.utilization.times_helpful} times",
            severity: "LOW",
            insight_id: insight.id
        })
        break  # One cross-pollination signal per scan is enough
```

## Phase S5: Signal Triage and Action

Route signals to the appropriate action based on severity. This is where
observation becomes work.

```
# Single-writer cadence stamp: reaching S5 means the scan ran end-to-end
# (signals were collected in S1-S4) regardless of whether any fired.
# The orchestrator's Phase 1.5 time_cadence trigger reads this slot.
# Without this write, strategic_scan.hours_cadence silently never fires.
echo "\"$(date +%Y-%m-%dT%H:%M:%S)\"" | Bash: wm-set.sh last_strategic_scan

IF len(signals) == 0:
    Output: ">> Strategic scan ({scan_trigger}): no signals -- environment is healthy"
    Bash: echo "Return to orchestrator"
    RETURN

# Sort by severity, cap at max_signals_per_scan
signals.sort(key=lambda s: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[s.severity])
signals = signals[:strategic_scan.max_signals_per_scan]

high_signals = [s for s in signals if s.severity == "HIGH"]
medium_signals = [s for s in signals if s.severity == "MEDIUM"]
low_signals = [s for s in signals if s.severity == "LOW"]

Output: ">> Strategic scan ({scan_trigger}): {len(signals)} signal(s) detected"
FOR EACH signal in signals[:5]:
    Output: "  [{signal.severity}] {signal.type}: {signal.description}"

# ── HIGH signals: create investigation goals immediately ──
FOR EACH signal in high_signals:
    # Find the most relevant active aspiration for this signal
    target_asp = (signal.aspiration if signal has aspiration
                  else find_aspiration_by_category(signal, compact))
    IF target_asp:
        goal_title = "Investigate: " + signal.description[:80]
        # Check for duplicate goal titles in target aspiration
        IF no similar title exists in target_asp.goals:
            goal_json = {
                title: goal_title,
                description: "Strategic scan detected: {signal.description}\nEvidence: {signal.evidence}\nAction: Investigate root cause and determine corrective action.",
                status: "pending",
                priority: "HIGH",
                category: signal.get("category", target_asp.goals[0].category),
                participants: ["agent"],
                origin_signal: "investigate:strategic-scan-{signal.type}"
            }
            echo '<goal_json>' | Bash: aspirations-add-goal.sh --source {source} {target_asp.id}
            Log: "STRATEGIC SCAN: HIGH signal -> created investigation goal in {target_asp.id}"

# ── MEDIUM signals: invoke create-aspiration with context ──
IF medium_signals:
    invoke /create-aspiration from-self with:
        scan_context: medium_signals  # Phase E3 will pick these up

# ── LOW signals: store in working memory for spark enrichment ──
IF low_signals:
    # Store signals for Phase R2 of routine spark and Phase E1 of create-aspiration
    low_signals_json = [{"type": s.type, "description": s.description,
                         "categories": s.get("categories", []),
                         "nodes": s.get("nodes", [])} for s in low_signals]
    echo '<low_signals_json>' | Bash: wm-set.sh strategic_scan_signals
    Log: "STRATEGIC SCAN: {len(low_signals)} LOW signals stored for spark enrichment"

# Journal the scan
echo '{"date":"<today>","event":"strategic_scan","details":"{len(signals)} signals: {len(high_signals)} HIGH, {len(medium_signals)} MEDIUM, {len(low_signals)} LOW, trigger: {scan_trigger}"}' | bash core/scripts/evolution-log-append.sh
Bash: echo "Return to orchestrator -- continue to next phase"
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 1.5, conditional)
- **Calls**: `experience-read.sh`, `tree-read.sh`, `reasoning-bank-read.sh`, `aspirations-add-goal.sh --source`, `wm-set.sh`, `/create-aspiration` (for MEDIUM signals)
- **Reads**: Aspiration compact data, experience entries, tree summary, reasoning bank, Self, config
- **Writes**: Working memory (`last_strategic_scan`, `strategic_scan_signals`, `portfolio_health_signal` slots), investigation goals (HIGH signals), evolution log
- **Source routing**: All `aspirations-*.sh` calls receive `--source {source}` from the orchestrator

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `wm-set.sh` (strategic_scan_signals slot) or
`aspirations-add-goal.sh`. Never end with a text summary of signals observed.
