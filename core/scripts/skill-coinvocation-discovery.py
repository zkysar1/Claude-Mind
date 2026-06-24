#!/usr/bin/env python3
"""skill-coinvocation-discovery.py --  (Master plan finding #13).

Mine the cross-agent skill-invocations.jsonl ledger for co-invocation patterns
(skills used together inside a time-windowed working episode) and propose
compose_with relation CANDIDATES into world/skill-relations.yaml under a
co_invocation_candidates section.

Distinct from `skill-relations.py discover` (the daemon-routed command), which
reads the SPARSE explicit co_invocation_log (per-goal >=2-skill recordings, a
handful of entries) and prints ephemeral stdout. This script mines the RICH raw
ledger (every invocation, with ts + sid) and persists durable, timestamped
candidates for human/bravo review before promotion to forged_relations. The two
are complementary, not duplicates (rb-1992 verified: different source, different
window model, different persistence).

Co-occurrence model (calibrated to real ledger shapes via the g-304-10 probe):
sessions are large (median ~348 invocations) so "same session" is far too coarse
-- everything co-occurs. Instead each session is gap-split into EPISODES wherever
consecutive invocations are more than gap_minutes apart (default 15min, which
the probe showed captures ~66% of consecutive gaps as intra-episode). Within
each episode, every distinct unordered skill PAIR gets ONE co-occurrence vote
(episode-level, not per-record -- avoids dense-session inflation). Confidence is
Jaccard = co_occ / (eps_with_A + eps_with_B - co_occ) so ubiquitous loop skills
do not dominate by raw frequency alone.

Candidates already registered as compose_with (base OR forged) are excluded
(same dedup as skill-relations.py cmd_discover). Output is idempotent: the
co_invocation_candidates key is REPLACED wholesale each --apply run, stamped
discovered_at.

The ledger read is read-only. The single world/skill-relations.yaml write
preserves all other keys (forged_relations, co_invocation_log, last_updated) via
read-modify-write + atomic tmp-replace, mirroring skill-relations.py write_yaml.
Concurrency note: the daemon co-invoke endpoint is the only other writer of this
file (rare -- only goals invoking >=2 skills append to co_invocation_log); a RMW
interleave could drop one co_invocation_log entry, an accepted trade-off at that
write frequency. Durable fix if it ever matters: route this write through a
daemon endpoint.

Cross-references: g-304-10; skill-relations.py (discover/dedup pattern reused);
skill-analytics.py cmd_co_invocation (pair-frequency precedent); rb-1992
(search-for-existing-module-before-building -- this is NOT a duplicate);
guard-594 (window calibrated to real gaps, not guessed).

Usage:
  py -3 core/scripts/skill-coinvocation-discovery.py [--apply]
        [--gap-minutes N] [--min-co-occurrences N] [--top N] [--output json|human]
  Default is DRY-RUN (compute + print, no write). --apply persists candidates.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations

try:
    from _stdio import reconfigure_stdio
    reconfigure_stdio()
except Exception:  # pragma: no cover - defensive stdio fallback
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import CONFIG_DIR, WORLD_DIR, agents_root

BASE_RELATIONS_PATH = CONFIG_DIR / "skill-relations.yaml"
WORLD_RELATIONS_PATH = WORLD_DIR / "skill-relations.yaml"

DEFAULT_GAP_MINUTES = 15.0
DEFAULT_MIN_CO = 3
DEFAULT_TOP = 30
TS_FMT = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_yaml(path):
    """Read a YAML file, return parsed dict. Returns {} if missing."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _write_yaml_atomic(path, data):
    """Atomically write data as YAML (mirrors skill-relations.py write_yaml)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def _load_config():
    """config section from core/config/skill-relations.yaml (defaults applied by caller)."""
    data = _read_yaml(BASE_RELATIONS_PATH)
    return data.get("config", {}) if isinstance(data, dict) else {}


def _parse_ts(ts):
    try:
        return datetime.strptime(ts, TS_FMT)
    except (ValueError, TypeError):
        return None


def read_ledger(root=None):
    """Read every agent's skill-invocations.jsonl via the routed cross-agent
    glob. Uses agents_root().glob -- the audited pattern that auto-tracks an
    AGENTS_PARENT_DIR rename (CLAUDE.md cross-agent glob table) -- NEVER a
    depth-1 PROJECT_ROOT glob (which would match nothing post-relocation).
    `root` override exists only for tests."""
    base = root if root is not None else agents_root()
    records = []
    for f in sorted(base.glob("*/skill-invocations.jsonl")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return records


def build_episodes(records, gap_minutes):
    """Group records by (agent, sid), sort by ts, split into EPISODES wherever
    the gap between consecutive invocations exceeds gap_minutes. Each episode is
    the SET of distinct skills in that contiguous burst. Episodes with <2
    distinct skills are dropped (no pairs to vote)."""
    by_session = defaultdict(list)
    for r in records:
        if not isinstance(r, dict):
            continue
        skill = r.get("skill")
        ts = _parse_ts(r.get("ts", ""))
        if not skill or ts is None:
            continue
        by_session[(r.get("agent"), r.get("sid"))].append((ts, skill))

    gap_secs = float(gap_minutes) * 60.0
    episodes = []
    for entries in by_session.values():
        entries.sort(key=lambda x: x[0])
        current = set()
        last_ts = None
        for ts, skill in entries:
            if last_ts is not None and (ts - last_ts).total_seconds() > gap_secs:
                if len(current) >= 2:
                    episodes.append(current)
                current = set()
            current.add(skill)
            last_ts = ts
        if len(current) >= 2:
            episodes.append(current)
    return episodes


def count_cooccurrence(episodes):
    """One co-occurrence vote per distinct skill PAIR per episode, plus a count
    of episodes each skill appears in (for the Jaccard denominator)."""
    pair_counts = Counter()
    skill_episodes = Counter()
    for ep in episodes:
        for s in ep:
            skill_episodes[s] += 1
        for a, b in combinations(sorted(ep), 2):
            pair_counts[(a, b)] += 1
    return pair_counts, skill_episodes


def load_existing_compose():
    """Normalized (min,max) pairs already registered as compose_with in base OR
    forged relations -- excluded from candidates (dedup mirrors cmd_discover)."""
    base = _read_yaml(BASE_RELATIONS_PATH)
    world = _read_yaml(WORLD_RELATIONS_PATH)
    rels = []
    if isinstance(base.get("relations"), list):
        rels += base["relations"]
    if isinstance(world.get("forged_relations"), list):
        rels += world["forged_relations"]
    existing = set()
    for r in rels:
        if isinstance(r, dict) and r.get("type") == "compose_with":
            s, t = r.get("source", ""), r.get("target", "")
            existing.add((min(s, t), max(s, t)))
    return existing


def propose(pair_counts, skill_episodes, existing, min_co, top):
    """Emit compose_with candidates: pairs at/above min_co episodes, not already
    a registered compose_with relation, ranked by Jaccard then raw count so
    specific pairings surface above ubiquitous loop noise."""
    proposed = []
    for (a, b), count in pair_counts.most_common():
        if count < min_co:
            continue
        if (min(a, b), max(a, b)) in existing:
            continue
        union = skill_episodes[a] + skill_episodes[b] - count
        jaccard = round(count / union, 3) if union > 0 else 0.0
        proposed.append({
            "source": a,
            "target": b,
            "type": "compose_with",
            "confidence": jaccard,
            "co_occurrence_episodes": count,
            "episodes_with_source": skill_episodes[a],
            "episodes_with_target": skill_episodes[b],
            "evidence": (
                "Co-occurred in {} episodes; source in {}, target in {} "
                "(Jaccard {})".format(count, skill_episodes[a], skill_episodes[b], jaccard)
            ),
        })
    proposed.sort(key=lambda c: (c["confidence"], c["co_occurrence_episodes"]), reverse=True)
    return proposed[:top] if top else proposed


def discover(gap_minutes, min_co, top, root=None):
    """Full pipeline: ledger -> episodes -> co-occurrence -> deduped candidates.
    Returns the payload dict (no side effects)."""
    records = read_ledger(root=root)
    episodes = build_episodes(records, gap_minutes)
    pair_counts, skill_episodes = count_cooccurrence(episodes)
    existing = load_existing_compose()
    candidates = propose(pair_counts, skill_episodes, existing, min_co, top)
    return {
        "discovered_at": datetime.now().strftime(TS_FMT),
        "gap_minutes": gap_minutes,
        "min_co_occurrences": min_co,
        "ledger_records": len(records),
        "total_episodes": len(episodes),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    cfg = _load_config()
    ap = argparse.ArgumentParser(
        description="Mine the skill-invocations ledger for co-invocation relation candidates")
    ap.add_argument("--apply", action="store_true",
                    help="Persist candidates to world/skill-relations.yaml (default: dry-run)")
    ap.add_argument("--gap-minutes", type=float,
                    default=cfg.get("ledger_discover_gap_minutes", DEFAULT_GAP_MINUTES),
                    help="Episode split threshold in minutes")
    ap.add_argument("--min-co-occurrences", type=int,
                    default=cfg.get("ledger_discover_min_co_occurrences", DEFAULT_MIN_CO),
                    help="Minimum episodes a pair must co-occur in to be proposed")
    ap.add_argument("--top", type=int,
                    default=cfg.get("ledger_discover_top", DEFAULT_TOP),
                    help="Cap candidate count (0 = no cap)")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    args = ap.parse_args(argv)

    payload = discover(args.gap_minutes, args.min_co_occurrences, args.top)

    if args.apply:
        data = _read_yaml(WORLD_RELATIONS_PATH)
        if not isinstance(data, dict):
            data = {}
        data["co_invocation_candidates"] = payload
        _write_yaml_atomic(WORLD_RELATIONS_PATH, data)

    if args.output == "human":
        print("co-invocation discovery: {} candidates from {} episodes "
              "({} ledger records, gap={}min, min_co={}){}".format(
                  payload["candidate_count"], payload["total_episodes"],
                  payload["ledger_records"], payload["gap_minutes"],
                  payload["min_co_occurrences"],
                  " [APPLIED]" if args.apply else " [dry-run]"))
        for c in payload["candidates"]:
            print("  {:<28} + {:<28} co={:<3} jaccard={}".format(
                c["source"], c["target"], c["co_occurrence_episodes"], c["confidence"]))
    else:
        summary = {k: payload[k] for k in (
            "discovered_at", "gap_minutes", "min_co_occurrences",
            "ledger_records", "total_episodes", "candidate_count")}
        summary["applied"] = bool(args.apply)
        summary["candidates"] = payload["candidates"]
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
