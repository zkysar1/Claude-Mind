#!/usr/bin/env python3
"""Backfill null goal_id on experience records from the id (7).

Many experience records carry an id that embeds the owning goal-id
(exp-g-NNN-NN-{slug}, as built by experience.py cmd_archive_goal) while the
goal_id FIELD is null — a caller-formed cmd_add record that never set the field.
The daemon --goal read filter matches on the stored field
(mind_api/src/endpoints/experience.py: `rec.get("goal_id") == goal`), so those
records are invisible to `experience-read --goal <id>` even though their id names
the goal. This one-off, idempotent, ADDITIVE backfill sets goal_id from the id
where the id embeds a canonical goal-id; slug-only ids (genuinely goal-less
experiences: exp-encode-session-*, exp-577-behavioral-*) are left null.

Companion to the forward fix in experience.py normalize_record (which prevents
NEW null-goal_id records). This script repairs EXISTING records at rest so the
daemon read finds them without any daemon change.

Non-destructive: writes goal_id ONLY when it is currently null/empty AND the id
yields a goal-id. NEVER overwrites a present goal_id. Re-running is a no-op. Does
NOT validate records (old records may reference a deleted content_path .md — the
backfill must not fail on those; it only touches the goal_id field). Uses
locked_modify_jsonl (the same lock the daemon writes under) so a concurrent agent
write cannot corrupt the file.

Usage:
    experience-backfill-goal-id.py [--all-agents | --agent NAME] [--apply] [--json]
Default is DRY-RUN on the current agent (MIND_AGENT). --apply writes.
"""
import argparse
import json
import os
import sys

# This migration touches ONLY git-tracked local agent files
# (agents/<agent>/experience{,-archive}.jsonl) — never a governed world/meta
# path — so force the local storage backend. Without this, a bare `py -3`
# invocation under the cc-04 default STORAGE_BACKEND=own-cloud aborts in
# _fileops.locked_modify_jsonl → get_backend() (OwnCloudBackend.from_env needs
# MIND_WORLD/MIND_META, which a non-sourced subprocess lacks). Set BEFORE the
# _fileops import so the backend never resolves to own-cloud. (7 / )
os.environ["STORAGE_BACKEND"] = "local"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import agents_root  # noqa: E402
from experience import derive_goal_id_from_id  # noqa: E402
from _fileops import locked_modify_jsonl  # noqa: E402


def _read_jsonl_plain(path):
    recs = []
    if not os.path.exists(path):
        return recs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _plan_file(path):
    """Return count of records that WOULD be backfilled (dry-run)."""
    n = 0
    for rec in _read_jsonl_plain(path):
        if rec.get("goal_id"):
            continue
        if derive_goal_id_from_id(rec.get("id")):
            n += 1
    return n


def _apply_file(path):
    """Backfill under lock. Returns count changed."""
    if not os.path.exists(path):
        return 0
    changed = {"n": 0}

    def _modifier(items):
        for rec in items:
            if rec.get("goal_id"):
                continue
            derived = derive_goal_id_from_id(rec.get("id"))
            if derived:
                rec["goal_id"] = derived
                changed["n"] += 1
        return items

    locked_modify_jsonl(path, _modifier)
    return changed["n"]


def _agent_files(agent_dir):
    return [
        os.path.join(agent_dir, "experience.jsonl"),
        os.path.join(agent_dir, "experience-archive.jsonl"),
    ]


def main():
    ap = argparse.ArgumentParser(description="Backfill null goal_id from experience id (g-115-1917)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--all-agents", action="store_true", help="Sweep every agent dir")
    grp.add_argument("--agent", type=str, help="Target a specific agent by name")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    root = agents_root()
    targets = []  # (agent_name, [paths])
    if args.all_agents:
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if os.path.isdir(d) and any(os.path.exists(p) for p in _agent_files(d)):
                targets.append((name, _agent_files(d)))
    else:
        name = args.agent or os.environ.get("MIND_AGENT", "")
        if not name:
            print("No agent: pass --agent NAME, --all-agents, or set MIND_AGENT", file=sys.stderr)
            sys.exit(2)
        targets.append((name, _agent_files(os.path.join(root, name))))

    report = {"mode": "apply" if args.apply else "dry-run", "agents": {}, "total": 0}
    for name, paths in targets:
        per_file = {}
        for p in paths:
            n = _apply_file(p) if args.apply else _plan_file(p)
            per_file[os.path.basename(p)] = n
        report["agents"][name] = per_file
        report["total"] += sum(per_file.values())

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        verb = "backfilled" if args.apply else "would backfill"
        print(f"experience-backfill-goal-id [{report['mode']}]: {verb} {report['total']} record(s)")
        for name, pf in report["agents"].items():
            if sum(pf.values()):
                print(f"  {name}: {sum(pf.values())}  {pf}")


if __name__ == "__main__":
    main()
