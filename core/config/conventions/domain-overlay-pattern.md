# Domain-Overlay Pattern

**Audience:** anyone authoring or modifying a `core/scripts/*` script that
needs deployment-specific data (process types, category prefixes, routing
tables, agent name lists, anything that varies per host).

**Principle:** `core/` ships generic. `world/config/` ships domain. The
script reads BOTH and merges. A fresh deployment with an empty world overlay
gets safe behavior; the host deployment populates the overlay with its
domain-specific values.

This convention formalizes the pattern adopted in 2026-05-18 packaging plan
Phases 2.5–2.7 (capability_route, scaffolded_exploration, audit-applies-to,
work-class-mapping, stale-scanner, infra-health-categories).

## The two files

| Layer | Location | Purpose | Default |
|-------|----------|---------|---------|
| **Generic** | `core/config/<thing>.yaml` (when applicable) OR an in-script empty | Framework-universal defaults — schema + cross-domain entries (e.g. `framework-*` categories, methodology terms). Always shipped. | Safe-empty (no domain leakage). |
| **Overlay** | `world/config/<thing>.yaml` | Host deployment's domain-specific entries (e.g. `npc-*`, `roblox-*`, `processor-*`). Populated by the host; absent on fresh installs. | Empty stub seeded by `init-world.sh` with `[[ -f ]]` guard. |

When the consuming script reads the data:

1. Load the generic core entries (if any).
2. Load the overlay via `_world_config.load_world_config("<thing>", default={...})`.
3. Merge — **overlay overrides core per-key**. A deployment can both add
   new entries AND re-classify a core entry by repeating the key.
4. Use the merged result.

## How to read the overlay

Always go through the helper:

```python
from _world_config import load_world_config

cfg = load_world_config(
    "scaffolded-exploration",
    default={"product_category_prefixes": []},
)
prefixes = tuple(cfg.get("product_category_prefixes") or [])
```

The helper:
- Resolves `world/config/<name>.yaml` via `MIND_WORLD` env, the bound
  agent's `local-paths.conf`, any agent's `local-paths.conf`, or
  `PROJECT_ROOT/world` (in priority order).
- Returns a copy of `default` if the file is missing, empty, or malformed.
- Emits a stderr WARN on YAML parse error (so users notice broken overlays)
  but never crashes the caller.
- Caches per-process. Daemons that need to react to a hot overlay edit can
  call `clear_cache(name)`.

## How to add a new overlay

When you find yourself wanting to hardcode a domain-specific value in
`core/`:

1. **Decide on the merge story.** Pure-domain values (npc-cognition,
   roblox-bridge) live entirely in the overlay; the core ships an empty
   default and the merge is "core OR overlay = overlay-only". Mixed values
   (work-class-mapping: framework-universal entries in core, domain entries
   in overlay) ship core entries inline and let the overlay add/override
   per-key.
2. **Add the read path.** Use `_world_config.load_world_config(...)` with a
   safe-empty default; never raise on missing overlay.
3. **Update `init-world.sh`** to seed an empty stub at
   `world/config/<name>.yaml` with `[[ -f ]]` guard. The stub must include
   inline comments describing the schema so a host operator can populate it
   without reading the script.
4. **Document the new overlay in the script's module docstring** + add a row
   to the table above when you commit. Future authors hunting for "where
   does this domain knowledge live" find both the inventory + the read site.
5. **Seed the the framework overlay** in the SAME commit (or first follow-up). If
   you ship core/ with an empty default and leave the host overlay
   unpopulated, you've silently broken the live deployment. Always migrate
   the existing values to the overlay atomically with the core change.

## Active overlays (as of 2026-05-18)

| Overlay file | Consumer(s) | Pure-domain or merge? |
|--------------|-------------|------------------------|
| `world/config/capability-routing.yaml` | `gates/capability_route.py` | Pure-domain (overlay is the only source). |
| `world/config/scaffolded-exploration.yaml` | `gates/scaffolded_exploration.py` | Pure-domain. |
| `world/config/applies-to-rules.yaml` | `audit-applies-to.py` | Pure-domain (DOMAIN_PREFIXES). METHODOLOGY_TERMS stays in core. |
| `world/config/work-class-mapping.yaml` | `_work_class.py` | Merge: core has 47 framework-universal entries; overlay has 36 domain entries; merged total 83. |
| `world/config/stale-scanner.yaml` | `world/scripts/stale-jobs-scan.py` | Merge with hardcoded DEFAULT_THRESHOLDS fallback. |
| `world/config/infra-health-categories.yaml` | `infra-health.py` | Pure-domain. Falls back to legacy `core/config/aspirations.yaml.infra_health.component_categories` for one cycle (empty there now). |

## Anti-patterns

- **Hardcoding domain values in core** then "we'll move them later." Defer
  becomes permanent; future audits find the same cruft. Apply the pattern
  at write time.
- **Empty default in core that silently disables features.** A new
  deployment getting "scaffolded-exploration gate never fires" with no log
  warning is hard to debug. The helper's stderr WARN on parse error helps,
  but a deployment with a deliberately empty overlay still needs to know
  WHY behavior is "passive." Document the safe-default behavior in the
  script's module docstring.
- **Reading the overlay outside the helper.** Inlining `yaml.safe_load` on
  `world/config/<x>.yaml` skips the path-resolution priority, the parse-
  error warning, and the cache. Always go through `_world_config`.
- **Overlay-only critical entries.** If a class-of-thing is required for
  any deployment to function (e.g., the `framework` work-class), ship it
  in core. The overlay is for domain extensions, not required vocabulary.

## Cross-references

- `core/scripts/_world_config.py` — the helper module.
- `core/scripts/init-world.sh` lines 60-130 — the stub-seeding section
  (one `[[ -f ]] || cat >` per overlay).
- `PACKAGING-PLAN.md` Phases 2.5–2.7 — the migration that established this
  pattern. Read the "Resolution" sections to see how each overlay was
  carved out from the previous in-core hardcoded form.
- `world/conventions/capability-routing.md` (if your deployment has one) —
  the host-side companion documenting WHICH entries the deployment defines
  and WHY.
