#!/usr/bin/env python3
"""close-review-queue.py — the POST-HOC close-review lane for closers whose model has no
measured closure pass rate (g-375-09, stage 1).

WHAT IT IS. Two read-only views over the same artifacts the blocking close-review gate
reads (`close-review-gate.py`, g-357-40) and its producer writes (`close-review-verdict.py`,
g-357-41):

  list   the closures by an UNCALIBRATED closer role (config: `close_review_gate.
         review_closer_roles`, today `[worker]`) that no independent verdict covers yet,
         ranked so the reviewer spends its budget where a defect costs most — tier 2
         first (`goal_close_risk_tier.classify`, the gate's own classifier), then HIGH
         priority, then the newest — and capped per run.
  stats  the pass rate over every verdict artifact, grouped by the CLOSER'S ROLE (joined
         through the goal record; the artifact names the closer, not the role), plus the
         relax-rule verdict written into `core/config/aspirations.yaml` next to the list.

WHY POST-HOC AND NOT THE BLOCKING GATE (an execution decision, alpha 2026-09-24). Both
Qwen-Body closures of 2026-09-23 carried a defect a reviewer catches in one step, and a
third was caught in flight (the goal's evidence). Enabling the gate for those closers
would REFUSE their close, and a lesser model handles a refusal the way zc-02 handled the
uncommitted-work gate the same day: it passes the override "exactly as the reducer
would". A refusal it can wave through teaches overriding, and one it cannot wave through
stalls the Body on a branch the worker loop does not have. Reviewing AFTER the close
keeps the Body's loop untouched, spends a stronger mind's time on the finished work (the
standard way small-model output is lifted), files a Fix goal on a REJECT (fix-forward
over already-merged work — guard-2852: the world, not the status field, is what is
wrong), and accumulates the pass rate the blocking decision needs BEFORE anyone is
blocked on it. The gate's tiering, artifact path and verdict format are reused unchanged
(rb-4452: one verdict format), so flipping to blocking later is a config change, not a
second system.

WHO MAY REVIEW. Independence is at the MIND level — the agent NAME, never the session
(coordination.md § "Independence Is At The MIND Level"). Every zc Body closes as `alpha`,
so alpha's own reducer must not review them: `list` partitions candidates by the running
reviewer and leaves same-mind closures for another agent's cycle, and the count it
reports for them is the honest "coverage I do not have" (guard-1760).

REPORT-ONLY, ALWAYS. This never mutates a goal, never writes a verdict, never blocks
anything. The verdict is the reviewer's to assert through `/fresh-eyes-close`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

#: The config key (under `close_review_gate:` in core/config/aspirations.yaml) naming the
#: closer roles whose closures this lane reviews. One list, one concept: the roles listed
#: here are "closures reviewed after the fact"; if the blocking gate is ever scoped by
#: role, it reads THIS list — two lists for one class would drift (communication-clarity
#: rule 5).
CONFIG_KEY = "review_closer_roles"

#: Relax rule for the routing (outcome 2 of ): a role leaves the reviewed class
#: only on evidence — at least this many reviewed closures, an approval rate at or above
#: the floor, and no REJECT among the most recent `RELAX_RECENT` verdicts.
RELAX_MIN_REVIEWED = 10
RELAX_MIN_APPROVE_RATE = 0.8
RELAX_RECENT = 5

TERMINAL_NOT_CLOSED = ("pending", "in-progress", "blocked", "skipped", "expired",
                       "decomposed", "superseded")


# ─── shared definitions, loaded from the gate (one definition each) ────────────

def _gate():
    """close-review-gate.py by path (its filename is hyphenated): `verdict_path`,
    `read_verdict` and `releases_close` keep ONE definition in the tree."""
    cached = sys.modules.get("close_review_gate")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "close_review_gate", SCRIPT_DIR / "close-review-gate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["close_review_gate"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _tier():
    import goal_close_risk_tier  # type: ignore
    return goal_close_risk_tier


# ─── inputs ───────────────────────────────────────────────────────────────────

def roles_from_config() -> list[str]:
    """`close_review_gate.review_closer_roles` from core/config/aspirations.yaml, or []."""
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load((SCRIPT_DIR.parent / "config" / "aspirations.yaml")
                             .read_text(encoding="utf-8")) or {}
        section = cfg.get("close_review_gate") or {}
        roles = section.get(CONFIG_KEY) or []
        return [str(r).strip().lower() for r in roles if str(r).strip()]
    except Exception as exc:  # loud, never a silent empty (guard-2298)
        print(f"close-review-queue: config read failed ({type(exc).__name__}: {exc}); "
              f"no roles from config", file=sys.stderr)
        return []


def load_closures(role: str) -> list[dict]:
    """Every goal record carrying completed_by_role == role, through the query API
    (never a direct JSONL read). Loud on failure: an unreadable store and a role with no
    closures are the same empty list downstream, and only one of them is news."""
    script = SCRIPT_DIR / "aspirations-query.sh"
    try:
        from _runtime_bash import bash_cmd  # type: ignore
        res = subprocess.run(
            bash_cmd(script, "--goal-field", "completed_by_role", role, "--full"),
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0 or not res.stdout.strip():
            print(f"close-review-queue: store read failed rc={res.returncode} "
                  f"{res.stderr.strip()[:200]}", file=sys.stderr)
            return []
        rows = json.loads(res.stdout)
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except Exception as exc:
        print(f"close-review-queue: store read error {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []


def completed_at(goal: dict) -> str:
    """The closure stamp: `completed_at` (full timestamp) or `completed_date` (a day)."""
    return str(goal.get("completed_at") or goal.get("completed_date") or "")


def goal_id_of(goal: dict) -> str:
    return str(goal.get("id") or goal.get("goal_id") or "")


# ─── list ─────────────────────────────────────────────────────────────────────

def closure_stamp(goal: dict) -> str:
    """`completed_at`, or a bare `completed_date` read as that day's midnight so a
    day-only stamp compares (and ranks) as the OLDEST moment of its day."""
    stamp = completed_at(goal)
    return stamp + "T00:00:00" if len(stamp) == 10 else stamp


def rank_key(goal: dict, tier: int) -> tuple:
    """Tier 2 first, then HIGH priority, then the newest closure."""
    prio = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(str(goal.get("priority") or "").upper(), 3)
    stamp = closure_stamp(goal)
    return (0 if tier == 2 else 1, prio, "~" if not stamp else
            "".join(chr(0x10FFFF - ord(c)) for c in stamp))


def select_candidates(goals: list[dict], *, reviewed: set[str], now: datetime,
                      since_hours: float, reviewer: str, cap: int) -> dict:
    """Pure: which closures a reviewer should look at next, and which it may not.

    Filters: status must be `completed` (a recurring goal rests at `pending` with its
    completed_by_role still stamped — those are tier 0 and never reviewed here), closed
    within `since_hours` (the measurement starts when the lane does; history is not
    re-litigated), and without a verdict artifact. Same-mind closures (completed_by ==
    reviewer) are partitioned out, never ranked, and counted so the caller can see the
    coverage this reviewer cannot provide."""
    cutoff = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    tier_mod = _tier()
    eligible: list[tuple[tuple, dict, dict]] = []
    same_mind: list[str] = []
    skipped = {"not_completed": 0, "recurring": 0, "too_old": 0, "reviewed": 0}
    for g in goals:
        gid = goal_id_of(g)
        if not gid:
            continue
        if str(g.get("status") or "").lower() != "completed":
            skipped["not_completed"] += 1
            continue
        if g.get("recurring") is True or str(g.get("recurring") or "").lower() == "true":
            skipped["recurring"] += 1
            continue
        if closure_stamp(g) < cutoff:
            skipped["too_old"] += 1
            continue
        if gid in reviewed:
            skipped["reviewed"] += 1
            continue
        if str(g.get("completed_by") or "").strip().lower() == reviewer.strip().lower():
            same_mind.append(gid)
            continue
        tier = tier_mod.classify(g)
        eligible.append((rank_key(g, tier["tier"]), g, tier))
    eligible.sort(key=lambda t: t[0])
    rows = []
    for _, g, tier in eligible[:cap]:
        rows.append({
            "goal_id": goal_id_of(g),
            "asp_id": g.get("asp_id") or g.get("aspiration_id"),
            "title": str(g.get("title") or "")[:160],
            "priority": g.get("priority"),
            "tier": tier["tier"],
            "tier_reasons": tier["reasons"],
            "completed_by": g.get("completed_by"),
            "completed_by_role": g.get("completed_by_role"),
            "completed_by_sid": g.get("completed_by_sid"),
            "completed_at": completed_at(g),
            "commit_sha": g.get("commit_sha"),
        })
    return {
        "candidates": rows,
        "eligible_total": len(eligible),
        "cap": cap,
        "same_mind": same_mind,
        "skipped": skipped,
    }


def reviewed_ids(goals: list[dict]) -> set[str]:
    gate = _gate()
    out: set[str] = set()
    for g in goals:
        gid = goal_id_of(g)
        if gid and gate.read_verdict(gate.verdict_path(gid)) is not None:
            out.add(gid)
    return out


# ─── stats ────────────────────────────────────────────────────────────────────

def artifacts_dir() -> Path | None:
    p = _gate().verdict_path("_probe_")
    return p.parent if p is not None else None


def read_all_verdicts(directory: Path | None) -> dict[str, dict]:
    """goal_id -> CURRENT verdict (last entry wins, the gate's own reading)."""
    gate = _gate()
    out: dict[str, dict] = {}
    if directory is None or not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.json")):
        v = gate.read_verdict(p)
        if isinstance(v, dict):
            gid = str(v.get("goal_id") or p.stem)
            out[gid] = v
    return out


def relax_rule(n: int, approve_rate: float, recent_verdicts: list[str]) -> dict:
    """The written-down rule for dropping a role from the reviewed class."""
    recent_reject = any(v == "REJECT" for v in recent_verdicts[-RELAX_RECENT:])
    ok = n >= RELAX_MIN_REVIEWED and approve_rate >= RELAX_MIN_APPROVE_RATE and not recent_reject
    return {
        "relax_ok": ok,
        "rule": (f">= {RELAX_MIN_REVIEWED} reviewed, approve rate >= {RELAX_MIN_APPROVE_RATE}, "
                 f"no REJECT in the last {RELAX_RECENT}"),
        "blocking_reasons": [r for r, hit in (
            (f"only {n} reviewed (< {RELAX_MIN_REVIEWED})", n < RELAX_MIN_REVIEWED),
            (f"approve rate {approve_rate:.2f} < {RELAX_MIN_APPROVE_RATE}",
             approve_rate < RELAX_MIN_APPROVE_RATE),
            (f"a REJECT among the last {RELAX_RECENT}", recent_reject),
        ) if hit],
    }


def role_stats(verdicts: dict[str, dict], goals_by_id: dict[str, dict],
               roles: list[str]) -> dict:
    """Pure: verdict counts and the relax verdict per closer role.

    An artifact whose goal is not among the loaded closures (a Mind's own tier-2 close,
    a goal outside the queried roles) is counted under `unmatched`, never under a role."""
    gate = _gate()
    per: dict[str, list[tuple[str, str]]] = {r: [] for r in roles}
    # A role is not a model: a `worker` closure may come from a Claude Body or a local
    # Qwen one, and the goal record carries no model field. The closing SESSION is the
    # finest key it does carry, so the per-sid split lets a reader separate the
    # uncalibrated Bodies (their SIDs are known to their operator) from the rest.
    by_sid: dict[str, dict[str, int]] = {}
    unmatched = 0
    for gid, v in verdicts.items():
        g = goals_by_id.get(gid)
        role = str((g or {}).get("completed_by_role") or "").strip().lower()
        if role in per:
            verdict = str(v.get("verdict") or "")
            per[role].append((str(v.get("reviewed_at") or ""), verdict))
            sid8 = str((g or {}).get("completed_by_sid") or "?")[:8]
            row = by_sid.setdefault(f"{role}/{sid8}", {"reviewed": 0, "approved": 0, "rejected": 0})
            row["reviewed"] += 1
            row["approved"] += 1 if gate.releases_close(verdict) else 0
            row["rejected"] += 1 if verdict == "REJECT" else 0
        else:
            unmatched += 1
    out: dict[str, Any] = {"roles": {}, "by_sid": by_sid, "unmatched_artifacts": unmatched}
    for role, items in per.items():
        items.sort()  # reviewed_at ascending; an absent stamp sorts first (oldest)
        seq = [v for _, v in items]
        n = len(seq)
        approved = sum(1 for v in seq if gate.releases_close(v))
        rejected = sum(1 for v in seq if v == "REJECT")
        rate = (approved / n) if n else 0.0
        out["roles"][role] = {
            "reviewed": n,
            "approved": approved,
            "rejected": rejected,
            "other": n - approved - rejected,
            "approve_rate": round(rate, 3),
            "recent": seq[-RELAX_RECENT:],
            **relax_rule(n, rate, seq),
        }
    return out


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _print_list(result: dict, roles: list[str], reviewer: str, since_hours: float) -> None:
    print(f"close-review-queue list: roles={roles} reviewer={reviewer} "
          f"since={since_hours}h eligible={result['eligible_total']} cap={result['cap']} "
          f"same_mind_left_for_another_agent={len(result['same_mind'])} "
          f"skipped={result['skipped']}")
    for r in result["candidates"]:
        print(f"  {r['goal_id']} [{r['asp_id']}] tier={r['tier']} {r['priority']} "
              f"by={r['completed_by']}/{str(r['completed_by_sid'] or '')[:8]} "
              f"at={r['completed_at']} — {r['title'][:90]}")
        for reason in r["tier_reasons"]:
            print(f"      · {reason}")
    if not result["candidates"]:
        print("  (no unreviewed closure this reviewer may review)")


def _print_stats(stats: dict) -> None:
    for role, s in stats["roles"].items():
        print(f"role={role}: reviewed={s['reviewed']} approved={s['approved']} "
              f"rejected={s['rejected']} other={s['other']} approve_rate={s['approve_rate']} "
              f"recent={s['recent']} relax_ok={s['relax_ok']}")
        for reason in s["blocking_reasons"]:
            print(f"    · not relaxable: {reason}")
        print(f"    rule: {s['rule']}")
    print(f"unmatched_artifacts (not a listed role's closure): {stats['unmatched_artifacts']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="unreviewed closures this reviewer may review, ranked")
    ls.add_argument("--roles", nargs="*", default=None,
                    help=f"closer roles; default: close_review_gate.{CONFIG_KEY} from config")
    ls.add_argument("--since-hours", type=float, default=72.0)
    ls.add_argument("--cap", type=int, default=3)
    ls.add_argument("--reviewer", default=None, help="defaults to $MIND_AGENT")
    ls.add_argument("--json", action="store_true")
    st = sub.add_parser("stats", help="pass rate per closer role + the relax-rule verdict")
    st.add_argument("--roles", nargs="*", default=None)
    st.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    roles = [r.lower() for r in (args.roles or [])] or roles_from_config()
    if not roles:
        print("close-review-queue: no closer roles (config list empty and no --roles)",
              file=sys.stderr)
        return 1
    goals: list[dict] = []
    for role in roles:
        goals.extend(load_closures(role))
    goals_by_id = {goal_id_of(g): g for g in goals if goal_id_of(g)}

    if args.cmd == "list":
        reviewer = (args.reviewer or os.environ.get("MIND_AGENT") or "").strip()
        if not reviewer:
            print("close-review-queue: reviewer unknown (--reviewer or $MIND_AGENT)",
                  file=sys.stderr)
            return 1
        result = select_candidates(goals, reviewed=reviewed_ids(goals), now=datetime.now(),
                                   since_hours=args.since_hours, reviewer=reviewer,
                                   cap=args.cap)
        result.update({"roles": roles, "reviewer": reviewer, "since_hours": args.since_hours})
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            _print_list(result, roles, reviewer, args.since_hours)
        return 0

    stats = role_stats(read_all_verdicts(artifacts_dir()), goals_by_id, roles)
    stats["artifacts_dir"] = str(artifacts_dir())
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        _print_stats(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
