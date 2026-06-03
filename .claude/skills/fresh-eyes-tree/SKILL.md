---
name: fresh-eyes-tree
description: "Periodic local self-audit of the knowledge tree's top-level taxonomy (cadence: every 200 goals). Assembles a briefing covering L1 distribution skew (S1), L1 pick-rate trends (S9), candidate L2 promotions, candidate L1 retirements, and SPROUT/REPARENT history, writes it to agents/<agent>/temp/ (a staging file drained to the knowledge tree), and posts a one-line summary to the coordination board. No email push, no user-approval gate. The user can invoke l1-domain-add.sh / l1-domain-rename.sh manually if taxonomy changes are desired. Closes the 'tree has no taxonomy-level review' gap."
user-invocable: true
triggers:
  - "/fresh-eyes-tree"
  - "fresh eyes tree"
  - "tree taxonomy review"
  - "L1 review"
tools_used: [Bash, Read, Write, Edit, Skill]
companion_scripts: [core/scripts/fresh-eyes-cadence-check.sh, core/scripts/fresh-eyes-record-tick.sh, core/scripts/l1-skew-check.sh, core/scripts/l1-emergence-detector.sh, core/scripts/tree-read.sh]
conventions: [aspirations, session-state, working-memory, tree-retrieval]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-fresh-eyes-tree-001"
previous_revision_id: null
---

# /fresh-eyes-tree — Periodic Tree-Taxonomy Self-Audit

Every 200 completed goals (or on user demand), step back and produce a
taxonomy assessment:

**Are the L1s still the right top-level cuts for the knowledge tree?**

The ritual runs autonomously, writes the briefing to `agents/<agent>/temp/`,
and posts a one-line summary to the coordination board. No email push, no
user-approval gate. The user reviews via git log and tracked signals.

If taxonomy changes are desired, the user can invoke the S8 apply scripts
(`l1-domain-rename.sh`, `l1-domain-add.sh`) manually. These scripts remain
standalone user-runnable tools. The periodic ritual does NOT auto-invoke or
gate on them.

## Sibling Relationship

Third sibling in the fresh-eyes ritual family. All three share infrastructure
(`fresh-eyes-cadence-check.sh`, `fresh-eyes-record-tick.sh`) but target
different scopes:

| Ritual | Scope | Cadence | WM slot |
|---|---|---|---|
| `/fresh-eyes-review` | Per-agent Self + portfolio | 25 goals | `last_fresh_eyes_review` |
| `/fresh-eyes-program` | World shared purpose + team alignment | 100 goals | `last_fresh_eyes_program_review` |
| `/fresh-eyes-tree` | **Knowledge-tree top-level taxonomy** | **200 goals** | `last_fresh_eyes_tree_review` |

Lower frequency (200 vs 100 vs 25) because L1 changes are high-blast-radius
and the evidence needs accumulation time. Skew + pick-rate signal is most
useful when measured across a few hundred goals.

## Sub-commands

```
/fresh-eyes-tree                 — User-forced review, bypasses cadence gate
/fresh-eyes-tree --cadence       — Check cadence; run only if gate passes
                                   (agent-invoked path from precheck)
```

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front
matter. Read only the paths returned. If output is empty, all conventions
already loaded — proceed.

## Phase 1: Cadence Gate

```
IF invoked with --cadence:
    Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_tree
    IF exit 1: Output "Fresh-eyes-tree: cadence not crossed — noop." → DONE (return)
    IF exit 0: proceed
ELSE (user-invoked, no --cadence flag):
    Proceed directly — user override.
```

## Phase 2: Briefing Assembly (read-only)

Read the inputs. Cache each result so Phase 3 can synthesize without re-reading.

```
# 2.1 L1 distribution stats (S1) — the structural mass + retrieval + utility breakdown
Bash: bash core/scripts/l1-skew-check.sh --markdown
  → capture markdown table (already nicely formatted)
Bash: py -3 core/scripts/tree.py read --stats --by-l1
  → capture JSON for programmatic use (mean_confidence, capability_mass, etc.)

# 2.2 L1 pick-rate history (S9) — what got picked recently
Bash: py -3 -c "
import json, sys, os
from pathlib import Path
sys.path.insert(0, 'core/scripts')
from _paths import META_DIR
log = Path(META_DIR) / 'l1-pick-log.jsonl'
if not log.exists():
    print(json.dumps({'entries': 0, 'pick_rate': {}, 'decision_types': {}}))
    sys.exit(0)
lines = log.read_text(encoding='utf-8').splitlines()
recent = [json.loads(l) for l in lines[-500:] if l.strip()]
from collections import Counter
pick_rate = Counter(e['l1'] for e in recent)
decision_types = Counter(e['decision_type'] for e in recent)
sources = Counter(e.get('source') or 'unknown' for e in recent)
print(json.dumps({
    'entries': len(recent),
    'pick_rate': dict(pick_rate.most_common()),
    'decision_types': dict(decision_types),
    'sources': dict(sources),
}))
"
  → parse the JSON for the briefing

# 2.3 Tree growth log (RENAME/ADD/REPARENT/PRUNE history).
# DO NOT interpolate ${WORLD_DIR} into the python -c body — guard-165 forbids
# bash-var injection into py source. Resolve WORLD_DIR via _paths import, the
# same way Phase 2.2 resolves META_DIR.
Bash: py -3 -c "
import sys, yaml
sys.path.insert(0, 'core/scripts')
from _paths import WORLD_DIR
with open(str(WORLD_DIR / 'knowledge' / 'tree' / '_tree.yaml'), 'r', encoding='utf-8') as f:
    tree = yaml.safe_load(f)
log = (tree.get('tree_growth_log') or [])[-30:]
print(yaml.dump(log, sort_keys=False, allow_unicode=True))
"
  → capture last 30 structural ops for the "what's been happening" section

# 2.4 Emergence candidates — S4 new-L1, S6 cross-domain leak, S7 reparent
# signals. The detector reads meta/l1-pick-log.jsonl (S9) and the live
# tree, runs three orthogonal analysis passes, and emits structured
# candidates with strength scores.
Bash: bash core/scripts/l1-emergence-detector.sh --markdown
  → capture the rendered table (paste into Phase 3 briefing verbatim)
Bash: bash core/scripts/l1-emergence-detector.sh
  → also capture the JSON form for programmatic use

JSON shape (consume programmatically when synthesizing the briefing):
  - s4_new_l1_candidates: LIST of {parent_key, l1, cluster_size,
    distinctive_tokens, child_keys, signal_strength}. Empty list when
    no parent met the cluster bar.
  - s6_cross_domain_leaks: LIST of {target_node, current_l1,
    suggested_l1, current_overlap, suggested_overlap, summary}. Empty
    list when no recent SPROUT scored higher against a non-assigned L1.
  - s7_reparent_signals: DICT (NOT a list) — {status, findings,
    total_picks, threshold}. status ∈
      no_data / no_tree   — detector inputs unavailable
      data_sparse         — <10 picks logged; signal not yet meaningful
      balanced            — all L1s within imbalance threshold
      imbalanced          — findings list is non-empty
    findings: LIST of {l1, signal: hot|stagnating, pick_share,
    mass_share, imbalance, interpretation}.

S4/S6 return [] when no candidates met the bar; S7 carries an explicit
status to distinguish "wait for more data" from "all clear" — they look
identical in `findings`=[] otherwise. Code that iterates s7 must read
s7["findings"], NOT s7 itself.

# 2.5 Goal-count context
Bash: core/scripts/fresh-eyes-cadence-check.sh --config-block fresh_eyes_tree --verbose
  → capture current goals-completed count, last-fire count, diff
```

## Phase 3: Synthesis

Build the briefing text (plain Markdown). Each observation must follow
`.claude/rules/communication-clarity.md` rule 6 — assert what evidence shows,
do not hedge.

```markdown
# Fresh-eyes TREE review — {today ISO date}

{One paragraph: N goals completed since last tree review (or "first tree
review since taxonomy review mechanism shipped on 2026-05-14"). What has
happened in the tree across this window — total nodes, growth log
operations, drift signals.}

## Are the L1s still the right top-level cuts?

Current L1 set (from core/config/tree.yaml l1_domains):

{For each L1: "- **{key}** — {summary}"}

### Distribution evidence (S1)

{Paste the markdown table from `l1-skew-check.sh --markdown`}

{Observation paragraph: which L1 is over-mass, which is under-mass.
Reference specific ratios. "Intelligence has 69× the structural mass of
performance; retrieval volume ratio is 124×. Performance has zero EXPLOIT
or MASTER nodes — it is structurally an early-stage L1 12 months after
framework inception."}

### Pick-rate evidence (S9)

Recent {entries} L1 picks (last ~500 tree writes):

| L1 | Picks | % of total |
|---|---:|---:|
{One row per L1 from pick_rate, sorted desc}

Decision-type distribution:
{One row per decision_type from decision_types}

Source attribution (where the picks come from):
{One row per source from sources, top 5}

{Observation paragraph: do picks distribute the same way nodes do? If
yes, the skew is self-reinforcing. If the picks lean toward one L1
disproportionate to its mass, that L1 is growing FASTER than others —
emergent signal worth noting.}

### Structural-op history (last 30)

{Paste tree_growth_log tail. Highlight any L1_ADD or L1_RENAME entries
since last review.}

### Candidate moves

{For each candidate, one bullet with: action (RENAME or ADD), reason
(evidence-backed), proposed key + summary, blast radius. None if the
evidence is balanced — say so.}

Out of scope for this review (filed for future): RETIRE and RESHAPE
operations. Surface candidates here as observations only; do not propose
them as one-shot moves — they require a separate workflow.

## Assessment

**Are the four L1s — execution, intelligence, performance, system —
still the right top-level cuts for this knowledge tree?**

{Agent's assessment based on the evidence assembled above. State the
conclusion clearly: "no change warranted" or "candidate change: {X}
with rationale: {Y}" — but do not auto-apply taxonomy changes.}

If the user decides a change is warranted after reviewing this archive,
they invoke the S8 apply scripts directly:
- `bash core/scripts/l1-domain-rename.sh --approved-by <user-supplied-id>`
- `bash core/scripts/l1-domain-add.sh --approved-by <user-supplied-id>`
```

## Phase 4: Stage Briefing to temp/

```
Bash: mkdir -p agents/<agent>/temp
Write the briefing body to agents/<agent>/temp/fresh-eyes-tree-{today-isotime}.md
  (where {today-isotime} = `date +%Y-%m-%dT%H-%M-%S` — colons replaced with
   hyphens for Windows filesystem compatibility)
```

## Phase 5: Apply-Script Note

The S8 apply scripts (`l1-domain-rename.sh`, `l1-domain-add.sh`) remain
standalone user-runnable tools with their own `--approved-by` validation. If
the user decides a taxonomy change is warranted after reviewing the archived
briefing, they invoke the apply scripts directly. The periodic ritual does
not gate on them.

## Phase 5.5: Encode Durable Findings

The Phase 3 briefing's taxonomy-health observations otherwise land ONLY in
the transient `temp/` staging file, which is invisible to `/prime` and
`retrieve.sh` and is drained away over time. This step encodes the substantive
findings into the durable stores so they survive after the staging file is
drained. Modeled on `/felt-sense-checkin` Phase 1.

**No-double-encode**: this skill has NO `act_now` (Self/Program edit) or
`act_later` (goal) routing — every finding here is net-new durable storage,
so there is nothing to exclude. (If a future revision adds a self-assess /
triage step, this step must then skip any finding already routed to Self or a
filed goal.)

Encode the substantive findings (S1 distribution skew, S9 pick-rate trends,
S4/S6/S7 emergence candidates, structural-op observations, and the overall
taxonomy assessment) per `core/config/conventions/learning-routing.md`. When
in doubt, drop:

- **tree** — a compressed durable fact about tree structure (skew ratio,
  pick-rate distribution, emergence candidate, taxonomy assessment). Target
  the tree-taxonomy-health area under the `system` L1. **Novelty gate
  (mandatory — preserves a time series instead of flooding):** before adding,
  check whether a node already covers this (`tree-read.sh --node
  {candidate-key}`). If one exists and this is a refreshed measurement,
  `/tree edit` it (update body + `last_updated` + `last_update_trigger:
  fresh-eyes-tree`) rather than adding a duplicate. Use `/tree add {parent}
  {key} {summary}` ONLY for a genuinely novel finding (e.g. a new emergence
  candidate).
- **reasoning_bank** — a recurring cross-review pattern (e.g. "skew is
  self-reinforcing across 3+ reviews"). `reasoning-bank-add.sh` with summary
  + ABC chain + `applies_to: framework`.
- **guardrails** — a prescriptive rule with a trigger condition (e.g. "when a
  single L1 exceeds N% structural mass for 2+ reviews, surface a decompose
  candidate"). `guardrails-add.sh` with rule + trigger_condition.
- **drop** — already captured, too thin, or a one-cycle anomaly.

Taxonomy CHANGES remain user-driven via the S8 apply scripts (Phase 5) — this
step encodes OBSERVATIONS only, never mutates `core/config/tree.yaml
l1_domains`. The encoding writes are self-evidencing; no separate log line is
required. Do NOT add a terminal action here — Phase 6's board-post remains
the skill's final tool call.

## Phase 6: Record the Tick

```
# Step 1: Record stamp (LOAD-BEARING — same g-240-60 lesson as the sibling rituals)
Bash: bash core/scripts/fresh-eyes-record-tick.sh last_fresh_eyes_tree_review

# Step 2: Board post (best-effort)
Bash: echo "Fresh-eyes TREE review completed; briefing staged at agents/<agent>/temp/fresh-eyes-tree-{today-isotime}.md." | bash core/scripts/board-post.sh --channel general --type status --tags fresh-eyes-tree || true
```

The board-post is the terminal action — per Return Protocol requirements,
the skill does NOT end with text output.

## Chaining

- **Called by**: User (`/fresh-eyes-tree`), `/aspirations-precheck` Phase 0.5e.7
  (`/fresh-eyes-tree --cadence`)
- **Calls**: `fresh-eyes-cadence-check.sh --config-block fresh_eyes_tree`,
  `l1-skew-check.sh --markdown`,
  `tree-read.sh --stats --by-l1`, `fresh-eyes-record-tick.sh
  last_fresh_eyes_tree_review`, `board-post.sh`,
  `/tree add`, `/tree edit`, `tree-read.sh --node`, `reasoning-bank-add.sh`,
  `guardrails-add.sh` (Phase 5.5 encoding)
- **Reads**: `meta/l1-pick-log.jsonl`, `world/knowledge/tree/_tree.yaml`
  (tree_growth_log), `core/config/tree.yaml` (l1_domains)
- **Modifies**: `agents/<agent>/temp/fresh-eyes-tree-*.md` (new staging file),
  `agents/<agent>/session/working-memory.yaml` (update last_fresh_eyes_tree_review),
  board `general` channel (best-effort),
  `world/knowledge/tree/` (Phase 5.5 observation nodes under existing L1s),
  `world/reasoning-bank.jsonl` (Phase 5.5 appends),
  `world/guardrails.jsonl` (Phase 5.5 appends)
- **Does NOT modify**: `core/config/tree.yaml l1_domains` — the L1 cut
  itself. Taxonomy changes (rename/add an L1) remain user-driven via
  `l1-domain-rename.sh` / `l1-domain-add.sh` invoked manually. No email is
  sent. (Phase 5.5 DOES add observation nodes under existing L1s and may
  append reasoning-bank / guardrail entries — see Modifies.)

## Relationship to Existing Mechanisms

| Mechanism | Scope | Trigger | User-facing? |
|-----------|-------|---------|--------------|
| `/tree maintain` | Per-node structural ops (decompose, distill, etc.) | Autonomous + manual | No |
| `/reflect` Step 7 Tree Health Lint | Per-node staleness + cross-ref + width | Reflection cadence | No |
| `l1-skew-check.py` (S1) | Per-L1 distribution measurement | Aspirations-precheck Phase 0.5g (50 goals) | Board post on skew |
| `/fresh-eyes-tree` | **L1 taxonomy itself — is the cut right?** | **200 goals cadence + user demand** | **No — local audit** |
| `l1-domain-rename.sh` / `l1-domain-add.sh` (S8) | Apply approved L1 changes | User invocation | Direct invocation |

Fresh-eyes-tree is the periodic self-audit at the L1-taxonomy scope. It
does NOT replace any of the above. `/tree maintain` keeps node-level
structure healthy; `/reflect` Step 7 lints individual node health; S1
surfaces quantitative skew evidence; S8 applies approved changes (invoked
by the user manually). This skill is the deliberate, low-frequency "step
back and examine the top-level cuts" that none of the others surface. The
user reviews via git log and tracked signals.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call,
not text. The terminal action is the Phase 6 Step 2 board-post Bash call.
Never end this skill with a text summary of the briefing — the briefing
is in the archive.
