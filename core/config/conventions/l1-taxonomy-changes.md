# L1 Taxonomy Changes (S8)

User-approval-gated protocol for modifying the knowledge tree's top-level
domains. Before this convention shipped, `l1_domains` was hard-coded in
`core/config/tree.yaml` and never re-evaluated. S1's distribution skew
check now produces evidence the L1 boundaries can drift; this protocol is
how that evidence gets acted on — without giving the autonomous loop the
power to silently restructure the tree.

## Locked Scope (2026-05-14)

Two operations are in scope for this iteration:

1. **RENAME** — change an L1's key + summary. Recomputes every descendant's
   file path; the subtree directory is moved on disk in a single
   `shutil.move`. Implementation: `core/scripts/l1-domain-rename.sh`.

2. **ADD** — create a new L1. Existing nodes are not affected. Implementation:
   `core/scripts/l1-domain-add.sh`.

OUT of scope (deferred):

- **RETIRE** an L1: requires reparenting every child to another L1 first.
  High blast radius. A separate retire workflow will land in a later
  iteration when there is a concrete L1 to retire.
- **RESHAPE** the L1 set (multi-step combinations): same reasoning — the
  one-shot composite would be hard to roll back. Compose RENAME + ADD
  manually if needed.

Both deferred operations require fresh user direction; do not stretch this
convention to cover them implicitly.

## The Protocol

### 1. Trigger

A taxonomy change can be triggered by:

- **Skew evidence** — S1's `l1-skew-check.py` posted a `findings` board
  message after detecting a ratio above threshold.
- **Fresh-eyes-tree** — the periodic ritual (S5) surfaced a candidate in
  its briefing and the user answered "yes, change it."
- **Direct user direction** — user types "let's rename system to
  infrastructure" or "we need a new L1 for experiments."

Auto-firing without user approval is forbidden. The `requires_user_approval`
flag in `tree.yaml structural_modifiable.l1_domains` is the explicit gate.

### 2. Propose

The proposing agent files a pending-question with id prefix `l1-taxonomy-`:

```
bash core/scripts/pending-questions-add.sh \
    --id "l1-taxonomy-${date}-rename-${old_key}" \
    --question "Rename L1 '${old_key}' to '${new_key}'? ..." \
    --default-action "no-change" \
    --priority HIGH
```

The pending-question text must include:

- The current state (key + summary + descendant count + retrieval volume)
- The proposed state (new key + new summary)
- The evidence motivating the change (link to skew check, fresh-eyes
  briefing, or board post)
- The blast radius (number of descendants, file moves)

### 3. User Decides

User answers via email reply, `/respond` directive, or by editing
`pending-questions.yaml` directly. Resolution patterns:

- `answer: "yes"` + `status: resolved` → proceed to step 4
- `answer: "no"` + `status: resolved` → archive the question, take no
  action, optionally encode why the user said no for future reference

### 4. Apply

The agent (or user) runs the corresponding apply script with the resolved
pending-id as `--approved-by`:

```
bash core/scripts/l1-domain-rename.sh \
    --old-key system \
    --new-key infrastructure \
    --summary "HOW we work — system infrastructure and meta-knowledge" \
    --approved-by l1-taxonomy-2026-05-14-rename-system

# Or for ADD:
bash core/scripts/l1-domain-add.sh \
    --key experiments \
    --summary "What we're TESTING — open hypotheses and experiments" \
    --approved-by l1-taxonomy-2026-05-14-add-experiments
```

The apply scripts validate the approval id (prefix check + non-null) before
any writes. **They do NOT re-verify the resolution status in pending-questions
— that would be redundant ceremony.** The script's job is to apply a
user-approved decision atomically; the human chain of custody from
pending-question to apply-script-invocation is the audit trail.

### 5. Audit

Each apply script writes to three audit streams:

- `tree_growth_log` in `_tree.yaml` — `{op: L1_ADD | L1_RENAME, ...}`. These
  two apply scripts write their rows directly; the OTHER ops in this log
  (DECOMPOSE, PRUNE, REPARENT) are written by the tree write paths via
  `core/scripts/_growth_log.py` (g-115-3210). So this log is not L1-only —
  reading it that way is the specific error that let it sit frozen for 3.7
  months.
- `meta/l1-pick-log.jsonl` — uniform with S9's auto-logging (`decision_type:
  l1-add | l1-rename`)
- Stdout JSON containing the approval id and the executed plan

Forensic trail covers: who proposed, what was proposed, when user approved,
what got applied. No silent moves.

## Cross-references

- `core/config/tree.yaml` → `l1_domains:` (the state) +
  `structural_modifiable.l1_domains:` (the gate config)
- `core/scripts/l1-domain-add.py` — ADD script
- `core/scripts/l1-domain-rename.py` — RENAME script
- `core/scripts/l1-skew-check.py` — S1, the evidence producer
- `.claude/skills/fresh-eyes-tree/SKILL.md` — S5, the ritual consumer
- `.claude/rules/communication-clarity.md` rule 6 — applies to proposal
  text (assert observed evidence, no hedging)

## Anti-patterns

- Auto-firing rename or add without `--approved-by` — the script will exit 3
  refusing the change. Do not work around by passing a synthetic id; the
  prefix gate AND the agent's own honor system are both layers of defense.
- Proposing without S1 evidence or fresh-eyes-tree context — the proposal
  text must point at the data motivating the change. "It feels off" is
  not evidence.
- Skipping the pending-question step and running the apply script with a
  fabricated approval id — the audit trail is broken. The pending-question
  IS the user's signature.
- Mixing RENAME + ADD in a single proposal — separate questions, separate
  approvals. The user must consent to each move independently.
