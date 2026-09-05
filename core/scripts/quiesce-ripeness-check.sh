#!/usr/bin/env bash
# Quiesce-window ripeness check ().
#
# world/conventions/fleet-quiesce-window.md says ripeness "is evaluated on a
# recurring cadence so the SYSTEM surfaces 'the window is ripe' rather than the
# user having to ask", and names a host sub-check. Nothing implemented it. This
# is that implementation; the scoring lives in core/scripts/quiesce_ripeness.py
# (pure, unit-tested) and this wrapper only gathers inputs for it.
#
#   bash core/scripts/quiesce-ripeness-check.sh            # human summary
#   bash core/scripts/quiesce-ripeness-check.sh --json     # machine payload
#   bash core/scripts/quiesce-ripeness-check.sh --quiet    # emit ONLY when ripe
#
# EXIT CODES ARE THE CONTRACT, and 0/1 are deliberately NOT pass/fail:
#   0 = ripe (GO)      1 = not ripe (HOLD)      2 = could not evaluate
# A caller must never read exit 1 as an error. The convention's design is that a
# hold emits nothing, so HOLD is the ordinary, healthy, overwhelmingly-common
# outcome. Conflating it with 2 is what would put a spurious red in the host
# sweep every 24h until someone stopped reading the host sweep at all.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null || true
cd .. 2>/dev/null || true
REPO="$(pwd)"
# shellcheck disable=SC1091
source "$REPO/core/scripts/_paths.sh" || { echo "quiesce-ripeness: cannot source _paths.sh" >&2; exit 2; }

MODE="human"
UPDATE="0"
for a in "$@"; do
  case "$a" in
    --json)  MODE="json" ;;
    --quiet) MODE="quiet" ;;
    --update-verdict) UPDATE="1" ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
  esac
done

CONV="$WORLD_PATH/conventions/fleet-quiesce-window.md"
[ -f "$CONV" ] || { echo "quiesce-ripeness: convention not found: $CONV" >&2; exit 2; }

TMP="$(agent_dir "${MIND_AGENT:-bravo}")/temp/quiesce-ripeness"
mkdir -p "$TMP" || { echo "quiesce-ripeness: cannot create $TMP" >&2; exit 2; }

# ---- 1. goal ids named by manifest rows -----------------------------------
# Parsing is the pure module's job, so the id list and the later scoring can
# never disagree about which rows exist.
# stderr is NOT suppressed here and the rc IS checked. Suppressing it was a real
# defect in this script's first version (found by the fresh-eyes probe the same
# day): a silent parse failure yields an empty id list, which yields an empty
# status map, under which every stale row counts as ready -- 5 rows/95 min became
# 7 rows/115 min with BOTH stale rows hidden, still printing GO. The module now
# refuses to score an all-empty status map, but the error must also be VISIBLE
# rather than inferred from a downstream refusal.
#
# `| tr -d '\r'` BELOW IS LOAD-BEARING ON WINDOWS AND MUST NOT BE DROPPED
# (found 2026-09-02, quiesce window). Python's stdout is a TEXT stream, so on
# Windows `print()` emits CRLF; the shell captures "\r\n\r\n"
# and word-splits into ids that each keep a trailing CR. Every downstream
# `aspirations-query.sh --goal-field id $'\r'` then matches nothing,
# the status map comes back empty, and this script exits 2 CANNOT-EVALUATE --
# which on this box it had done since it was written. It is invisible from an
# interactive shell (a literal id works) and doubly hidden by the `2>/dev/null`
# on the per-goal query plus the bare `except: arr = []` below, so the only way
# to SEE it is `bash -x` (which renders the argument as $'...\r'); a repr of the
# value INSIDE Python looks clean, because the CR is added by the stdout
# translation and is not carried in the string. Linux Bodies are unaffected,
# which is why no other box ever surfaced it.
IDS="$(PYTHONPATH="$REPO/core/scripts" py -3 -c "
import sys
from quiesce_ripeness import parse_rows
md = open(sys.argv[1], encoding='utf-8').read()
print('\n'.join(sorted({r['goal_id'] for r in parse_rows(md) if r['goal_id']})))
" "$CONV" | tr -d '\r')" || { echo "quiesce-ripeness: manifest parse failed (see stderr above)" >&2; exit 2; }

# ---- 2. live status + priority for each named goal -------------------------
# Per-goal lookups, bounded by manifest size (~10). Deliberately NOT derived by
# absence from the non-terminal dumps in step 3: absence there cannot separate
# "completed" from "unreadable", and those two must not collapse -- one means a
# stale row, the other means unknown, and the evaluator treats them oppositely.
: > "$TMP/statuses.jsonl"
for gid in $IDS; do
  bash "$REPO/core/scripts/aspirations-query.sh" --goal-field id "$gid" --full 2>/dev/null \
    | PYTHONPATH="$REPO/core/scripts" py -3 -c "
import json,sys
try: arr = json.load(sys.stdin)
except Exception: arr = []
if arr:
    g = arr[0]
    print(json.dumps({'goal_id': g.get('goal_id'), 'status': g.get('status'), 'priority': g.get('priority')}))
" >> "$TMP/statuses.jsonl"
done

# ---- 3. goals frozen waiting on a quiesced window --------------------------
# SUBSTRING scan over every non-terminal goal, NOT the exact-match query, and
# not the structured prefix the convention names. Measured 2026-08-13 (bravo,
# cc-05): `--goal-field defer_reason <v>` is EXACT-match -- proven with a
# positive control that round-tripped a known defer verbatim and returned 1,
# while the same query on a substring of it returned 0. And the ONE goal in the
# fleet actually frozen on this window (, which is manifest row Q1)
# carries `human_blocked: requires a fleet-quiesced window ...` -- prose, not
# `precondition_unmet:fleet_quiesced_window`. So a literal implementation of the
# convention's criterion (b) matches zero forever while the real population is
# one, and it would report that as a healthy hold. Cost is ~13MB / ~1.3s per
# run; at a 24h cadence that is the right trade for not being structurally blind.
#
# WHICH substrings is NOT decided here -- quiesce_ripeness.QUIESCE_DEFER_TOKENS
# owns it (, 2026-09-05). This block used to inline `'quiesce' in ...`,
# and a substring predicate is only as good as the vocabulary it tracks: when
# the master quiet-window goal standardised its members on a
# `human_blocked: quiet-window member of <master>` prefix, the inline test kept
# matching a word that prefix does not contain, and went blind to 3 of 5 frozen
# goals -- including a HIGH member whose own defer read `WINDOW-READY: YES`.
for s in pending blocked in-progress; do
  bash "$REPO/core/scripts/aspirations-query.sh" --goal-status "$s" --full 2>/dev/null > "$TMP/q-$s.json"
done

# ---- 4. score -------------------------------------------------------------
PYTHONPATH="$REPO/core/scripts" py -3 -c "
import json, sys
from quiesce_ripeness import evaluate, is_quiesce_frozen_defer

conv, tmp, mode = sys.argv[1], sys.argv[2], sys.argv[3]
md = open(conv, encoding='utf-8').read()

status = {}
for line in open(tmp + '/statuses.jsonl', encoding='utf-8'):
    line = line.strip()
    if not line: continue
    r = json.loads(line)
    if r.get('goal_id'):
        status[r['goal_id']] = {'status': r.get('status'), 'priority': r.get('priority')}

deferred, scanned = [], 0
for s in ('pending', 'blocked', 'in-progress'):
    try: arr = json.load(open(tmp + '/q-%s.json' % s, encoding='utf-8'))
    except Exception: continue
    scanned += len(arr)
    for g in arr:
        # The predicate lives in quiesce_ripeness.QUIESCE_DEFER_TOKENS -- do NOT
        # re-inline a substring test here. This line WAS \`'quiesce' in ...\`
        # until 2026-09-05, and it went blind when the registration vocabulary
        # moved to 'quiet-window member of <master>' on 2026-08-23 ().
        if is_quiesce_frozen_defer(g.get('defer_reason')):
            deferred.append(g['goal_id'])

res = evaluate(md, status, deferred)
res['defer_scan'] = {'non_terminal_scanned': scanned, 'quiesce_frozen': deferred}

# ---- stamp: written on GO **and** HOLD -------------------------------------
# The hold case is the load-bearing one. Skipping it would restore the exact
# ambiguity this wiring closed: a never-wired evaluator and a standing hold both
# leave the file untouched, and nothing tells them apart.
if len(sys.argv) > 4 and sys.argv[4] == '1':
    import datetime
    from quiesce_ripeness import apply_stamp, render_stamp
    now = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    updated = apply_stamp(md, render_stamp(res, now))
    if updated is None:
        res['stamp'] = 'SENTINEL ABSENT -- verdict line not written (checker refuses to create its own write target)'
    elif updated == md:
        res['stamp'] = 'unchanged'
    else:
        try:
            import _fileops
            _fileops.durable_write_text(conv, updated)
        except Exception as e:  # never let a stamp failure change the verdict
            res['stamp'] = 'write failed: %s' % e
        else:
            res['stamp'] = 'written'
# A defer scan that read NOTHING must not be reported as 'no frozen goals'. Both
# produce an empty list, and only one of them is evidence (rb-245).
if scanned == 0:
    res['defer_scan']['warning'] = 'scan read 0 goals -- criterion (b) defer leg is UNEVALUATED, not clean'

if mode == 'json':
    print(json.dumps(res, indent=2))
elif mode == 'quiet':
    if res['verdict'] == 'GO':
        print('QUIESCE WINDOW RIPE: %s' % res['reason'])
        for r in res['ready']:
            print('  %s %s (%s min)' % (r['qid'], r['goal_id'] or '-', r['est_minutes']))
else:
    c = res['counts']
    print('quiesce ripeness: %s' % res['verdict'])
    print('  reason      : %s' % res['reason'])
    print('  ready       : %d row(s), %d min (floor %d)' % (c['ready'], res['total_ready_minutes'], res['batch_floor_minutes']))
    print('  rows parsed : %d (tombstoned %d, not-ready %d, stale-row %d)'
          % (c['rows_parsed'], c['tombstoned'], c['not_ready'], c['stale_row']))
    for r in res['ready']:
        print('    READY %s %s %s min %s' % (r['qid'], r['goal_id'] or '-', r['est_minutes'], r['live_status'] or ''))
    for r in res['stale_row']:
        print('    STALE %s %s -- manifest says ready, goal is %s' % (r['qid'], r['goal_id'], r['live_status']))
    for r in res['unscoreable_estimate']:
        print('    NO-EST %s %s -- %r' % (r['qid'], r['goal_id'], r['est_raw']))
    if deferred:
        print('  quiesce-frozen goals: %s' % ', '.join(deferred))

if mode != 'json' and 'stamp' in res and res['stamp'] not in ('written', 'unchanged'):
    print('  stamp       : %s' % res['stamp'], file=sys.stderr)

# A defer-scan that read NOTHING must be LOUD in every mode, not only in the JSON
# payload. --quiet is the mode the recurring lane runs in, so a warning visible
# only in --json is a warning nobody sees.
w = res.get('defer_scan', {}).get('warning')
if w:
    print('  defer-scan  : %s' % w, file=sys.stderr)

if res['verdict'] == 'CANNOT-EVALUATE':
    print('quiesce-ripeness: CANNOT EVALUATE -- %s' % res['reason'], file=sys.stderr)
    sys.exit(2)
sys.exit(0 if res['verdict'] == 'GO' else 1)
" "$CONV" "$TMP" "$MODE" "$UPDATE"
exit $?
