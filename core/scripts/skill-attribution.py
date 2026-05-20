#!/usr/bin/env python3
"""Aggregate per-skill invocation telemetry from <agent>/skill-invocations.jsonl.

MVP — Layer 2 of the skill-telemetry signal repair master plan. Read-only
aggregator. Joins (currently): all agents' invocation ledgers. Future joins
(when data accumulates): execution-diary, journal outcomes, stop-hook-log turn
duration.

Outputs per skill:
  total_invocations, model_invocations, user_invocations,
  agents_using, session_count, first_seen, last_seen, days_since_last

Knowledge tree: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md

Usage:
  python3 core/scripts/skill-attribution.py                  # text report
  python3 core/scripts/skill-attribution.py --json           # machine-readable
  python3 core/scripts/skill-attribution.py --agent bravo    # one agent only
  python3 core/scripts/skill-attribution.py --since 7d       # last 7 days
  python3 core/scripts/skill-attribution.py --skill /reflect # one skill drill-down
  python3 core/scripts/skill-attribution.py --silent-only    # never-seen skills
"""
import os, sys, json, argparse, datetime
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

try:
    import yaml
except ImportError:
    yaml = None

import _paths  # noqa: E402


def find_agent_dirs():
    """Return list of agent directories (each containing a local-paths.conf)."""
    out = []
    for entry in sorted(os.listdir(PROJECT_ROOT)):
        agent_path = os.path.join(PROJECT_ROOT, entry)
        if not os.path.isdir(agent_path):
            continue
        if os.path.isfile(os.path.join(agent_path, 'local-paths.conf')):
            out.append(entry)
    return out


def read_invocations(agent_name, since_dt=None):
    """Read <agent>/skill-invocations.jsonl and return list of row dicts."""
    path = os.path.join(PROJECT_ROOT, agent_name, 'skill-invocations.jsonl')
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_dt:
                try:
                    ts = datetime.datetime.fromisoformat(row.get('ts', ''))
                except ValueError:
                    continue
                if ts < since_dt:
                    continue
            rows.append(row)
    return rows


def known_skills():
    """Return the set of canonical skill names from disk + forged registry."""
    names = set()
    skills_dir = os.path.join(PROJECT_ROOT, '.claude', 'skills')
    if os.path.isdir(skills_dir):
        for entry in os.listdir(skills_dir):
            if os.path.isfile(os.path.join(skills_dir, entry, 'SKILL.md')):
                names.add(entry)
    if yaml is not None:
        forged_path = os.path.join(str(_paths.WORLD_DIR), 'forged-skills.yaml')
        if os.path.exists(forged_path):
            try:
                with open(forged_path, encoding='utf-8') as f:
                    fd = yaml.safe_load(f) or {}
                names.update((fd.get('skills') or {}).keys())
            except Exception:
                pass
    return names


def aggregate(rows):
    """Group rows by skill and emit per-skill metrics."""
    by_skill = defaultdict(lambda: {
        'total_invocations': 0,
        'model_invocations': 0,
        'user_invocations': 0,
        'unknown_invocations': 0,
        'agents_using': set(),
        'sessions': set(),
        'timestamps': [],
    })
    for r in rows:
        sk = r.get('skill', '')
        if not sk:
            continue
        # Strip leading slash for canonical key (mirrors skill-quality-score
        # canonicalization)
        sk = sk.lstrip('/').split(None, 1)[0] if sk else sk
        if not sk:
            continue
        d = by_skill[sk]
        d['total_invocations'] += 1
        src = r.get('invocation_source', 'unknown')
        if src == 'model':
            d['model_invocations'] += 1
        elif src == 'user':
            d['user_invocations'] += 1
        else:
            d['unknown_invocations'] += 1
        ag = r.get('agent', '')
        if ag:
            d['agents_using'].add(ag)
        sid = r.get('sid', '')
        if sid:
            d['sessions'].add(sid)
        ts = r.get('ts', '')
        if ts:
            d['timestamps'].append(ts)
    # finalize
    now = datetime.datetime.now()
    out = {}
    for sk, d in by_skill.items():
        ts_sorted = sorted(d['timestamps'])
        first = ts_sorted[0] if ts_sorted else None
        last = ts_sorted[-1] if ts_sorted else None
        days_since = None
        if last:
            try:
                last_dt = datetime.datetime.fromisoformat(last)
                days_since = round((now - last_dt).total_seconds() / 86400.0, 2)
            except ValueError:
                pass
        out[sk] = {
            'total_invocations': d['total_invocations'],
            'model_invocations': d['model_invocations'],
            'user_invocations': d['user_invocations'],
            'unknown_invocations': d['unknown_invocations'],
            'agents_using': sorted(d['agents_using']),
            'session_count': len(d['sessions']),
            'first_seen': first,
            'last_seen': last,
            'days_since_last': days_since,
        }
    return out


def parse_since(s):
    """Parse --since: '7d', '24h', '30m', ISO date, or empty."""
    if not s:
        return None
    s = s.strip().lower()
    now = datetime.datetime.now()
    if s.endswith('d'):
        return now - datetime.timedelta(days=int(s[:-1]))
    if s.endswith('h'):
        return now - datetime.timedelta(hours=int(s[:-1]))
    if s.endswith('m'):
        return now - datetime.timedelta(minutes=int(s[:-1]))
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        sys.stderr.write(f"unparseable --since value: {s!r}\n")
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--agent', help='Limit to one agent name (else all)')
    ap.add_argument('--since', default='', help='Time window: 7d, 24h, 30m, or ISO date')
    ap.add_argument('--skill', help='Drill into one skill (raw "/foo" or canonical "foo")')
    ap.add_argument('--silent-only', action='store_true',
                    help='List known skills with ZERO invocations (in window)')
    ap.add_argument('--json', action='store_true', help='Machine-readable output')
    args = ap.parse_args()

    since_dt = parse_since(args.since)
    agents = [args.agent] if args.agent else find_agent_dirs()
    all_rows = []
    per_agent_rows = {}
    for ag in agents:
        rs = read_invocations(ag, since_dt=since_dt)
        per_agent_rows[ag] = rs
        all_rows.extend(rs)

    stats = aggregate(all_rows)

    # Skill drill-down
    if args.skill:
        sk_canon = args.skill.lstrip('/').split(None, 1)[0]
        s = stats.get(sk_canon)
        out = {'skill': sk_canon, 'stats': s, 'rows': [r for r in all_rows
               if r.get('skill', '').lstrip('/').split(None, 1)[0] == sk_canon]}
        if args.json:
            print(json.dumps(out, indent=2, default=str))
        else:
            print(f"=== skill: {sk_canon} ===")
            if not s:
                print("  no invocations in window")
            else:
                for k, v in s.items():
                    print(f"  {k}: {v}")
                print(f"\n  rows ({len(out['rows'])}):")
                for r in out['rows'][-20:]:
                    print(f"    [{r.get('ts','?')}] agent={r.get('agent','?')} "
                          f"src={r.get('invocation_source','?')} sid={r.get('sid','?')[:8]}")
        return 0

    # silent-only mode
    if args.silent_only:
        all_known = known_skills()
        seen = set(stats.keys())
        silent = sorted(all_known - seen)
        if args.json:
            print(json.dumps({'silent_count': len(silent), 'silent': silent,
                              'window_since': args.since or 'all_time',
                              'known_total': len(all_known)}, indent=2))
        else:
            print(f"=== silent-only ({len(silent)} of {len(all_known)} known skills) ===")
            print(f"  window: {args.since or 'all_time'}")
            for sk in silent:
                print(f"  - {sk}")
        return 0

    # default — full report
    total_rows = len(all_rows)
    distinct_skills = len(stats)
    if args.json:
        print(json.dumps({
            'total_rows': total_rows,
            'distinct_skills': distinct_skills,
            'agents_scanned': agents,
            'window_since': args.since or 'all_time',
            'per_skill': stats,
        }, indent=2, default=str))
    else:
        print(f"=== skill-attribution ===")
        print(f"  agents: {', '.join(agents)}")
        print(f"  window: {args.since or 'all_time'}")
        print(f"  rows: {total_rows}  distinct_skills: {distinct_skills}")
        if total_rows == 0:
            print(f"  (ledger empty — invocations begin populating from the next "
                  f"PreToolUse[Skill] or UserPromptExpansion hook fire)")
            return 0
        print(f"\n  Top skills by invocation count:")
        ranked = sorted(stats.items(), key=lambda kv: -kv[1]['total_invocations'])
        for sk, s in ranked[:20]:
            print(f"    {sk:30}  total={s['total_invocations']:4}  "
                  f"model={s['model_invocations']:3} user={s['user_invocations']:3}  "
                  f"agents={','.join(s['agents_using']):15}  "
                  f"last={s['last_seen'] or '-'} ({s['days_since_last']}d ago)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
