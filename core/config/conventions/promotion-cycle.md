# Promotion Cycle (Genome-Model Zone Governance)

How framework improvements flow between deployment instances of this framework
(a **dev source** and one or more **downstream prod** deployments) along the
cross-world versioning rails. This convention codifies the **zone governance**:
which classes of change may promote, in which direction, under what gate.

User-sanctioned program (decision 2026-07-21, recorded in seed goal
`g-115-2863`): *"I do eventually want full bidirectional... go as far as you can
with what you have while staying confident."* The zones below are the
end-state target; the **Current Status** table names what is live today.

## The Three Zones

Every framework path classifies into exactly one promotion zone. The classifier
is `core/scripts/promotion-preflight.py::classify_zone` (the machine
diff-classifier splits PHENOTYPE parametric-vs-structural; KERNEL and NICHE are
path-inherent). See also the knowledge-tree node
`world/knowledge/tree/system/publication-pipeline.md`.

| Zone | What it covers | Promotion rule |
|------|----------------|----------------|
| **KERNEL** | The constitutional anchor (`.claude/settings.local.json`, `settings-structural-validator.{py,sh}`) **and the promotion mechanism itself** (`.claude/skills/seed/`, `core/config/seed-templates/`). | **HUMAN-LOCKED, DOWN-ONLY, FOREVER.** Never reconciles up. This invariant is what makes every other flow safe — the mechanism that governs promotion cannot itself be promoted by an agent. |
| **PHENOTYPE — parametric** | Value/threshold **config DATA** (numeric bounds, tuning knobs) in skills/rules/scripts/config/CLAUDE.md. | **Bidirectional.** May originate in ANY deployment and reconcile UP the chain, gated by regression + `verify-learning`. Down-flow unchanged (the seed carries it). |
| **PHENOTYPE — structural** | **Logic** in skills/rules/scripts/docs (control flow, new functions, prose that changes behavior). | **Bidirectional WITH REVIEW.** May reconcile UP, but only through an explicit review gate (a structural change is not auto-merged the way a parametric tweak is). |
| **NICHE** | `world/`, `meta/`, `agents/` — domain and per-agent data. | **Never promoted, either direction.** Domain data is deployment-local by definition. (Exception: `core/config/upgrade-recipes/` domain recipes travel *in the seed* — see `domain-recipe-seed-purity.md`; they live in `core/`, not `world/`, precisely so they are KERNEL-transport, not NICHE.) |

## Direction Semantics

- **DOWN** (dev source → downstream prod): the original seed flow. All zones
  except NICHE flow down via the seed (`seed-manifest.yaml include: core/`).
  KERNEL flows down and ONLY down.
- **UP** (downstream prod → dev source): the bidirectional addition. Only
  PHENOTYPE reconciles up. Parametric up-flow is gated by regression +
  verify-learning; structural up-flow additionally requires review. A
  downstream deployment that validates a KERNEL-class pattern does **not**
  implement it locally — it routes the *pattern* up for formalization at the
  dev source (no KERNEL code originates downstream). See
  `fleet-secret-provisioning.md` § Promotion for the canonical example of
  up-routing a validated pattern without promoting mechanism code.

## Why prod-leads-dev is real (motivation)

Down-only is not guaranteed to be the steady state: IF a downstream prod
originates improvements, they must be reconcilable up or they strand.
Bidirectional PHENOTYPE flow is what keeps the dev source the true superset.
(Historical note: an "18 target-ahead / ~30 prod-parked `g-001-*` fixes"
backlog was cited 2026-06-24, but the 2026-07-22 re-measurement (`g-115-2908`)
found NO genuine backlog — the current downstream (Claude-Mind) is a clean
sync-mirror whose "target-ahead" files are 100% seed-transform artifacts
(AYOAI↔MIND rebrand + goal-id strips), not prod-origin fixes. The mechanism
stands for future divergence; there is nothing to drain today.)

## Current Status (2026-07-22)

| Capability | Status |
|------------|--------|
| Zone-classification report (preflight column) | **Live** (Phase 0, `g-115-2864`) |
| Parametric up-reconcile lane, one cycle piloted | **Live** (Phase 1, `g-115-2867`) |
| Machine parametric-vs-structural diff-classifier (enforcement) | **Live** (Phase 2, `g-115-2865`) |
| Structural-with-review up-reconcile mechanism | **Live** (Phase 3b, `g-115-2907` — zone-partition labeling + review-gate routing in `promotion-preflight.py`) |
| Prod-parked backlog drained up through the lane | **Complete-as-noop** (Phase 3c, `g-115-2908`, verified 2026-07-22) — re-measured by gh-cloning the downstream (Claude-Mind is PUBLIC + gh-clonable, so the earlier "no cross-box access" premise was itself wrong): the downstream is a clean sync-mirror with ZERO prod-origin improvements; all 582 "target-ahead" files are seed-transform artifacts. No backlog exists to drain; draining would CORRUPT the dev source (namespace rebrand + goal-id strips). |

The structural-with-review up-reconcile mechanism is **live** (Phase 3b,
`g-115-2907`): `promotion-preflight.py` labels PHENOTYPE-structural changes and
routes them to the review gate. Phase 3c (`g-115-2908`) — draining the backlog —
was executed as a re-measurement on 2026-07-22 and found the backlog EMPTY: the
downstream (Claude-Mind) is a clean sync-mirror of the dev source (its entire
47-commit history is "sync framework" / "promote from frontier"; even its lone
cross-applicable-looking fix — the stop-hook `$PY` launcher resolver — is
labelled "promoted from Ayoai `g-115-2205`" and already present in the dev
source). Its 582 "target-ahead" files are 100% seed-transform artifacts, not
prod-origin improvements. Both drain tools (`promotion-content-diff.sh`,
`promotion-preflight.py`) need `--source <downstream-clone> --target <dev>`; the
clone IS obtainable (`gh repo clone zkysar1/Claude-Mind` — Claude-Mind is
PUBLIC), which corrected the earlier "no cross-box access" premise — but with the
clone in hand there was nothing genuine to reconcile up. The mechanism stands
ready for any FUTURE downstream-origin improvement; today Phase 3c is
complete-as-noop.

## Enforcing Guardrails

- **`guard-97`** — Framework-capability changes originate at the dev source and
  flow DOWN; a downstream prod that validates a pattern routes it UP for
  formalization rather than building it locally (dev-origination for
  mechanism/KERNEL; PHENOTYPE parametric may originate anywhere and reconcile
  up under gate).
- **`guard-98`** — KERNEL never reconciles up (human-locked, down-only,
  forever); NICHE never promotes either direction.

## Cross-references

- `world/knowledge/tree/system/publication-pipeline.md` — the pipeline model +
  the "Planned Evolution — Zone-Aware Bidirectional Promotion" section this
  convention formalizes.
- `core/scripts/promotion-preflight.py` — `classify_zone` (the zone classifier)
  + the diff-classifier enforcement.
- `core/config/conventions/domain-recipe-seed-purity.md` — why domain recipes
  live in `core/` and travel in the seed (KERNEL-transport, not NICHE).
- `core/config/conventions/fleet-secret-provisioning.md` § Promotion — the
  canonical up-routing-a-pattern example.
- `core/config/conventions/constitutional-rings.md` — the KERNEL anchor's
  three-ring governance (Ring 1 immutable = the human-locked KERNEL).
- Seed goal `g-115-2863` — the user-sanctioned program seed + verbatim decision.
