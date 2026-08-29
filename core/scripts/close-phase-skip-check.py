#!/usr/bin/env python3
"""close-phase-skip-check.py — the I/O half of the close-phase skip check.

Gathers the population and the two oracles, hands them to the pure
`close_phase_skip.decide()`, and renders. Every read lives here; every decision
lives there (g-115-8219).

  Bash: py -3 core/scripts/close-phase-skip-check.py [--json] [--limit N]

Exit codes -- a DETECTOR, so a finding is not an error:
  0  ran (clean, findings, or not-applicable) — read `status` for the verdict
  2  could not run (no agent/SID binding, store unreadable)

The predicate it calls is `loop-state-bump-counters.py --verify-counted-many`,
the batch twin added for this sweep (g-115-8219). It answers N membership
questions from ONE read: measured 1.19s for 25 goals against 27.7s for the same
25 through the single-goal mode's one-process-per-goal shape — a 23x difference
that decides whether a loop-entry lane is affordable at all.

Batch was worth adding for a second reason beyond speed. The single-goal mode
collapses "counted" and "indeterminate" into rc 0 (deliberate upstream: a torn
WM read must not trigger a spurious re-fire), so a caller using it CANNOT see a
torn read — an unreadable WM would arrive as health, and this check would report
a clean sweep for exactly the reason it exists to catch, one level up. The batch
mode reports `indeterminate` as its own field, so that blind spot is closed
rather than merely documented.

Agreement between the two modes was verified per-id before switching, not
assumed: 4 ids, batch counted/absent matching single-goal rc 0/0/1/1 exactly.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import close_phase_skip as cps  # noqa: E402

_BUMP = _HERE / "loop-state-bump-counters.py"
# The window is deliberately SMALL. The actionable question this lane answers is
# "did the close that just happened land?" -- an uncounted MOST-RECENT close is
# repairable right now (zeta repaired 186->187 by hand, commit 4516c06f); older
# ones are historical and mostly not. It also matches the shape the goal itself
# proposed: "comparing the LAST CLOSED GOAL ID against the counter's last
# advance". Measured on the incident session (zeta, SID bde2c353, 118 closes):
# 11 were uncounted, so an unbounded window would report 11 standing findings on
# EVERY iteration -- a lane people learn to ignore, which is worse than no lane.
# `--limit` opens the backlog view for anyone who wants it, and `bound_excluded`
# always reports what the window hid (never a silent top-N).
_DEFAULT_LIMIT = 5


def _role_signals(agent, sid, project_root):
    """(role, signals) — TWO independent signals, OR'd, and both reported.

    `BODY_ROLE` is the env var the PreToolUse bash hook injects (the predicate
    iteration-open.py, precheck-medium-battery.py and agent-watchdog.py all
    read). The forked-WM file is the predicate worker-loop Phase -0 and
    capture_fast_lane.is_worker_body use. Neither is reimplemented here; both
    are one-line reads of established signals.

    OR'd because each fails toward "reducer" when its signal is simply missing
    (an unset env var, an unreadable dir), and a missed worker is the noisy
    direction: the check would then fire on every one of that worker's closes.
    Both are reported so a wrong not-applicable is diagnosable instead of
    silent (guard-1922: a check whose substrate is unreadable must say so).
    """
    by_env = (os.environ.get("BODY_ROLE") or "").strip().lower() == "worker"
    by_wm = False
    if agent and sid:
        try:
            by_wm = (project_root / "agents" / agent / "sessions" / sid
                     / "working-memory.yaml").is_file()
        except OSError:
            by_wm = False
    return ("worker" if (by_env or by_wm) else "reducer",
            {"body_role_env": by_env, "forked_wm_file": by_wm})


def _closed_this_session(store_path, sid, limit, agent):
    """Goals this SESSION closed, most recent first, bounded by `limit`.

    Scoped by `completed_by_sid` because counted_goals_this_session is
    session-scoped: comparing a wider population against it would flag goals the
    list never claimed to contain. Returns (population, total_matching) so the
    bound is REPORTED and never silent (a top-N that hides its remainder reads
    as "covered everything" when it did not).
    """
    total = 0
    rows = []
    foreign = 0
    with store_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # one torn line must not blind the whole sweep
            for g in (rec.get("goals") or []):
                if not isinstance(g, dict) or g.get("completed_by_sid") != sid:
                    continue
                # Scope by AGENT as well as SID. The membership oracle resolves
                # the WM as THIS agent (wm.wm_path()), so a population closed by
                # a DIFFERENT agent would be compared against the wrong counted
                # list and every row would read uncounted -- a confident, wholly
                # wrong "N of N skipped". Measured while validating this check:
                # pointed at another agent's SID it reported 118 of 118 against a
                # true value of 11. The hook injects MIND_AGENT and MIND_SID
                # together so this should not arise in production, but a checker
                # that answers confidently from mismatched stores is the exact
                # defect class this whole lane exists to detect.
                if agent and g.get("completed_by") and g.get("completed_by") != agent:
                    foreign += 1
                    continue
                total += 1
                rows.append({"id": g.get("id"),
                             "completed_at": g.get("completed_at") or ""})
    rows.sort(key=lambda r: r["completed_at"], reverse=True)
    return rows[:limit], total, foreign


def _bump_failures(agent, project_root):
    """goal_ids in loop-state-bump-failures.jsonl — the ledger that discriminates
    'the bump no-op'd' from 'the phase never ran'.

    ONE writer (iteration-close.sh:2113) and, before this script, ZERO readers.
    Absent/empty is the normal case and means no bump has been OBSERVED to
    no-op — not that none could.
    """
    p = project_root / "agents" / agent / "session" / "loop-state-bump-failures.jsonl"
    out = set()
    if not p.is_file():
        return out
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "bump_noop_detected" and rec.get("goal_id"):
                out.add(rec["goal_id"])
    except OSError:
        pass  # an unreadable ledger costs discrimination, never the sweep
    return out


def _membership_oracle(goal_ids):
    """Build the membership callable from ONE batch call to the SHIPPED predicate.

    Never a reimplementation of it (guard-2676): the component owns the retry
    read, the torn-read conservatism and the shape of loop_state. This resolves
    the whole population in a single process and hands `decide()` a dict lookup,
    so the pure decision half keeps its callable interface and pays no I/O.

    Every failure mode here resolves to INDETERMINATE, never to COUNTED. An
    oracle that could not answer must not be able to render as health — that
    substitution is the exact defect this check exists to detect.
    """
    if not goal_ids:
        return lambda _g: cps.INDETERMINATE
    try:
        proc = subprocess.run(
            [sys.executable, str(_BUMP), "--verify-counted-many", *goal_ids],
            capture_output=True, text=True, timeout=60,
        )
        payload = json.loads(proc.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError):
        return lambda _g: cps.INDETERMINATE
    if payload.get("indeterminate"):
        return lambda _g: cps.INDETERMINATE
    counted = set(payload.get("counted") or [])
    return lambda g: cps.COUNTED if g in counted else "absent"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--limit", type=int, default=_DEFAULT_LIMIT,
                    help=f"most-recent closes to check (default {_DEFAULT_LIMIT})")
    args = ap.parse_args(argv)

    agent = (os.environ.get("MIND_AGENT") or "").strip()
    sid = (os.environ.get("MIND_SID") or "").strip()
    project_root = _HERE.parent.parent

    if not agent or not sid:
        msg = {"error": "no_binding", "agent": agent or None, "sid": sid or None,
               "hint": "MIND_AGENT and MIND_SID are injected by the PreToolUse "
                       "bash hook; without them the session-scoped population "
                       "cannot be built and a silent empty sweep would read as "
                       "clean"}
        print(json.dumps(msg) if args.json else f"close-phase-skip: cannot run — {msg['hint']}",
              file=sys.stderr)
        return 2

    role, signals = _role_signals(agent, sid, project_root)

    if role == "worker":
        report = cps.decide([], lambda _g: cps.INDETERMINATE, role="worker")
    else:
        try:
            sys.path.insert(0, str(_HERE))
            from _paths import WORLD_DIR
            store = WORLD_DIR / "aspirations.jsonl"
            population, total, foreign = _closed_this_session(
                store, sid, args.limit, agent)
        except Exception as exc:  # noqa: BLE001 — an unreadable store is rc=2, not a clean sweep
            print(json.dumps({"error": "store_unreadable", "detail": str(exc)})
                  if args.json else f"close-phase-skip: store unreadable — {exc}",
                  file=sys.stderr)
            return 2
        oracle = _membership_oracle([r["id"] for r in population if r.get("id")])
        report = cps.decide(population, oracle,
                            _bump_failures(agent, project_root), role="reducer")
        report["population_total_this_session"] = total
        report["population_bound"] = args.limit
        if foreign:
            # Do not silently drop them: a SID that names another agent's
            # session means the binding is inconsistent, and the sweep saw only
            # part of what that SID closed.
            report["foreign_agent_closes_excluded"] = foreign
            report["completeness"] = "partial"
        if total > len(population):
            report["bound_excluded"] = total - len(population)
            report["completeness"] = "partial"  # a hidden remainder is not complete

    report["agent"] = agent
    report["sid"] = sid
    report["role"] = role
    report["role_signals"] = signals

    if args.json:
        print(json.dumps(report))
    else:
        print(cps.render(report))
        if report.get("bound_excluded"):
            print(f"  BOUND: {report['bound_excluded']} older close(s) this session "
                  f"not checked (--limit {args.limit}) — re-run with a higher limit "
                  f"to cover them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
