#!/usr/bin/env python3
"""Rewrite .claude/skills/_tree.yaml from the family-based FAMILY_MAP.

Single-shot restructure script. Reads:
  - core/scripts/_skill_families.py (FAMILY_MAP — authorial source of truth)
  - .claude/skills/*/SKILL.md (front matter for per-leaf metadata)
  - world/forged-skills.yaml (forged skills list — filtered OUT of this tree)

Writes:
  - .claude/skills/_tree.yaml (with all families + leaves; comments stripped)

Validation:
  - Every classified skill in FAMILY_MAP must have a SKILL.md on disk
  - Every core SKILL.md on disk must appear somewhere in FAMILY_MAP (else WARN)
  - Forged skills excluded from both directions (they live in world registry)

Usage:
  python3 core/scripts/skill-tree-restructure.py           # dry-run
  python3 core/scripts/skill-tree-restructure.py --apply   # write _tree.yaml
"""
import os, sys, re, argparse

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML not available\n"); sys.exit(2)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
SKILLS_DIR = os.path.join(PROJECT_ROOT, '.claude', 'skills')
TREE_YAML = os.path.join(SKILLS_DIR, '_tree.yaml')

sys.path.insert(0, SCRIPT_DIR)
import _skill_families as fam  # noqa: E402

try:
    import _paths  # noqa: E402
    WORLD_DIR_RESOLVED = str(_paths.WORLD_DIR)
except Exception:
    WORLD_DIR_RESOLVED = None


def parse_front_matter(text):
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def scan_disk():
    """Return {skill_name: {'fm': dict, 'rel_path': str}} for every SKILL.md."""
    disk = {}
    if not os.path.isdir(SKILLS_DIR):
        return disk
    for entry in sorted(os.listdir(SKILLS_DIR)):
        sk_path = os.path.join(SKILLS_DIR, entry, 'SKILL.md')
        if not os.path.isfile(sk_path):
            continue
        with open(sk_path, encoding='utf-8') as f:
            txt = f.read()
        fm = parse_front_matter(txt)
        disk[entry] = {
            'fm': fm,
            'rel_path': f'.claude/skills/{entry}/SKILL.md',
        }
    return disk


def load_forged():
    if not WORLD_DIR_RESOLVED:
        return set()
    forged_path = os.path.join(WORLD_DIR_RESOLVED, 'forged-skills.yaml')
    if not os.path.exists(forged_path):
        return set()
    try:
        with open(forged_path, encoding='utf-8') as f:
            fd = yaml.safe_load(f) or {}
        return set((fd.get('skills') or {}).keys())
    except Exception:
        return set()


def infer_type(name, fm, is_family):
    """Heuristic type assignment, matching the existing _tree.yaml vocabulary."""
    if is_family:
        return 'family'
    if name in ('start', 'stop'):
        return 'control'
    if name == 'verify-learning':
        return 'control'
    if name == 'aspirations':
        return 'orchestrator'
    if name.startswith('aspirations-'):
        return 'orchestrator-sub'
    if name == 'reflect':
        return 'learning'
    if name.startswith('reflect-') or name == 'replay':
        return 'learning'
    if name.startswith('fresh-eyes-'):
        return 'review-ritual'
    if name in ('encode-session', 'felt-sense-checkin', 'strategic-pulse'):
        return 'review-ritual'
    if name in ('boot', 'respond'):
        return 'system'
    if name == 'curriculum-gates':
        return 'system'
    if name in ('tree', 'tree-reader', 'prime'):
        return 'memory'
    if name in ('agent-completion-report', 'backlog-report', 'priority-review', 'open-questions'):
        return 'reporting'
    if name in ('decompose', 'create-aspiration'):
        return 'planning'
    if name == 'forge-skill':
        return 'meta'
    if name == 'research-topic':
        return 'research'
    if name == 'review-hypotheses':
        return 'review'
    if name == 'verify-learning-staleness':
        return 'audit'
    return 'utility'


def build_tree(disk, forged):
    """Construct the new _tree.yaml dict from FAMILY_MAP + disk metadata.

    Returns (tree_dict, issues_list).
    """
    issues = []
    tree = {
        'config': {
            'max_skills': 100,
            'developmental_gate': 'EXPLOIT',
            'control_skills': ['start', 'stop', 'verify-learning'],
            'structure': 'family-based (see core/scripts/_skill_families.py)',
            'forged_skills_registry': 'world/forged-skills.yaml',
            'last_restructured': '2026-05-12',
        },
    }

    classified = fam.all_classified_skills()
    families = fam.all_families()

    # 1. Emit family entries
    for fname, finfo in fam.FAMILY_MAP.items():
        parent = finfo.get('parent')
        tree[fname] = {
            'file': None,  # families have no SKILL.md
            'parent': parent,
            'children': list(finfo.get('children', [])),
            'type': 'family',
            'summary': finfo.get('summary', ''),
            'user-invocable': False,
            'status': 'active',
        }

    # 2. Emit leaf (skill) entries from disk, filtered by forged
    core_disk = {n: info for n, info in disk.items() if n not in forged}

    for name, info in sorted(core_disk.items()):
        parent = fam.parent_of(name)
        if parent is None:
            issues.append(f"UNCLASSIFIED: {name} (SKILL.md exists but no family in FAMILY_MAP)")
            # Still emit it as a root-level entry so it appears
            parent = None
        fm = info['fm']
        tree[name] = {
            'file': info['rel_path'],
            'parent': parent,
            'children': [],
            'type': infer_type(name, fm, is_family=False),
            'user-invocable': bool(fm.get('user-invocable')) if fm.get('user-invocable') is not None else False,
            'minimum_mode': fm.get('minimum_mode'),
            'status': 'active',
        }

    # 3. Verify every classified skill in FAMILY_MAP has a SKILL.md
    for sname in classified:
        if sname not in disk:
            issues.append(f"PHANTOM: {sname} (classified in FAMILY_MAP but no SKILL.md on disk)")

    # 4. Verify forged skills are NOT in FAMILY_MAP (would be duplication)
    for sname in classified:
        if sname in forged:
            issues.append(f"DUPLICATION: {sname} (in FAMILY_MAP and also forged — should be world registry only)")

    return tree, issues


def write_tree(tree):
    header = (
        "# Skill Tree Index — family-based structure (regenerated by skill-tree-restructure.py)\n"
        "# Source of truth for family/parent assignments: core/scripts/_skill_families.py\n"
        "# Comments from prior versions of this file are NOT preserved on rewrite.\n"
        "# Forged skills (registered in world/forged-skills.yaml) are NOT listed here.\n"
        "#\n"
        "# To restructure: edit core/scripts/_skill_families.py and re-run\n"
        "#   python3 core/scripts/skill-tree-restructure.py --apply\n\n"
    )
    with open(TREE_YAML, 'w', encoding='utf-8') as f:
        f.write(header)
        yaml.safe_dump(tree, f, default_flow_style=False, sort_keys=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='Rewrite .claude/skills/_tree.yaml with the new structure')
    args = ap.parse_args()

    disk = scan_disk()
    forged = load_forged()
    tree, issues = build_tree(disk, forged)

    # Stats
    families = [k for k, v in tree.items() if k != 'config' and v.get('type') == 'family']
    leaves = [k for k, v in tree.items() if k != 'config' and v.get('type') != 'family']
    print(f"=== skill-tree-restructure ===")
    print(f"  disk_skills={len(disk)}  forged={len(forged)}  core={len(disk)-len(forged)}")
    print(f"  families={len(families)}  leaves={len(leaves)}")
    print(f"  classified_in_FAMILY_MAP={len(fam.all_classified_skills())}")
    if issues:
        print(f"\nISSUES ({len(issues)}):")
        for i in issues:
            print(f"  ! {i}")
    else:
        print(f"  validation: OK")

    # Show top-level structure
    print(f"\nProposed top-level structure:")
    for fname in [k for k, v in tree.items() if k != 'config' and v.get('parent') is None and v.get('type') == 'family']:
        finfo = tree[fname]
        ch_count = len(finfo.get('children', []))
        print(f"  {fname:24} children={ch_count}  {finfo.get('summary','')[:60]}")
        for child in finfo.get('children', []):
            ctype = tree.get(child, {}).get('type', '?')
            if ctype == 'family':
                grand = tree[child].get('children', [])
                print(f"    └ {child:22} ({ctype}, {len(grand)} children)")
            else:
                print(f"    └ {child}")

    if args.apply:
        if issues:
            print(f"\nRefusing --apply: {len(issues)} validation issue(s) above. Fix _skill_families.py first.")
            return 1
        write_tree(tree)
        print(f"\nAPPLIED: wrote {TREE_YAML}")
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
