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
  stats  the pass rate over every verdict artifact, grouped by the CLOSER'S ROLE (see
         WHERE THE ROLE COMES FROM below), plus the relax-rule verdict written into
         `core/config/aspirations.yaml` next to the list.

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

RELAXING NEVER MEANS ZERO (g-375-82). A role that passes the relax rule MOVES to
`review_sampled_roles`: a deterministic `review_sample_rate` share of its closures (default
0.2) stays in `list`, and `stats` keeps reporting its pass rate. Dropping the role outright
would end the measurement that justified dropping it, and a later regression in that
closer would go unseen. The sample is a hash of the goal id, so every run and every box
agrees on which closures are in it and a sampled closure stays queued until reviewed.

WHERE THE ROLE COMES FROM (g-375-55). A verdict written since g-375-55 names its closer:
close-review-verdict.py copies `completed_by`, `completed_by_role` and `completed_by_sid`
from the goal record when it writes. Earlier verdicts named neither the closer nor the
role (their keys are checks, direction, fidelity, findings, goal_id, produced_by,
reviewed_at, reviewer and verdict); g-375-84 copied the closer onto those whose goal
record still existed on 2026-09-29, marking each `closer_backfilled_at`. A verdict that
still names no closer is joined to the goal record: the live closures first, then every
store through goal-resolve.py. A goal leaves the live query
when its aspiration is archived and when it is evicted, and a pass rate that loses those
verdicts measures the archival cadence, not the closer. An evicted goal survives only as
an id in its aspiration's census, so its role is gone: `stats` counts such a verdict in
its own bucket, beside "closer's role not reviewed" and "goal record not found
anywhere", and none of the three is folded into a role's rate.

REPORT-ONLY, ALWAYS. This never mutates a goal, never writes a verdict, never blocks
anything. The verdict is the reviewer's to assert through `/fresh-eyes-close`.
"""
from __future__ import annotations

import argparse
import hashlib
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

#: Relax rule for the routing (outcome 2 of ): a role leaves FULL review only on
#: evidence — at least this many reviewed closures, an approval rate at or above the
#: floor, and no REJECT among the most recent `RELAX_RECENT` verdicts. Leaving full review
#: means moving to the sampled class below, never to none ().
RELAX_MIN_REVIEWED = 10
RELAX_MIN_APPROVE_RATE = 0.8
RELAX_RECENT = 5

#: The relaxed class: roles whose closures are reviewed by a deterministic sample of
#: `review_sample_rate` (config keys beside CONFIG_KEY). A rate outside (0, 1] reads as the
#: default, loudly, because a zero rate is exactly the drop-to-none this class replaces.
SAMPLED_KEY = "review_sampled_roles"
SAMPLE_RATE_KEY = "review_sample_rate"
DEFAULT_SAMPLE_RATE = 0.2
SAMPLE_SEED = "close-review-sample-v1"

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


def _resolver():
    """goal-resolve.py by path (hyphenated too): the one lookup of a goal id across the
    live store, the archive and the eviction census."""
    cached = sys.modules.get("goal_resolve")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("goal_resolve", SCRIPT_DIR / "goal-resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["goal_resolve"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─── inputs ───────────────────────────────────────────────────────────────────

def _gate_section() -> dict:
    """The `close_review_gate` section of core/config/aspirations.yaml, or {} (loudly)."""
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load((SCRIPT_DIR.parent / "config" / "aspirations.yaml")
                             .read_text(encoding="utf-8")) or {}
        section = cfg.get("close_review_gate") or {}
        if not isinstance(section, dict):
            raise TypeError(f"close_review_gate is a {type(section).__name__}, not a mapping")
        return section
    except Exception as exc:  # loud, never a silent empty (guard-2298)
        print(f"close-review-queue: config read failed ({type(exc).__name__}: {exc}); "
              f"no roles from config", file=sys.stderr)
        return {}


def _roles(section: dict, key: str) -> list[str]:
    return [str(r).strip().lower() for r in (section.get(key) or []) if str(r).strip()]


def roles_from_config() -> list[str]:
    """`close_review_gate.review_closer_roles`: the roles whose every closure is reviewed."""
    return _roles(_gate_section(), CONFIG_KEY)


def sample_plan_from_config() -> tuple[list[str], float]:
    """`review_sampled_roles` and `review_sample_rate`: the relaxed roles, and the share of
    their closures that stays reviewed. A rate outside (0, 1] reads as the default, loudly."""
    section = _gate_section()
    raw = section.get(SAMPLE_RATE_KEY, DEFAULT_SAMPLE_RATE)
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        rate = -1.0
    if not 0.0 < rate <= 1.0:
        print(f"close-review-queue: {SAMPLE_RATE_KEY}={raw!r} is not in (0, 1]; using "
              f"{DEFAULT_SAMPLE_RATE} (a zero rate is the drop-to-none it replaces)",
              file=sys.stderr)
        rate = DEFAULT_SAMPLE_RATE
    return _roles(section, SAMPLED_KEY), rate


def in_sample(goal_id: str, rate: float) -> bool:
    """Whether a relaxed role's closure is in the reviewed sample. A hash of the goal id
    under SAMPLE_SEED, so the answer is the same on every run and every box."""
    digest = hashlib.sha256(f"{SAMPLE_SEED}:{goal_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") < rate * 2 ** 64


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


def resolve_unattributed(goal_ids: list[str],
                         world: str | None = None) -> tuple[dict[str, dict], str | None]:
    """goal_id -> goal-resolve.py's answer (disposition live | archived | evicted | unknown,
    plus the closer fields of a found record) for the verdicts the live closures did not
    cover, and the error text if the lookup could not run. Loud, like load_closures: a
    lookup that failed and a goal that is nowhere must not read the same."""
    if not goal_ids:
        return {}, None
    try:
        world = world or str(_gate().WORLD_DIR or "")
        if not world:
            raise RuntimeError("no world directory")
        mod = _resolver()
        return {gid: mod.resolve(gid, world) for gid in goal_ids}, None
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"close-review-queue: goal resolution failed ({err}); {len(goal_ids)} "
              f"verdict(s) left unresolved", file=sys.stderr)
        return {}, err


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
                      since_hours: float, reviewer: str, cap: int,
                      sampled_roles: frozenset[str] | set[str] = frozenset(),
                      sample_rate: float = DEFAULT_SAMPLE_RATE) -> dict:
    """Pure: which closures a reviewer should look at next, and which it may not.

    Filters: status must be `completed` (a recurring goal rests at `pending` with its
    completed_by_role still stamped — those are tier 0 and never reviewed here), closed
    within `since_hours` (the measurement starts when the lane does; history is not
    re-litigated), without a verdict artifact, and, for a relaxed role in
    `sampled_roles`, inside the sample (the rest are counted as `not_sampled`). Same-mind
    closures (completed_by == reviewer) are partitioned out, never ranked, and counted so
    the caller can see the coverage this reviewer cannot provide."""
    cutoff = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    tier_mod = _tier()
    eligible: list[tuple[tuple, dict, dict]] = []
    same_mind: list[str] = []
    skipped = {"not_completed": 0, "recurring": 0, "too_old": 0, "reviewed": 0,
               "not_sampled": 0}
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
        role = str(g.get("completed_by_role") or "").strip().lower()
        if role in sampled_roles and not in_sample(gid, sample_rate):
            skipped["not_sampled"] += 1
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


def _closer_role(record: dict | None) -> str:
    return str((record or {}).get("completed_by_role") or "").strip().lower()


def role_stats(verdicts: dict[str, dict], goals_by_id: dict[str, dict],
               roles: list[str], sampled: frozenset[str] | set[str] = frozenset(),
               sample_rate: float = DEFAULT_SAMPLE_RATE,
               resolved: dict[str, dict] | None = None) -> dict:
    """Pure: verdict counts and the relax verdict per closer role.

    The closer comes from the verdict when it names one (written since g-375-55), else
    from the live closure in `goals_by_id`, else from `resolved`, goal-resolve.py's
    answer for the rest (a live or archived record). A verdict attributed to no listed
    role lands in exactly one `unattributed` bucket, never in a role's rate: its closer's
    role is not reviewed, its goal was evicted to an id-only census so no role survives,
    no store holds its goal, or it was never resolved (`resolved` has no answer for it).
    Each role carries its review class: `full`, or `sampled` for a relaxed role in
    `sampled`, whose pass rate is measured over its sample."""
    gate = _gate()
    resolved = resolved or {}
    per: dict[str, list[tuple[str, str]]] = {r: [] for r in roles}
    # A role is not a model: a `worker` closure may come from a Claude Body or a local
    # Qwen one, and the goal record carries no model field. The closing SESSION is the
    # finest key it does carry, so the per-sid split lets a reader separate the
    # uncalibrated Bodies (their SIDs are known to their operator) from the rest.
    by_sid: dict[str, dict[str, int]] = {}
    via = {"verdict": 0, "live": 0, "archive": 0}
    unattributed: dict[str, list[str]] = {"role_not_reviewed": [], "evicted_role_unknown": [],
                                          "not_found_anywhere": [], "not_resolved": []}
    for gid, v in verdicts.items():
        if _closer_role(v):
            record, source = v, "verdict"
        elif gid in goals_by_id:
            record, source = goals_by_id[gid], "live"
        elif gid not in resolved:
            unattributed["not_resolved"].append(gid)
            continue
        elif resolved[gid].get("disposition") in ("live", "archived"):
            record = resolved[gid]
            source = "archive" if record["disposition"] == "archived" else "live"
        elif resolved[gid].get("disposition") == "evicted":
            unattributed["evicted_role_unknown"].append(gid)
            continue
        else:
            unattributed["not_found_anywhere"].append(gid)
            continue
        role = _closer_role(record)
        if role not in per:
            unattributed["role_not_reviewed"].append(gid)
            continue
        via[source] += 1
        verdict = str(v.get("verdict") or "")
        per[role].append((str(v.get("reviewed_at") or ""), verdict))
        sid8 = str(record.get("completed_by_sid") or "?")[:8]
        row = by_sid.setdefault(f"{role}/{sid8}", {"reviewed": 0, "approved": 0, "rejected": 0})
        row["reviewed"] += 1
        row["approved"] += 1 if gate.releases_close(verdict) else 0
        row["rejected"] += 1 if verdict == "REJECT" else 0
    out: dict[str, Any] = {"roles": {}, "by_sid": by_sid, "artifacts_total": len(verdicts),
                           "attributed_via": via,
                           "unattributed": {k: sorted(x) for k, x in unattributed.items()}}
    for role, items in per.items():
        items.sort()  # reviewed_at ascending; an absent stamp sorts first (oldest)
        seq = [v for _, v in items]
        n = len(seq)
        approved = sum(1 for v in seq if gate.releases_close(v))
        rejected = sum(1 for v in seq if v == "REJECT")
        rate = (approved / n) if n else 0.0
        out["roles"][role] = {
            "review": "sampled" if role in sampled else "full",
            "sample_rate": sample_rate if role in sampled else 1.0,
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
    print(f"close-review-queue list: roles={roles} "
          f"sampled={result['sampled_roles']}@{result['sample_rate']} reviewer={reviewer} "
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
        review = "full" if s["review"] == "full" else f"sampled@{s['sample_rate']}"
        print(f"role={role} review={review}: reviewed={s['reviewed']} "
              f"approved={s['approved']} rejected={s['rejected']} other={s['other']} "
              f"approve_rate={s['approve_rate']} recent={s['recent']} relax_ok={s['relax_ok']}")
        if s["review"] == "full" and s["relax_ok"]:
            print(f"    · relaxable: move it to {SAMPLED_KEY}, where a sample stays reviewed; "
                  f"never drop it")
        for reason in s["blocking_reasons"]:
            where = "not relaxable" if s["review"] == "full" else "relax rule no longer met"
            print(f"    · {where}: {reason}")
        print(f"    rule: {s['rule']}")
    via = stats["attributed_via"]
    print(f"artifacts={stats['artifacts_total']}: attributed to a listed role "
          f"{sum(via.values())} (closer named by the verdict {via['verdict']}, "
          f"live record {via['live']}, archived record {via['archive']})")
    for key, label in (("role_not_reviewed", "closer's role not reviewed"),
                       ("evicted_role_unknown", "goal evicted to an id-only census, role unknown"),
                       ("not_found_anywhere", "goal record not found anywhere"),
                       ("not_resolved", "not resolved")):
        ids = stats["unattributed"][key]
        more = f" +{len(ids) - 12} more" if len(ids) > 12 else ""
        print(f"    {label}: {len(ids)}" + (f" [{' '.join(ids[:12])}{more}]" if ids else ""))
    if stats.get("resolution_error"):
        print(f"    goal resolution FAILED ({stats['resolution_error']}): the not-resolved "
              f"verdicts may belong to a listed role")


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
    sampled_cfg, sample_rate = sample_plan_from_config()
    # A role on both lists is reviewed in full: sampling only ever lowers coverage, so a
    # contradiction between the two lists resolves to the safer reading.
    sampled = [r for r in sampled_cfg if r not in roles]
    if not roles and not sampled:
        print("close-review-queue: no closer roles (config lists empty and no --roles)",
              file=sys.stderr)
        return 1
    goals: list[dict] = []
    for role in roles + sampled:
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
                                   cap=args.cap, sampled_roles=set(sampled),
                                   sample_rate=sample_rate)
        result.update({"roles": roles, "sampled_roles": sampled, "sample_rate": sample_rate,
                       "reviewer": reviewer, "since_hours": args.since_hours})
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            _print_list(result, roles, reviewer, args.since_hours)
        return 0

    verdicts = read_all_verdicts(artifacts_dir())
    # The live closures hold only goals still in the live store. Resolve every other
    # verdict's goal through all stores before attributing it ().
    rest = [gid for gid, v in verdicts.items() if not _closer_role(v) and gid not in goals_by_id]
    resolved, resolution_error = resolve_unattributed(rest)
    stats = role_stats(verdicts, goals_by_id, roles + sampled, sampled=set(sampled),
                       sample_rate=sample_rate, resolved=resolved)
    if resolution_error:
        stats["resolution_error"] = resolution_error
    stats["artifacts_dir"] = str(artifacts_dir())
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        _print_stats(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
