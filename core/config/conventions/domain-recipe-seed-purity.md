# Domain Recipes and Seed Purity (D1)

How a domain-specific upgrade recipe coexists with the domain-free promotion
seed in the cross-world versioning rails.

**Decision (locked 2026-06-05): A — domain-specific upgrade recipes are
`domain-leak-exempt` and travel in the seed.** Settled when the first
domain-touching upgrade recipe became the first recipe carrying *functional*
domain tokens (a storage-provider SDK import plus named cloud resources) and
tripped the seed publishability gate (`seed-preflight` domain-token sweep).
Motivating instance: a cloud-resource rename release — see cross-references.

## The tension

The cross-world rails copy all of `core/` — including
`core/config/upgrade-recipes/` — wholesale into the promotion seed
(`seed-manifest.yaml` `include: core/`). The recipe *mechanism* (`release.sh`,
`_release_lib.py`, the recipe templates) is domain-free framework. But a specific
recipe *instance* (e.g. a cloud-resource rename) is domain content. The question:
where do domain recipes live, and how do they pass the domain-free seed gate?

## Resolution: A (durable)

Domain recipes stay in `core/config/upgrade-recipes/`, carry the
`domain-leak-exempt:` marker, and travel in the seed. **The seed IS the recipe
transport** — the same mechanism the first cross-world release used to reach a
downstream deployment. Options B (exclude recipes from the seed) and C (relocate
recipes to `world/`) both break that transport: a downstream world needs the
recipe to run its own migration, and a recipe in `world/` does not travel with
the framework version.

## Invariants

1. **Location.** Domain upgrade recipes live in `core/config/upgrade-recipes/`,
   never `world/`. The seed carries them downstream.
2. **Marker.** They carry `domain-leak-exempt: <rationale>` after the shebang.
   This is the sanctioned mechanism (`.claude/rules/domain-free-examples.md`
   § "Marker Restriction" — executable code with *functional* domain strings: a
   migration recipe imports the storage provider's SDK and operates on named
   cloud resources). `domain-leak-check.sh` honors the marker per-file.
3. **FROM-state guard.** They check their FROM state and are **inert where
   inapplicable** — no-op or refuse when the expected starting state is not
   present. This makes a recipe safe to seed onto a deployment where it does not
   apply (already migrated, a different backend, or a world that does not use the
   renamed resource).
4. **cross_world ⇒ H3b.** `cross_world: true` recipes additionally carry H3b
   snapshot/restore markers (snapshot `$WORLD_PATH`/`$META_PATH` before mutation;
   restore from the snapshot in the rollback, in executable code) — already
   enforced by `validate_recipe_structure(cross_world=True)`.
5. **Gate scope.** `upgrade-recipes/*.sh` is outside the scope of both
   `marker-placement-gate.py` (polices `.claude/skills/*/SKILL.md` +
   `core/config/conventions/*.md` only) and `domain-leak-check.sh`'s
   misplaced-marker scan (`.claude/skills` + `core/config/conventions` only). A
   recipe's marker is therefore *never* flagged misplaced, and a recipe path does
   **not** belong in either gate's ALLOWLIST (those hold exact in-scope file
   paths). The marker is honored by the recursive domain-token sweep — the
   correct and only interaction.

   This convention file is a different case: it *documents* the marker token in
   invariant 2 and lands in `core/config/conventions/`, which IS in scope, so its
   path IS allowlisted in both gates (`marker-placement-gate.py` ALLOWLIST set +
   `domain-leak-check.sh` ALLOWLIST array, kept in sync) — exactly like
   `learning-routing.md`. The distinction: convention/SKILL files that *document*
   the marker are allowlisted; recipe `.sh` files that *functionally use* it are
   simply out of scope. Per the domain-free doctrine, this file's body describes
   the mechanism generically and does not itself lean on the marker.

## Seed-down semantics

When a domain recipe reaches a downstream deployment via the seed, it runs
**per-env-id** (operating only on that `ENVIRONMENT_ID`'s namespace) and relies
on its FROM-state guard (invariant 3) to be inert where the migration does not
apply. A downstream world on a different backend, or already past the rename,
gets a safe no-op.

## Cross-references

- `world/reasoning-bank.jsonl` → `rb-1462` — the discovery that produced this convention.
- `.claude/rules/domain-free-examples.md` § "Marker Restriction" — the marker's sanctioned use; conventions describe mechanisms generically and do not lean on the marker.
- `core/scripts/_release_lib.py::validate_recipe_structure` — H3b snapshot/restore enforcement.
- `core/config/seed-manifest.yaml` — `include: core/` (the recipe transport into the seed).
- `core/config/upgrade-recipes/v1.0.0-n3-mind-prefix.sh` — recipe-travels-in-seed precedent (a domain-free recipe).
- `core/config/upgrade-recipes/v2.0.0-n4-aws-resource-names.sh` — the motivating instance: first domain recipe (cloud-resource rename, v2.0.0).
