#!/usr/bin/env python3
"""phantom-goal-audit — detect phantom goals: null created_at provenance.

g-115-2733. A goal created via the sanctioned ``aspirations-add-goal.sh`` path
ALWAYS carries ``created_at`` (stamped inside the file lock). A goal reaching
the world queue with ``created_at`` null/missing bypassed that path — e.g. a
cross-box LWW-merge phantom (g-315-416, found by g-115-2730) or a raw
injection. Harm is low BY DESIGN: guard-33 (curriculum promotion) is
record-gated not directive-gated, so a provenance-less goal is harmlessly
auto-skipped and no promotion-bypass occurs. This is DEFENSE-IN-DEPTH
data-integrity hardening, not a security gate.

DESIGN (first-principles, refined from the g-115-2733 proposal):
- PHANTOM SIGNATURE is ALL-null provenance: ``created_at`` AND ``filed_by``
  AND ``goal_source`` all null/missing — the goal bypassed the sanctioned path
  ENTIRELY (g-315-416, g-115-2730). ``created_at``-null ALONE is NOT sufficient:
  ~52 LEGACY goals predate the created_at convention and carry PARTIAL
  provenance (filed_by/goal_source present) — harmless history, not phantoms.
  Verified 2026-07-19: requiring all-null gives 0 false positives on the legacy
  corpus (0 all-null vs 52 created_at-null). The proposal's "no matching
  changelog write" does NOT map: ``world/changelog.jsonl`` records FILE-level
  writes (``{timestamp,agent,file,action,summary}``), not per-goal-id events, so
  a goal id cannot be cross-referenced there.
- DETECTIVE by default (JSON to stdout), mirroring silent-gap-audit.py.
  ``--apply`` files ONE consolidated Investigate goal (dedup'd) when NEW
  phantoms are present.
- rb-245 ZERO-COUNT VERIFICATION: before flagging "N goals have null
  created_at", verify ``created_at`` is a REAL populated field by finding >=1
  goal that HAS it. If NO goal in the corpus carries ``created_at``, the field
  name is wrong/renamed — abort with a schema warning rather than false-flag
  every goal.
- TRANSIENCE TOLERANCE: phantoms self-reconcile via the next LWW merge
  (g-315-416 now carries provenance). The audit re-runs on cadence; a transient
  mid-merge null clears itself and dedup makes re-filing idempotent — so a
  single flag is advisory, and only a persistent phantom keeps surfacing.

Reads the LOCAL world/aspirations.jsonl (detective audit; a stale own-cloud
mirror self-corrects on the next cadence run). Exit 0 always (detective).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR  # noqa: E402

ORIGIN_PREFIX = "phantom-goal-audit"


def _is_null(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def _iter_world_goals(world_dir):
    """Yield (aspiration_id, goal_dict) for every goal in world/aspirations.jsonl."""
    if not world_dir:
        return
    p = Path(world_dir) / "aspirations.jsonl"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
        except Exception:
            continue
        aid = asp.get("id", "?")
        for g in asp.get("goals", []) or []:
            if isinstance(g, dict):
                yield aid, g


def audit(world_dir=WORLD_DIR):
    goals = list(_iter_world_goals(world_dir))
    scanned = len(goals)
    if scanned == 0:
        return {"subcommand": "audit", "summary": "audit: no world goals read",
                "flags": [], "scanned": 0, "schema_verified": False, "phantoms": []}

    # rb-245: created_at must be a REAL populated field on the corpus.
    have_created_at = sum(1 for _, g in goals if not _is_null(g.get("created_at")))
    if have_created_at == 0:
        return {"subcommand": "audit",
                "summary": ("audit: SCHEMA UNVERIFIED — 0/%d goals carry created_at; "
                            "field renamed? aborting to avoid false-flagging every goal "
                            "(rb-245)" % scanned),
                "flags": ["schema_unverified"], "scanned": scanned,
                "schema_verified": False, "phantoms": []}

    # Dedup marker: an already-open phantom-goal-audit Investigate suppresses re-file.
    already_filed = any(
        (g.get("status") in ("pending", "in-progress"))
        and str(g.get("origin_signal") or "").startswith(ORIGIN_PREFIX)
        for _, g in goals
    )

    phantoms = []
    legacy_null_created_at = 0  # created_at null but partial provenance present
    for aid, g in goals:
        if not _is_null(g.get("created_at")):
            continue
        fb_null = _is_null(g.get("filed_by_agent") or g.get("filed_by"))
        gs_null = _is_null(g.get("goal_source"))
        if fb_null and gs_null:
            # ALL-null provenance — bypassed the sanctioned path entirely (phantom).
            phantoms.append({
                "goal_id": g.get("id"),
                "aspiration_id": aid,
                "title": (g.get("title") or "")[:80],
                "status": g.get("status"),
                "live": g.get("status") in ("pending", "in-progress"),
            })
        else:
            # created_at null but filed_by/goal_source present — a LEGACY goal
            # predating the created_at convention, NOT a phantom.
            legacy_null_created_at += 1

    live_n = sum(1 for p in phantoms if p.get("live"))
    summary = ("audit: %d phantom goal(s) [all-null provenance; %d LIVE] of %d scanned "
               "(%d legacy null-created_at with partial provenance = NOT phantoms; "
               "schema verified: %d carry created_at)"
               % (len(phantoms), live_n, scanned, legacy_null_created_at, have_created_at))
    return {"subcommand": "audit", "summary": summary,
            "flags": ["phantom_goals_found"] if phantoms else [],
            "scanned": scanned, "schema_verified": True,
            "have_created_at": have_created_at,
            "legacy_null_created_at": legacy_null_created_at,
            "live_phantoms": live_n,
            "already_filed_open_investigate": already_filed,
            "phantoms": phantoms}


def apply_file(result):
    """File ONE consolidated Investigate for NEW phantoms (dedup'd). Fail-open."""
    phantoms = result.get("phantoms") or []
    if not phantoms:
        result["apply"] = "no phantoms — nothing to file"
        return result
    if result.get("already_filed_open_investigate"):
        result["apply"] = "dedup — an open phantom-goal-audit Investigate already exists"
        return result
    ids = ", ".join(str(p.get("goal_id")) for p in phantoms)
    goal = {
        "title": ("Investigate: %d phantom goal(s) with null created_at provenance in world queue"
                  % len(phantoms)),
        "description": ("phantom-goal-audit (g-115-2733) flagged %d world goal(s) with null "
                        "created_at — created outside the sanctioned aspirations-add-goal.sh path "
                        "(likely cross-box LWW-merge phantoms; harm low by design, guard-33 is "
                        "record-gated). Goal ids: %s. Verify each: is it a transient mid-merge "
                        "null (re-run the audit — self-reconciles) or a persistent injection? "
                        "created_at-null is the canonical signal (changelog is file-level, not "
                        "goal-id-level, so it cannot corroborate per-goal)." % (len(phantoms), ids)),
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-hardening",
        "origin_signal": "%s:%d-phantoms" % (ORIGIN_PREFIX, len(phantoms)),
    }
    try:
        script = str(Path(__file__).resolve().parent / "aspirations-add-goal.sh")
        from _runtime_bash import BASH  # rb-1472: not bare "bash"
        proc = subprocess.run(
            [BASH, script, "--source", "world", "asp-115"],
            input=json.dumps(goal), capture_output=True, text=True, timeout=60,
        )
        result["apply"] = ("filed Investigate" if proc.returncode == 0
                           else "file FAILED rc=%d: %s" % (proc.returncode, (proc.stderr or proc.stdout)[:200]))
    except Exception as e:  # fail-open — detection already succeeded
        result["apply"] = "file ERROR: %s" % e
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Detect phantom goals (null created_at provenance)")
    ap.add_argument("subcommand", nargs="?", default="audit", choices=["audit"])
    ap.add_argument("--apply", action="store_true",
                    help="file one consolidated Investigate for NEW phantoms (dedup'd)")
    ap.add_argument("--world-dir", default=WORLD_DIR, help="override world dir (tests)")
    args = ap.parse_args(argv)

    result = audit(world_dir=args.world_dir)
    if args.apply:
        result = apply_file(result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
