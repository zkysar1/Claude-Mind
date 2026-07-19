#!/usr/bin/env python3
"""Initialize meta/ directory from core/config/meta.yaml initial_state.

Reads the initial_state section and writes each key to its corresponding file.
Key name mapping: underscores in key → hyphens in filename.

--missing-only (g-115-2524 backfill mode): write ONLY targets that do not
already exist — never overwrite an evolved live strategy file. Under
STORAGE_BACKEND=own-cloud, a locally-missing target is additionally probed
against the store of record via _init_seed_probe (local absence is not
authoritative under the read-through cache; guard-980 class).
"""

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR, CONFIG_DIR

# Key → file mapping. Underscores become hyphens.
# Special cases: nested paths, markdown files, JSONL.
FILE_MAP = {
    "goal_selection_strategy": "goal-selection-strategy.yaml",
    "reflection_strategy": "reflection-strategy.yaml",
    "evolution_strategy": "evolution-strategy.yaml",
    "aspiration_generation_strategy": "aspiration-generation-strategy.yaml",
    "encoding_strategy": "encoding-strategy.yaml",
    "skill_quality_strategy": "skill-quality-strategy.yaml",
    "improvement_instructions": "improvement-instructions.md",
    "meta_state": "meta.yaml",
    "improvement_velocity": "improvement-velocity.yaml",
    "active_experiments": "experiments/active-experiments.yaml",
    "completed_experiments": "experiments/completed-experiments.yaml",
    "transfer_profile": "transfer-profile.yaml",
    # transfer/_index.yaml is created directly by init-meta.sh
    # AutoContext-inspired subsystems
    "backpressure": "backpressure.yaml",
    "credit_assignment": "credit-assignment.yaml",
    "strategy_generations": "strategy-generations.yaml",
    # dead-ends.jsonl is created directly by init-meta.sh (JSONL, not YAML)
}


def main():
    missing_only = "--missing-only" in sys.argv[1:]
    config_path = CONFIG_DIR / "meta.yaml"
    if not config_path.exists():
        print(f"ERROR: {config_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    initial_state = config.get("initial_state", {})
    if not initial_state:
        print("ERROR: No initial_state found in meta.yaml", file=sys.stderr)
        sys.exit(1)

    for key, data in initial_state.items():
        if key not in FILE_MAP:
            print(f"  WARNING: Unknown initial_state key '{key}' — skipping", file=sys.stderr)
            continue

        filename = FILE_MAP[key]
        target = META_DIR / filename

        if missing_only:
            # Additive backfill (4): never overwrite an existing
            # (potentially agent-evolved) strategy file.
            if target.exists():
                continue
            if os.environ.get("STORAGE_BACKEND") == "own-cloud":
                # Local absence is not authoritative under the read-through
                # cache — consult the store of record (safe direction on
                # probe failure is DO NOT SEED).
                from _init_seed_probe import probe_path
                verdict = probe_path(target)
                if verdict != "seed":
                    print(f"  backfill: {filename} {verdict} — not seeding")
                    continue

        # Ensure parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        if filename.endswith(".md"):
            # Markdown files: write as plain text
            with open(target, "w", encoding="utf-8") as f:
                f.write(str(data))
        else:
            # YAML files: write as YAML
            with open(target, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        print(f"  Seeded {filename} from initial_state.{key}")


if __name__ == "__main__":
    main()
