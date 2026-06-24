#!/usr/bin/env python3
"""team-contribution-report.py --  (, Team-eval T2).

Per-agent contribution-vs-harm scorecard AGGREGATOR over a rolling window.

READ-ONLY by design: it aggregates telemetry that already exists (g-318-02
desc: "Build an AGGREGATOR (not new instrumentation)"). No file is written
except the report on stdout. Sources (all verified present at build time):

  contribution
    - world/productivity-snapshots.jsonl   per-iteration {agent, score, breakdown,
                                            goals_completed, productive_goals, ...}
  harm
    - meta/gate-firings.jsonl              {agent, gate_id, decision}  -> blocks
    - world/override-bypass-ledger.jsonl   {agent, ...}                -> override usage
    - world/goal-duplication-overrides.jsonl {agent, ...}             -> dup-override usage
  contribution + harm (best-effort)
    - git log                              feat/fix(g-NNN) commits + "Revert" commits,
                                            attributed via a goal-id -> claimed_by map
                                            built from the aspiration queues.

Cumulative-counts caveat (the one correctness trap here): productivity-snapshots
`breakdown.counts` (tree_writes, rb, hyp_resolved, ...) AND the top-level
`goals_completed`/`productive_goals` are SESSION-CUMULATIVE running totals, not
per-iteration deltas. Summing them across snapshots double-counts. This
aggregator therefore derives window contribution ONLY from per-iteration-safe
signals (snapshot COUNT, MEAN score, MEAN ratios) and reports the most-recent
snapshot's cumulative counts separately, labelled `latest_snapshot`, never summed.

Usage:
  py -3 core/scripts/team-contribution-report.py [--window-days N] [--format json|table]
                                                 [--agent NAME] [--world-dir P] [--meta-dir P]
                                                 [--no-commits] [--now ISO8601]

--world-dir / --meta-dir override path resolution (used by tests; production
resolves via core/scripts/_paths.py, which itself honours MIND_WORLD/MIND_META).
--now pins the window's upper bound (tests; default = system local time).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# A gate firing counts as a "block" (harm) ONLY when its decision is an explicit
# refusal. Blocklist, not allowlist (chosen during the  live smoke test):
# the real decision vocabulary is {noop, pass, block, override, fail_open}.
# 'override' was bypassed (and is counted separately via override-bypass-ledger --
# counting it here too would double-count harm) and 'fail_open' let the action
# through, so neither is a block. A blocklist is conservative: an unknown future
# decision defaults to non-harm rather than silently inflating the harm metric.
_BLOCK_DECISIONS = {"block", "blocked", "refuse", "refused", "fail", "deny",
                    "denied", "reject", "rejected"}

_AGENTS_FALLBACK = ("alpha", "bravo", "charlie", "delta", "echo", "zeta")


def _resolve_dirs(world_dir: str | None, meta_dir: str | None):
    """Return (world_path, meta_path). Explicit flags win; else _paths.py
    (which honours MIND_WORLD/MIND_META env overrides). Fail-soft: if _paths
    cannot be imported, return whatever was passed (possibly None)."""
    w = Path(world_dir) if world_dir else None
    m = Path(meta_dir) if meta_dir else None
    if w and m:
        return w, m
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
        import _paths  # type: ignore

        if w is None:
            w = Path(_paths.WORLD_DIR)
        if m is None:
            m = Path(_paths.META_DIR)
    except Exception as e:  # pragma: no cover - exercised only on broken envs
        print(f"WARN: path resolution via _paths failed: {e}", file=sys.stderr)
    return w, m


def _load_jsonl(path: Path):
    """Yield parsed objects from a JSONL file. Tolerant: missing file -> nothing;
    a malformed line is skipped (not fatal -- telemetry logs occasionally carry a
    half-written tail line)."""
    if not path or not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"WARN: cannot read {path}: {e}", file=sys.stderr)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (ValueError, TypeError):
            continue


def _parse_ts(value):
    """Parse an ISO8601-ish timestamp; return a datetime or None. Tolerates a
    trailing 'Z' and space-separated date/time."""
    if not isinstance(value, str) or not value:
        return None
    v = value.strip().replace("Z", "").replace(" ", "T")
    # Drop fractional seconds / offset the cheap way -- snapshots use second
    # precision local time, so a prefix parse is enough.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[: len(("2026-06-14T07:00:00")[: len(fmt) + 0])] if False else v.split(".")[0][:19], fmt)
        except ValueError:
            continue
    return None


def _in_window(obj, ts_key, cutoff):
    """True if obj[ts_key] parses and is >= cutoff. Records with an unparseable
    timestamp are EXCLUDED (conservative -- a window report must not silently
    fold in undated rows)."""
    dt = _parse_ts(obj.get(ts_key))
    return dt is not None and dt >= cutoff


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def aggregate_contribution(snapshots, cutoff):
    """Per-agent contribution from productivity-snapshots within the window.
    Uses per-iteration-safe signals only (count + means); reports the latest
    snapshot's cumulative counts separately (never summed)."""
    per_agent = {}
    for s in snapshots:
        if not _in_window(s, "ts", cutoff):
            continue
        agent = s.get("agent")
        if not agent:
            continue
        a = per_agent.setdefault(agent, {"scores": [], "deep": [], "enc": [],
                                         "routine": [], "latest_ts": None, "latest": {}})
        a["scores"].append(s.get("score"))
        bd = s.get("breakdown") or {}
        a["deep"].append(bd.get("deep_ratio"))
        a["enc"].append(bd.get("encoding_ratio"))
        a["routine"].append(bd.get("routine_ratio"))
        dt = _parse_ts(s.get("ts"))
        if dt and (a["latest_ts"] is None or dt >= a["latest_ts"]):
            a["latest_ts"] = dt
            counts = (bd.get("counts") or {})
            a["latest"] = {
                "goals_completed": s.get("goals_completed"),
                "productive_goals": s.get("productive_goals"),
                "tree_writes": counts.get("tree_writes"),
                "rb": counts.get("rb"),
                "guards": counts.get("guards"),
                "hyp_created": counts.get("hyp_created"),
                "hyp_resolved": counts.get("hyp_resolved"),
                "experience": counts.get("experience"),
            }
    out = {}
    for agent, a in per_agent.items():
        out[agent] = {
            "iterations": len(a["scores"]),
            "avg_score": _mean(a["scores"]),
            "avg_deep_ratio": _mean(a["deep"]),
            "avg_encoding_ratio": _mean(a["enc"]),
            "avg_routine_ratio": _mean(a["routine"]),
            "latest_snapshot": a["latest"],
        }
    return out


def aggregate_harm(gate_firings, overrides, dup_overrides, cutoff):
    """Per-agent harm counts from the three ledgers within the window."""
    per_agent = {}

    def _slot(agent):
        return per_agent.setdefault(agent, {
            "gate_blocks": 0, "gate_total": 0,
            "override_count": 0, "dup_override_count": 0})

    for g in gate_firings:
        if not _in_window(g, "ts", cutoff):
            continue
        agent = g.get("agent")
        if not agent:
            continue
        slot = _slot(agent)
        slot["gate_total"] += 1
        if str(g.get("decision", "")).lower() in _BLOCK_DECISIONS:
            slot["gate_blocks"] += 1

    for o in overrides:
        if not _in_window(o, "ts", cutoff):
            continue
        agent = o.get("agent")
        if agent:
            _slot(agent)["override_count"] += 1

    # goal-duplication-overrides uses `timestamp`, not `ts`.
    for d in dup_overrides:
        if not _in_window(d, "timestamp", cutoff):
            continue
        agent = d.get("agent")
        if agent:
            _slot(agent)["dup_override_count"] += 1

    for agent, slot in per_agent.items():
        slot["block_ratio"] = (round(slot["gate_blocks"] / slot["gate_total"], 4)
                               if slot["gate_total"] else 0.0)
    return per_agent


def build_goal_agent_map(world_dir, agents_root):
    """Best-effort goal_id -> agent map from aspiration queues. Prefers
    claimed_by, falls back to filed_by_agent. Returns {} on any failure (the
    commit section then degrades to team-level only)."""
    mapping = {}

    def _scan(path):
        for asp in _load_jsonl(path):
            for g in (asp.get("goals") or []):
                gid = g.get("id")
                if not gid:
                    continue
                who = g.get("claimed_by") or g.get("filed_by_agent")
                if who:
                    mapping[gid] = who

    try:
        if world_dir:
            _scan(Path(world_dir) / "aspirations.jsonl")
        if agents_root and Path(agents_root).exists():
            for conf in Path(agents_root).glob("*/aspirations.jsonl"):
                _scan(conf)
    except Exception as e:  # pragma: no cover
        print(f"WARN: goal-agent map build failed: {e}", file=sys.stderr)
    return mapping


def commit_stats(cutoff, goal_agent_map, repo=PROJECT_ROOT):
    """Best-effort per-agent commit + revert counts from git log since cutoff.
    Attribution: a commit message naming a goal-id present in goal_agent_map is
    credited to that agent. Reverts ('Revert' in subject) are harm. Commits with
    no resolvable agent accrue to the 'team' bucket. Returns {} if git fails."""
    import re

    since = cutoff.strftime("%Y-%m-%d")
    try:
        out = subprocess.run(
            ["git", "log", f"--since={since}", "--pretty=format:%H%x1f%s"],
            cwd=str(repo), capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {}
    except Exception as e:
        print(f"WARN: git log failed: {e}", file=sys.stderr)
        return {}

    gid_re = re.compile(r"g-\d+-\d+")
    per_agent = {}

    def _slot(agent):
        return per_agent.setdefault(agent, {"commits": 0, "reverts": 0})

    for line in out.stdout.splitlines():
        if "\x1f" not in line:
            continue
        _sha, subject = line.split("\x1f", 1)
        is_revert = subject.lower().startswith("revert") or "revert" in subject.lower()[:12]
        gids = gid_re.findall(subject)
        agent = None
        for gid in gids:
            if gid in goal_agent_map:
                agent = goal_agent_map[gid]
                break
        bucket = _slot(agent or "team")
        bucket["commits"] += 1
        if is_revert:
            bucket["reverts"] += 1
    return per_agent


def build_scorecard(contribution, harm, commits):
    """Merge the three per-agent dicts into one scorecard. harm_per_iteration is
    the comparable harm-density metric (harm events normalised by iterations)."""
    agents = set(contribution) | set(harm) | {a for a in commits if a != "team"}
    cards = {}
    for agent in sorted(agents):
        c = contribution.get(agent, {})
        h = harm.get(agent, {})
        cm = commits.get(agent, {})
        iters = c.get("iterations", 0)
        harm_events = (h.get("gate_blocks", 0) + h.get("override_count", 0)
                       + h.get("dup_override_count", 0) + cm.get("reverts", 0))
        cards[agent] = {
            "contribution": {
                "iterations": iters,
                "avg_score": c.get("avg_score", 0.0),
                "avg_deep_ratio": c.get("avg_deep_ratio", 0.0),
                "avg_encoding_ratio": c.get("avg_encoding_ratio", 0.0),
                "commits": cm.get("commits", 0),
                "latest_snapshot": c.get("latest_snapshot", {}),
            },
            "harm": {
                "gate_blocks": h.get("gate_blocks", 0),
                "block_ratio": h.get("block_ratio", 0.0),
                "override_count": h.get("override_count", 0),
                "dup_override_count": h.get("dup_override_count", 0),
                "reverts": cm.get("reverts", 0),
            },
            "harm_per_iteration": round(harm_events / iters, 4) if iters else 0.0,
        }
    return cards


def render_table(scorecard, window_days, team_commits):
    lines = []
    lines.append(f"=== Team Contribution-vs-Harm Scorecard (rolling {window_days}d) ===")
    hdr = (f"{'agent':<8} {'iters':>5} {'avg_score':>9} {'deep%':>6} "
           f"{'commits':>7} {'blocks':>6} {'ovr':>4} {'dup':>4} {'rev':>4} {'harm/it':>7}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for agent, card in scorecard.items():
        c, h = card["contribution"], card["harm"]
        lines.append(
            f"{agent:<8} {c['iterations']:>5} {c['avg_score']:>9.3f} "
            f"{c['avg_deep_ratio']*100:>5.0f}% {c['commits']:>7} {h['gate_blocks']:>6} "
            f"{h['override_count']:>4} {h['dup_override_count']:>4} {h['reverts']:>4} "
            f"{card['harm_per_iteration']:>7.3f}")
    if team_commits:
        lines.append(f"(+{team_commits} commit(s) in window with no resolvable agent -> team bucket)")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Per-agent contribution-vs-harm scorecard.")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--format", choices=["json", "table"], default="json")
    ap.add_argument("--agent", default=None, help="Filter to a single agent.")
    ap.add_argument("--world-dir", default=None)
    ap.add_argument("--meta-dir", default=None)
    ap.add_argument("--no-commits", action="store_true",
                    help="Skip the git-log commit/revert section.")
    ap.add_argument("--now", default=None,
                    help="Pin the window upper bound (ISO8601); default = system time.")
    args = ap.parse_args(argv)

    now = _parse_ts(args.now) if args.now else datetime.now()
    if now is None:
        print("ERROR: --now did not parse as ISO8601", file=sys.stderr)
        return 2
    cutoff = now - timedelta(days=args.window_days)

    world_dir, meta_dir = _resolve_dirs(args.world_dir, args.meta_dir)

    snapshots = list(_load_jsonl(world_dir / "productivity-snapshots.jsonl")) if world_dir else []
    gate_firings = list(_load_jsonl(meta_dir / "gate-firings.jsonl")) if meta_dir else []
    overrides = list(_load_jsonl(world_dir / "override-bypass-ledger.jsonl")) if world_dir else []
    dup_overrides = list(_load_jsonl(world_dir / "goal-duplication-overrides.jsonl")) if world_dir else []

    contribution = aggregate_contribution(snapshots, cutoff)
    harm = aggregate_harm(gate_firings, overrides, dup_overrides, cutoff)

    commits = {}
    if not args.no_commits:
        agents_root = PROJECT_ROOT / "agents"
        gmap = build_goal_agent_map(world_dir, agents_root)
        commits = commit_stats(cutoff, gmap)
    team_commits = commits.get("team", {}).get("commits", 0)

    scorecard = build_scorecard(contribution, harm, commits)
    if args.agent:
        scorecard = {k: v for k, v in scorecard.items() if k == args.agent}

    if args.format == "table":
        print(render_table(scorecard, args.window_days, team_commits))
    else:
        print(json.dumps({
            "window_days": args.window_days,
            "generated_for_window_ending": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "agents": scorecard,
            "unattributed_team_commits": team_commits,
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
