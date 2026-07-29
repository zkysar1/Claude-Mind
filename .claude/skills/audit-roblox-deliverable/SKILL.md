---
name: audit-roblox-deliverable
forged: true
forged_by: delta
forged_date: "2026-07-05"
rematerialized_by: foxtrot
rematerialized_date: "2026-07-16"
description: "Audits a Roblox game-system or world deliverable against its spec via the live AyoBridge: rubric from the prior audit, per-env bridge liveness re-probe, read-only DataModel walk for structure (ayoType/ayoKey/ayoDescription), Source-level behavior verification, and an accept/punch-list verdict with the static-vs-runtime caveat. Use when a contractor ships a Roblox deliverable, when re-auditing a previously-WITHHELD deliverable after rework, or for any 'does this shipped Roblox work meet the spec' question."
minimum_mode: assistant
user-invocable: true
conventions:
  - infrastructure
companion_scripts:
  - world/scripts/roblox-studio.sh
triggers:
  - "contractor shipped a Roblox deliverable"
  - "audit a game-system or world build against its spec"
  - "verify Roblox in-world content completeness before sign-off"
  - "quality-audit a Roblox deliverable"
gap_ref: gap-006
revision_id: "skill-rematerialize-audit-roblox-deliverable-g1152298"
previous_revision_id: null
---

# /audit-roblox-deliverable — Roblox Deliverable Audit

Wraps the reusable audit procedure in `world/conventions/roblox-deliverable-audit.md`
(method origin rb-1304; done manually twice — bravo 2026-05-10 WITHHOLD, charlie
2026-05-25 ACCEPT — before the convention existed). The convention file is the
canonical procedure spec; this skill is its operational entry point. If the two
ever diverge, the convention wins — update this wrapper.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the
`conventions:` front matter. Then read the canonical procedure:
`Bash: world-cat.sh conventions/roblox-deliverable-audit.md`

## Inputs

- `deliverable`: What shipped (game-system name, world build, env). REQUIRED.
- `spec_ref` (optional): Prior audit or spec pointer. If omitted, Step 1 retrieves it.

## Step 1: Rubric From the Prior Audit, Not the Full Spec

```
Bash: retrieve.sh --category "roblox deliverable audit {deliverable}" --depth shallow
Locate the PRIOR completeness audit (tree node / experience / journal) and use IT
as the rubric — the prior audit already distilled the spec into checkable line
items. Re-reading the full multi-thousand-line spec wastes context and risks
drift from the agreed acceptance criteria. If NO prior audit exists, distill the
spec into a line-item rubric FIRST and encode it before walking the DataModel.
```

## Step 2: Re-Probe Bridge Liveness Per-Env at Point of Use

```
Bash: source core/scripts/_paths.sh && bash "$WORLD_DIR/scripts/roblox-studio.sh" status --env {env}
Also probe the /api/health endpoint at the moment of use — never trust a cached
liveness signal.
Gotchas (verify-before-assuming):
  - A stale team-state offline-streak is NOT current evidence of down.
  - A 404 on a guessed path is server-up-wrong-endpoint, not down.
  - Only connection-refused means the bridge is actually down.
```

## Step 3: Read-Only DataModel Walk for Structure

```
Bash: source core/scripts/_paths.sh && bash "$WORLD_DIR/scripts/roblox-studio.sh" query --env {env} <path> [--depth N]
Walk the DataModel READ-ONLY. Capture structure plus the ayoType / ayoKey /
ayoDescription attributes marking Ayo-managed instances. This proves the
deliverable's SHAPE (what instances exist, how they are tagged) — nothing more.
Never hand-roll raw bridge calls; the script is the read-only-enforced path.
```

## Step 4: Read the Source — a Script Named X Does Not Prove Behavior X

```
Bash: ... roblox-studio.sh query --env {env} <script-path> --include-properties Source
Grep the returned Source for the spec's code-behavior claims: action roster,
Success/Fail return contracts, validation rules, proximity-scan loop, etc.
A script NAMED ProximityScan does not prove a proximity scan runs — confirm the
behavior is implemented, not stubbed/named.
```

## Step 5: Verdict With the Static-vs-Runtime Caveat

```
Emit accept OR punch-list explicitly, per rubric line item.
ALWAYS attach: a read-only walk proves SHAPE, not that the deliverable RUNS —
a Play-mode game session is still required before final sign-off. Never sign
off on a read-only walk alone.
Post the verdict to the findings board (board-post.sh --channel findings
--type finding --tags "deliverable-audit,{deliverable}") and encode the audit
as the NEXT audit's rubric (Step 1 of the next cycle).
```

## Anti-patterns

- Re-reading the full spec instead of the prior audit's distilled rubric.
- Trusting a cached/team-state liveness signal instead of re-probing at point of use.
- Concluding "down" from a 404 or one stale signal (only connection-refused is down).
- Treating a script's NAME as proof of its behavior — read the Source.
- Signing off on a read-only walk without a Play-mode runtime session.

## Cross-references

- `world/conventions/roblox-deliverable-audit.md` — canonical procedure (this skill wraps it)
- `rb-1304` — method origin
- `world/scripts/roblox-studio.sh` — companion read-only bridge access
- skill-gap `gap-006` / registry row in `world/forged-skills.yaml`

## Return Protocol

See `.claude/rules/return-protocol.md` — terminate with a Bash tool call handing
control back to the orchestrator (e.g. `echo "Return to orchestrator — audit complete"`),
never with trailing text.
