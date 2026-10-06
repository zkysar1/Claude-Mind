#!/usr/bin/env python3
"""Aggregate per-skill invocation telemetry from <agent>/skill-invocations.jsonl.

Layer 2 of the skill-telemetry signal repair master plan. Read-only aggregator.
Joins all agents' invocation ledgers; the invocation->outcome join (g-355-06)
layers execution-diary + journal outcomes on top to score each invocation
success/failure/unknown by the enclosing-goal time-window (the Continual-Harness
skill lifecycle: skills are not just invocation-tracked but success/failure-scored,
so failing skills surface for reconsolidation — see skill-evaluate.py
`reconsolidation`).

Outputs per skill:
  total_invocations, model_invocations, user_invocations,
  agents_using, session_count, first_seen, last_seen, days_since_last
  (with --with-outcomes) success/failure/unknown invocations + success_rate

Knowledge tree: world/knowledge/tree/system/system-constraints-loop/skill-telemetry-signal-master-plan.md

Usage:
  python3 core/scripts/skill-attribution.py                  # text report
  python3 core/scripts/skill-attribution.py --json           # machine-readable
  python3 core/scripts/skill-attribution.py --agent bravo    # one agent only
  python3 core/scripts/skill-attribution.py --since 7d       # last 7 days
  python3 core/scripts/skill-attribution.py --skill /reflect # one skill drill-down
  python3 core/scripts/skill-attribution.py --silent-only    # never-seen skills
  python3 core/scripts/skill-attribution.py --with-outcomes  # per-skill success/failure join
  python3 core/scripts/skill-attribution.py --failing-invocations  # list failing invocations
"""
import os, sys, json, argparse, datetime, re
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
    """Return agent names (dirs under the agents-parent with a skill-invocations.jsonl).

    Two properties of this discovery:
      1. Routed through _paths.agents_root() — agent dirs live at
         PROJECT_ROOT/<AGENTS_PARENT_DIR>/<name> (currently agents/<name>), NOT at
         PROJECT_ROOT directly. A depth-1 PROJECT_ROOT scan (the pre-g-355-06
         form) silently found ZERO agents post-Phase-2.5.D relocation — the
         documented AGENTS_PARENT_DIR drift class (g-115-1405).
      2. Marker is the telemetry file itself, NOT local-paths.conf. The conf is
         gitignored + per-box, so on a synced multi-box fleet only the locally-
         bound agent has one — using it would collapse this CROSS-agent
         aggregator to a single agent on every box. Matching skill-discovery.py's
         data-file-glob pattern, discovery keys on the actual invocation ledger.
    """
    out = []
    agents_root = str(_paths.agents_root())
    if not os.path.isdir(agents_root):
        return out
    for entry in sorted(os.listdir(agents_root)):
        if os.path.isfile(os.path.join(agents_root, entry, 'skill-invocations.jsonl')):
            out.append(entry)
    return out


def read_invocations(agent_name, since_dt=None):
    """Read <agent>/skill-invocations.jsonl and return list of row dicts."""
    path = os.path.join(str(_paths.agents_root()), agent_name, 'skill-invocations.jsonl')
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


# ---------------------------------------------------------------------------
# Invocation -> outcome join ()
#
# Continual-Harness skill lifecycle: a reusable skill is not just invocation-
# tracked, it is success/failure-scored per invocation so failing skills can be
# surfaced for reconsolidation/debug. Each invocation is joined to the goal that
# was executing when it fired (enclosing time-window per agent), and that goal's
# outcome is resolved from the journal + execution-diary.
#
# Outcome sources (both are success-biased — they record COMPLETED goals):
#   - journal 'Outcome: deep|routine|durable' line              -> success
#   - execution-diary reaching 'phase-12-productivity' phase_end -> success (closed)
#   - a goal with a diary window that NEVER reached close and is
#     not the in-flight (last, open) goal                        -> failure, unless the
#     goal store says the goal is completed/decomposed             -> success ()
#   - journal 'Outcome: deferred'                                -> failure
#   - no attributable window / no signal                         -> unknown
# The never-closed failure is scored from the ABSENCE of a success signal on THIS box
# (diary and journal are per-box read-through caches), so it is the one branch the
# store may overrule; an explicit 'deferred' is evidence and stays a failure.
#
# Join key: agent + time-window. The invocation's sid scopes to the agent's
# ledger; the execution-diary is that agent's single append-only timeline, so
# sid-level disambiguation across concurrent same-agent sessions is a future
# refinement (the diary carries no sid field). Inter-goal invocations (precheck/
# select, fired between goals) attach to the preceding goal's window.
# ---------------------------------------------------------------------------

SUCCESS_OUTCOMES = {'deep', 'routine', 'durable'}
CLOSE_PHASE = 'phase-12-productivity'
# Terminal store statuses that are NOT a failure (): a goal the store says
# completed, or split into sub-goals, did not fail, whatever this box's diary shows.
COMPLETED_STATUSES = {'completed', 'decomposed'}
# Diary rows that may OPEN a goal window. Only these say "this agent is executing
# <goal_id>"; a finding/observation/decision row carries the id of the goal it is ABOUT,
# so letting it open a window splits the executing goal's window and mints a never-closed
# phantom window for the other goal ().
WINDOW_OPENING_ENTRY_TYPES = {'phase_start', 'phase_end', 'scorer_override'}
_GOAL_ID_RE = re.compile(r'^g-[0-9]+-[0-9]+$')
_GOAL_LINE_RE = re.compile(r'Goal:\s*\(?(g-[0-9]+-[0-9]+)\)?')
_OUTCOME_LINE_RE = re.compile(r'Outcome:\s*(\w+)')


def _canon_skill(sk):
    """Strip leading slash + args -> canonical skill key (mirrors aggregate())."""
    if not sk:
        return sk
    return sk.lstrip('/').split(None, 1)[0]


def read_execution_diary(agent_name):
    """Read <agent>/session/execution-diary.jsonl -> ts-sorted list of row dicts.

    Reads through the STORAGE BACKEND, not the local filesystem (g-115-4143).
    execution-diary.jsonl is `sync_tier: continuity` in session-manifest.yaml and
    is NOT machine-local, so under own-cloud the authoritative copy lives in S3
    and the local tree is a read-through cache. That cache is populated per-AGENT:
    owncloud-pull.sh is --agent-scoped and /start calls it for the bound agent
    only (--all-agents exists but is used solely by /open-questions, scoped to
    pending-questions.yaml). So a peer's diary is simply ABSENT on this box until
    something reads it, and an os.path.exists() gate silently returned [] for
    every agent but self — dropping their goal windows and with them the entire
    left side of the invocation->outcome join.

    The sibling ledger read above escapes this only because
    skill-invocations.jsonl sits OUTSIDE session/ and is synced by a different
    path; the two functions had identical code shape, so the asymmetry was in
    cache state, never in the read (measured cc-02 2026-07-31: invocations local
    for 5/5 agents, diaries local for 1/5 while all 5 were live in S3).

    The failure is per-box and therefore differently wrong on every box — cc-05
    read 2 of 5 agents, cc-02 read 1 of 5 — so its symptom (a near-zero
    classification rate) is not reproducible from another machine's numbers.

    A backend error other than absence is deliberately allowed to propagate: a
    silent [] on an S3 fault would re-create exactly the vacuous zero this fix
    removes (communication-clarity rule 5 — fail visibly, never degrade quietly).
    """
    from storage_backend import get_backend
    path = os.path.join(str(_paths.agents_root()), agent_name,
                        'session', 'execution-diary.jsonl')
    rows = []
    try:
        text = get_backend().read_text(path)
    except FileNotFoundError:
        return rows  # genuinely absent in the store of record
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda r: r.get('timestamp', ''))
    return rows


def read_journal_outcomes(agent_name):
    """Parse <agent>/journal/**/*.md -> {goal_id: outcome_word}. Last write wins.

    Journal blocks look like '## HH:MM — Goal: g-XXX (g-XXX)\\nOutcome: deep'.
    The outcome word is searched within the 200 chars following each goal
    mention (the journal only logs completed goals, so this is a success signal).
    """
    base = os.path.join(str(_paths.agents_root()), agent_name, 'journal')
    outcomes = {}
    if not os.path.isdir(base):
        return outcomes
    for root, _dirs, files in os.walk(base):
        for fn in sorted(files):
            if not fn.endswith('.md'):
                continue
            try:
                with open(os.path.join(root, fn), encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                continue
            for m in _GOAL_LINE_RE.finditer(text):
                gid = m.group(1)
                om = _OUTCOME_LINE_RE.search(text, m.end(), m.end() + 200)
                if om:
                    outcomes[gid] = om.group(1).lower()
    return outcomes


def build_goal_windows(diary_rows):
    """From ts-sorted diary rows, build [(goal_id, start_ts, end_ts)] intervals.

    Contiguous same-goal entries collapse into one run; a window ends where the
    NEXT distinct goal begins (the last window's end is None = open/in-flight).
    Only WINDOW_OPENING_ENTRY_TYPES rows keyed by a real goal id count: a row ABOUT
    another goal must not split the executing goal's window or open a phantom one, and
    a non-goal token ('precheck', 'none') is no goal to score (g-115-4215).
    """
    seq = [(r['goal_id'], r['timestamp']) for r in diary_rows
           if r.get('timestamp') and r.get('entry_type') in WINDOW_OPENING_ENTRY_TYPES
           and _GOAL_ID_RE.match(str(r.get('goal_id') or ''))]
    runs = []  # [goal_id, first_ts, last_ts]
    for gid, ts in seq:
        if runs and runs[-1][0] == gid:
            runs[-1][2] = ts
        else:
            runs.append([gid, ts, ts])
    windows = []
    for idx, (gid, first_ts, _last_ts) in enumerate(runs):
        end = runs[idx + 1][1] if idx + 1 < len(runs) else None
        windows.append((gid, first_ts, end))
    return windows


def _resolve_window_outcome(gid, start, end, is_last, journal_out, close_ts, goal_status=None):
    """success | failure | unknown for one goal window (see module join docstring).

    `goal_status` is the store's {goal_id: status} oracle (goal_status_map), or None."""
    jo = journal_out.get(gid)
    if jo in SUCCESS_OUTCOMES:
        return 'success'
    if jo == 'deferred':
        return 'failure'
    # closed if a phase-12-productivity phase_end timestamp falls in [start, end)
    for cts in close_ts:
        if cts >= start and (end is None or cts < end):
            return 'success'
    if is_last and end is None:
        return 'unknown'  # most-recent, open window — in-flight, not yet closed
    # No success signal on THIS box, and the store is the authority on whether the goal
    # completed: a goal a peer closed leaves no local evidence ( read-through
    # cache), so absence here is not a failure ().
    if goal_status and goal_status.get(gid) in COMPLETED_STATUSES:
        return 'success'
    return 'failure'      # window exists, no success signal, not in-flight


def _locate_invocation(ts, win_outcomes):
    """Return (outcome, goal_id) for the goal window containing ts, else ('unknown', None)."""
    for gid, start, end, outcome in win_outcomes:
        if ts >= start and (end is None or ts < end):
            return outcome, gid
    return 'unknown', None


def read_goals():
    """Read every goal the store holds -> (goals, errors).

    Both queues (world, agent), live AND archive, plus the census of EVICTED ids: a
    terminal goal leaves its aspiration after aspirations_eviction.age_days and survives
    only as a bare id under archived_census.evicted_ids (keyed by status), and a goal in
    a completed aspiration is absent from every live read (guard-1555) -- so a live-only
    lookup reports "not found" for goals that provably completed (g-353-108). Records are
    slimmed to what the join and the reconsolidation dedup consume.

    A failed read is RETURNED in `errors`, never swallowed: an empty oracle renders
    identically to a healthy one that had nothing to clear (guard-3992), so the caller
    must be able to tell them apart. An undecodable body still aborts (guard-383).
    """
    import _rt  # noqa: E402 -- lazy: only the outcome/reconsolidation paths need the daemon
    import _goal_census  # noqa: E402
    goals, errors = [], []
    for source in ('world', 'agent'):
        for archive in (False, True):
            label = '%s/%s' % (source, 'archive' if archive else 'live')
            try:
                raw = _rt.aspirations_read(source=source, active=not archive, archive=archive)
            except Exception as e:  # noqa: BLE001 -- recorded in errors, not raised
                errors.append('%s: %s' % (label, e))
                continue
            data = _rt.tolerant_decode_aggregate('skill-attribution goals: %s' % label, raw)
            for asp in (data.get('aspirations') if isinstance(data, dict) else data) or []:
                for g in asp.get('goals') or []:
                    gid = g.get('id') or g.get('goal_id')
                    if gid:
                        goals.append({
                            'id': gid, 'status': g.get('status'), 'source': source,
                            'origin_signal': g.get('origin_signal'),
                            'completed_at': g.get('completed_at') or g.get('completed_date'),
                        })
                for status, ids in _goal_census.census_evicted_ids(asp).items():
                    goals.extend({'id': gid, 'status': status, 'source': source,
                                  'evicted': True} for gid in ids)
    return goals, errors


def goal_status_map(goals):
    """{goal_id: status} from read_goals() records. An id held with two different
    statuses (a cross-queue id collision; 7 of 7131 ids measured 2026-10-04) is dropped:
    ambiguous is not evidence, and the join leaves that window as it was."""
    seen = defaultdict(set)
    for g in goals:
        if g.get('status'):
            seen[g['id']].add(g['status'])
    return {gid: next(iter(st)) for gid, st in seen.items() if len(st) == 1}


def compute_join(agents, since_dt=None, goal_status=None):
    """Join invocations to enclosing-goal outcomes across `agents`.

    `goal_status` is the goal store's {goal_id: status} oracle (read_goals +
    goal_status_map); None means no oracle, so a window's outcome rests on this box's
    diary and journal alone.

    Returns {'per_skill': {skill: {success, failure, unknown, classified,
    success_rate}}, 'failing': [{skill, goal_id, ts, agent}], 'diary_coverage': {...},
    'goal_status_check': {...}}.
    """
    per_skill = defaultdict(lambda: {'success': 0, 'failure': 0, 'unknown': 0})
    failing = []
    coverage = {}
    windows_cleared = failure_windows = failure_windows_unknown = 0
    for ag in agents:
        invs = read_invocations(ag, since_dt=since_dt)
        if not invs:
            continue
        diary = read_execution_diary(ag)
        windows = build_goal_windows(diary)
        journal_out = read_journal_outcomes(ag)
        close_ts = sorted(r['timestamp'] for r in diary
                          if r.get('entry_type') == 'phase_end'
                          and r.get('phase') == CLOSE_PHASE and r.get('timestamp'))
        win_outcomes = []
        for idx, (gid, start, end) in enumerate(windows):
            is_last = idx == len(windows) - 1
            outcome = _resolve_window_outcome(gid, start, end, is_last,
                                              journal_out, close_ts, goal_status)
            if goal_status is not None:
                # Carry the control (guard-3992): how many windows the store cleared, and
                # how many failures it could NOT check, so a blind oracle is visible.
                if outcome == 'failure':
                    failure_windows += 1
                    failure_windows_unknown += gid not in goal_status
                elif _resolve_window_outcome(gid, start, end, is_last,
                                             journal_out, close_ts) == 'failure':
                    windows_cleared += 1
            win_outcomes.append((gid, start, end, outcome))
        # Diary coverage ( part 2): the two ledgers have ASYMMETRIC
        # retention — skill-invocations.jsonl is append-only across months, while
        # execution-diary.jsonl is session-scoped (~8h). An invocation outside every
        # window has no goal to attribute to and can only ever be 'unknown', so the
        # classifiable CEILING is the in-span count, not the invocation total.
        # Reported so a caller cannot read the resulting low rate as a join defect:
        # --since/'all_time' describes the INVOCATION side only and overstates the
        # window side. Measured cc-02 2026-07-31: 190 of 14788 invocations (1.28%)
        # fell inside any diary span, and the join classified exactly 190 — i.e.
        # 100% of what was structurally classifiable.
        ts_all = sorted(r.get('ts', '') for r in invs if r.get('ts'))
        d_ts = [r['timestamp'] for r in diary if r.get('timestamp')]
        d_first, d_last = (d_ts[0], d_ts[-1]) if d_ts else (None, None)
        in_span = sum(1 for t in ts_all if d_first and d_first <= t <= d_last)
        coverage[ag] = {
            'diary_first': d_first, 'diary_last': d_last,
            'diary_windows': len(windows),
            'invocations': len(ts_all), 'invocations_in_diary_span': in_span,
        }
        for r in invs:
            ts = r.get('ts', '')
            sk = _canon_skill(r.get('skill', ''))
            if not sk or not ts:
                continue
            cls, gid = _locate_invocation(ts, win_outcomes)
            per_skill[sk][cls] += 1
            if cls == 'failure':
                failing.append({'skill': sk, 'goal_id': gid, 'ts': ts, 'agent': ag})
    out = {}
    for sk, c in per_skill.items():
        classified = c['success'] + c['failure']
        out[sk] = {
            'success': c['success'],
            'failure': c['failure'],
            'unknown': c['unknown'],
            'classified': classified,
            'success_rate': round(c['success'] / classified, 4) if classified else None,
        }
    failing.sort(key=lambda f: f['ts'])
    tot_inv = sum(c['invocations'] for c in coverage.values())
    tot_span = sum(c['invocations_in_diary_span'] for c in coverage.values())
    # classified_invocations is the COUNT the join actually classified (success +
    # failure). classifiable_ceiling counts span membership while classification
    # counts WINDOW membership, so once one agent's span decouples from its windows
    # the ceiling stops being an upper bound (, zeta 2026-08-20: 6574
    # classified against a stated ceiling of 2023). Emit both so a reader compares them.
    tot_classified = sum(v['classified'] for v in out.values())
    return {
        'per_skill': out,
        'failing': failing,
        'diary_coverage': {
            'per_agent': coverage,
            'invocations': tot_inv,
            'classifiable_ceiling': tot_span,
            'ceiling_ratio': round(tot_span / tot_inv, 4) if tot_inv else None,
            'classified_invocations': tot_classified,
        },
        'goal_status_check': {
            'oracle_goals': len(goal_status) if goal_status is not None else None,
            'windows_cleared': windows_cleared,
            'failure_windows': failure_windows,
            'failure_windows_status_unknown': failure_windows_unknown,
        },
    }


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
    ap.add_argument('--with-outcomes', action='store_true',
                    help='Join invocations to enclosing-goal outcomes (success/failure/unknown)')
    ap.add_argument('--failing-invocations', action='store_true',
                    help='List invocations whose enclosing goal FAILED (for reconsolidation review)')
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

    # Invocation -> outcome join (opt-in; heavier — reads diaries + journals)
    join = None
    if args.with_outcomes or args.failing_invocations:
        goals, goal_errors = read_goals()
        for err in goal_errors:
            print('[skill-attribution] goal store read failed: %s' % err, file=sys.stderr)
        join = compute_join(agents, since_dt=since_dt, goal_status=goal_status_map(goals))
        # Fold per-skill outcome counts into stats for the report paths
        for sk, oc in join['per_skill'].items():
            if sk in stats:
                stats[sk].update({
                    'success_invocations': oc['success'],
                    'failure_invocations': oc['failure'],
                    'unknown_invocations_outcome': oc['unknown'],
                    'classified_invocations': oc['classified'],
                    'success_rate': oc['success_rate'],
                })

    # Failing-invocations mode (feeds skill-evaluate reconsolidation review)
    if args.failing_invocations:
        failing = join['failing']
        by_skill = defaultdict(int)
        for f in failing:
            by_skill[f['skill']] += 1
        if args.json:
            print(json.dumps({
                'failing_count': len(failing),
                'by_skill': dict(sorted(by_skill.items(), key=lambda kv: -kv[1])),
                'failing': failing,
                'window_since': args.since or 'all_time',
                'agents_scanned': agents,
                # compute_join measures how much of the invocation stream the
                # local diaries can classify at all. Emitted because the
                # strategic-scan S4.6 marker instructs the reader to print
                # ceiling_ratio beside the candidate count -- a near-zero ratio
                # means the run is a COVERAGE measurement, not a skill-quality
                # one. It was computed and discarded on every CLI path until
                # 2026-08-16, so that instruction named a field no command
                # emitted (guard-2046: a step naming both a command and a
                # capture list is an unverified pairing).
                'diary_coverage': join['diary_coverage'],
                'goal_status_check': join['goal_status_check'],
            }, indent=2, default=str))
        else:
            print(f"=== failing invocations ({len(failing)}) ===")
            print(f"  window: {args.since or 'all_time'}")
            for sk, n in sorted(by_skill.items(), key=lambda kv: -kv[1]):
                print(f"    {sk:30}  failing={n}")
            print(f"\n  recent failing invocations:")
            for f in failing[-20:]:
                print(f"    [{f['ts']}] {f['skill']:24} goal={f['goal_id']} agent={f['agent']}")
        return 0

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
            line = (f"    {sk:30}  total={s['total_invocations']:4}  "
                    f"model={s['model_invocations']:3} user={s['user_invocations']:3}  "
                    f"agents={','.join(s['agents_using']):15}  "
                    f"last={s['last_seen'] or '-'} ({s['days_since_last']}d ago)")
            if args.with_outcomes and 'success_rate' in s:
                sr = s['success_rate']
                line += (f"  | ok={s['success_invocations']} fail={s['failure_invocations']} "
                         f"rate={sr if sr is not None else '-'}")
            print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
