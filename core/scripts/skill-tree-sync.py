#!/usr/bin/env python3
"""Reconcile .claude/skills/_tree.yaml against actual SKILL.md files on disk.

The skills registry at .claude/skills/_tree.yaml is meant to mirror every core
SKILL.md that exists on disk. Forged skills (registered in
world/forged-skills.yaml) are excluded — they live in the world registry, not
the framework one. As of 2026-05-12, _tree.yaml lists 17 entries while
.claude/skills/ has 46 core SKILL.md files — 29 are silently missing.

This script:
  1. Walks .claude/skills/*/SKILL.md, parses each front matter
  2. Loads forged-skills.yaml to filter out forged names
  3. Loads .claude/skills/_tree.yaml
  4. Categorizes:
     - MISSING: SKILL.md on disk, not in _tree.yaml, not forged
     - ORPHAN:  in _tree.yaml, no SKILL.md on disk
     - DRIFT:   in both, but file path or user-invocable flag differs
  5. By default, prints a human-readable report (dry-run)
  6. With --apply, writes MISSING entries into _tree.yaml (additive only)
     — never auto-deletes orphans; those need human review

Caveat: --apply uses PyYAML which does NOT preserve comments. The existing
_tree.yaml has section-header comments ("# --- Control Skills ---", etc.)
that will be stripped. Review before --apply if comment loss matters.

Usage:
  python3 core/scripts/skill-tree-sync.py            # dry-run report
  python3 core/scripts/skill-tree-sync.py --json     # JSON output for tooling
  python3 core/scripts/skill-tree-sync.py --apply    # additive writes
"""
import os, sys, re, json, argparse

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML not available\n"); sys.exit(2)

# Resolve paths — same convention as _paths.py
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
SKILLS_DIR = os.path.join(PROJECT_ROOT, '.claude', 'skills')
TREE_YAML = os.path.join(SKILLS_DIR, '_tree.yaml')

# Import _paths.py for WORLD_DIR resolution (handles external path config).
# Subprocess approach via bash had issues with env propagation on Windows.
sys.path.insert(0, SCRIPT_DIR)
try:
    import _paths  # noqa: E402
    WORLD_DIR_RESOLVED = str(_paths.WORLD_DIR)
except Exception as _e:
    WORLD_DIR_RESOLVED = None
    _paths_error = str(_e)
else:
    _paths_error = None

# Family map — authoritative source for skill parents (2026-05-12 restructure).
import _skill_families as fam  # noqa: E402


def parse_front_matter(text):
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def infer_parent(name):
    """Parent assignment for missing entries — sourced from FAMILY_MAP.

    Returns None if the skill is not classified anywhere in FAMILY_MAP
    (indicates the skill is unknown and should be added to _skill_families.py).
    """
    return fam.parent_of(name)


def infer_type(name, fm):
    """Heuristic type assignment. Existing types in _tree.yaml: control,
    orchestrator, review, learning, research, reporting, planning, system,
    meta, routing.
    """
    if name in ('start', 'stop'):
        return 'control'
    if name == 'aspirations':
        return 'orchestrator'
    if name.startswith('aspirations-'):
        return 'orchestrator-sub'
    if name.startswith('reflect'):
        return 'learning'
    if name == 'replay':
        return 'learning'
    if name.startswith('fresh-eyes-'):
        return 'review-ritual'
    if name in ('boot', 'prime', 'respond'):
        return 'system'
    if name == 'tree' or name == 'tree-reader':
        return 'system'
    if 'report' in name or name in ('priority-review', 'open-questions'):
        return 'reporting'
    if name in ('encode-session', 'felt-sense-checkin', 'strategic-pulse'):
        return 'review-ritual'
    if name == 'create-aspiration':
        return 'planning'
    if name == 'decompose':
        return 'planning'
    if name == 'forge-skill':
        return 'meta'
    if name == 'verify-learning' or name == 'verify-learning-staleness':
        return 'control' if name == 'verify-learning' else 'audit'
    if name == 'curriculum-gates':
        return 'system'
    if name in ('research-topic',):
        return 'research'
    return 'utility'


def load_forged():
    if not WORLD_DIR_RESOLVED:
        return set(), f"_paths.py import failed — forged filter disabled ({_paths_error})"
    forged_path = os.path.join(WORLD_DIR_RESOLVED, 'forged-skills.yaml')
    if not os.path.exists(forged_path):
        return set(), f"forged-skills.yaml not found at {forged_path}"
    try:
        with open(forged_path, encoding='utf-8') as f:
            fd = yaml.safe_load(f) or {}
        return set((fd.get('skills') or {}).keys()), None
    except Exception as e:
        return set(), f"failed to load forged-skills.yaml: {e}"


def scan_disk():
    """Return {skill_name: {'fm': dict, 'path': str}} for every SKILL.md."""
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
            'path': f'.claude/skills/{entry}/SKILL.md',
        }
    return disk


def load_tree():
    with open(TREE_YAML, encoding='utf-8') as f:
        tree = yaml.safe_load(f) or {}
    return tree


def diff(tree, disk, forged):
    tree_keys = {k for k in tree.keys() if k != 'config'}
    missing, orphan, drift = [], [], []

    for name, info in disk.items():
        if name in forged:
            continue  # tracked in world/forged-skills.yaml
        if name not in tree_keys:
            ui = info['fm'].get('user-invocable')
            missing.append({
                'name': name,
                'user-invocable': bool(ui) if ui is not None else False,
                'minimum_mode': info['fm'].get('minimum_mode'),
                'parent_proposed': infer_parent(name),
                'type_proposed': infer_type(name, info['fm']),
                'file': info['path'],
            })
        else:
            existing = tree[name]
            ui_disk = info['fm'].get('user-invocable')
            ui_tree = existing.get('user-invocable')
            if existing.get('file') and existing['file'] != info['path']:
                drift.append({'name': name, 'reason': 'file_path',
                               'tree': existing.get('file'), 'disk': info['path']})
            elif ui_disk is not None and ui_tree is not None and bool(ui_disk) != bool(ui_tree):
                drift.append({'name': name, 'reason': 'user-invocable',
                               'tree': ui_tree, 'disk': ui_disk})

    family_names = fam.all_families()
    for name in tree_keys:
        if name in forged:
            continue  # forged shouldn't be in this tree at all
        if name in family_names:
            continue  # families are organizational, no SKILL.md expected
        if name not in disk:
            orphan.append({'name': name, 'tree_entry': tree[name]})

    return missing, orphan, drift


def apply_missing(tree, missing):
    """Add missing entries to tree dict (in-memory). Updates parent children."""
    for m in missing:
        tree[m['name']] = {
            'file': m['file'],
            'parent': m['parent_proposed'],
            'children': [],
            'type': m['type_proposed'],
            'user-invocable': m['user-invocable'],
            'status': 'active',
        }
    for m in missing:
        p = m['parent_proposed']
        if p and p in tree:
            ch = tree[p].setdefault('children', [])
            if m['name'] not in ch:
                ch.append(m['name'])


def write_tree(tree):
    with open(TREE_YAML, 'w', encoding='utf-8') as f:
        f.write("# Skill Tree Index (rewritten by skill-tree-sync.py)\n")
        f.write("# Comments from prior versions are NOT preserved — re-add manually if needed.\n\n")
        yaml.safe_dump(tree, f, default_flow_style=False, sort_keys=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='Write MISSING entries to _tree.yaml (additive only)')
    ap.add_argument('--json', action='store_true',
                    help='Output JSON report instead of human-readable text')
    args = ap.parse_args()

    forged, forged_warn = load_forged()
    disk = scan_disk()
    tree = load_tree()
    missing, orphan, drift = diff(tree, disk, forged)

    tree_keys = sorted([k for k in tree.keys() if k != 'config'])
    report = {
        'counts': {
            'tree_entries': len(tree_keys),
            'disk_skills': len(disk),
            'forged_in_world': len(forged),
            'missing': len(missing),
            'orphan': len(orphan),
            'drift': len(drift),
        },
        'missing': missing,
        'orphan': orphan,
        'drift': drift,
    }
    if forged_warn:
        report['warnings'] = [forged_warn]

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        c = report['counts']
        print(f"=== skill-tree-sync ===")
        print(f"  tree_entries={c['tree_entries']}  disk_skills={c['disk_skills']}  "
              f"forged={c['forged_in_world']}")
        print(f"  missing={c['missing']}  orphan={c['orphan']}  drift={c['drift']}")
        if forged_warn:
            print(f"  warn: {forged_warn}")
        if missing:
            print(f"\nMISSING ({len(missing)}) — on disk, not in _tree.yaml:")
            for m in missing:
                p = m['parent_proposed'] or 'null'
                print(f"  + {m['name']:33}  parent={p:18} type={m['type_proposed']:18} "
                      f"ui={m['user-invocable']}")
        if drift:
            print(f"\nDRIFT ({len(drift)}):")
            for d in drift:
                print(f"  ! {d['name']}: {d['reason']}  tree={d.get('tree')!r}  disk={d.get('disk')!r}")
        if orphan:
            print(f"\nORPHAN ({len(orphan)}) — in _tree.yaml, no SKILL.md on disk:")
            for o in orphan:
                print(f"  ? {o['name']}")

    if args.apply and missing:
        apply_missing(tree, missing)
        write_tree(tree)
        print(f"\nAPPLIED: {len(missing)} entries added to {TREE_YAML}")
        print("(Comments may have been stripped — review and re-add section headers if needed)")
    elif args.apply:
        print(f"\nNo missing entries to apply.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
