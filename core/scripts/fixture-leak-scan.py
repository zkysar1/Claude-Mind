#!/usr/bin/env python3
# domain-leak-exempt: scanner enumerates test-fixture title/motivation literals by design
r"""Fixture-leak scanner (guard-955 defense-in-depth, Layer-C detective).

A "fixture leak" is a TEST-fixture record that ended up in a PRODUCTION store.
Canonical incident (g-115-2054, 2026-07-12): a pytest suite run on an own-cloud
box whose conftest STORAGE_BACKEND=local pin was absent/stale minted the
world-writing fixtures of test_asp_id_auto_allocation.py (asp-338..343, titled
"auto-minted"/"parallel filer 0-3", motivation "g-328-29 fixture", goals "first
goal"/"second goal") AGAINST the production aspirations store — the guard-955
S3-key-collision class (OwnCloudBackend derives the S3 key from
customer_prefix+env_id+basename, NOT the MIND_WORLD tmp override, so a tmp-world
subprocess write lands on the PRODUCTION key). That incident also truncated the
live store (rb-2983 / exp-owncloud-s3-collision-truncation-2026-07-09).

guard-955 + the g-115-1875 conftest autouse pin are the AUTHORING-time gate
(prevent the leak). This scanner is the missing DETECTIVE layer: it scans the
production stores for fixtures that ALREADY leaked (or leaked from a box whose
pin was stale), so a leak is surfaced within one hygiene cadence instead of
silently corrupting retrieval / id-allocation until someone notices.

Distinct from timebomb-fixture-scan.py (guard-566: aging ISO literals in TEST
files) — that scans the working tree's test sources; THIS scans the live
production JSONL stores for leaked fixture CONTENT.

Why advisory (exit 0 by default), not a fail-loud gate: a curated content
signature can, in rare cases, collide with a genuine production record (a real
goal a human happened to title "first goal"). A hard gate would either force
noise-suppression or be ignored. So the default posture is REPORT; enforcement
is opt-in via --exit-on-hits (for a recurring goal that should file a board
finding, or a CI check). The signatures are curated to be high-confidence — the
strongest (motivation "g-328-29 fixture", title /^parallel filer \d+$/) are
strings a real production record would essentially never carry — so a hit is a
near-certain leak worth triaging, not alarm-fatigue noise.

On a hit, the remediation is NOT to bulk-delete: follow archive-before-delete
(.claude/rules/archive-before-delete.md) + the governed-ops path g-115-2054 used
(retire via aspirations status, re-close resurrected archives), AND investigate
the leaking test run (which box, was the STORAGE_BACKEND=local pin present).

Usage:
  py -3 core/scripts/fixture-leak-scan.py               # scan prod stores, report, exit 0
  py -3 core/scripts/fixture-leak-scan.py --json        # machine-readable
  py -3 core/scripts/fixture-leak-scan.py --exit-on-hits # exit 1 if any leak (recurring/CI)
  py -3 core/scripts/fixture-leak-scan.py --world-dir <path>  # scan a specific world (tests)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Curated fixture signatures. Each entry names a field SCOPE + a match KIND +
# the literal/pattern. Seeded from the canonical world-writing test
# (test_asp_id_auto_allocation.py, g-328-29). When a NEW world-writing test is
# identified (its fixtures could leak the same way), add its distinctive
# title/motivation strings here. Keep signatures HIGH-CONFIDENCE — strings a
# real production record would essentially never carry — because the scanner is
# advisory and a human triages each hit.
#
# id-shape (asp-338..343 "minted against production max") is deliberately NOT a
# signature: a leaked fixture id is indistinguishable from a real asp-NNN id, so
# the CONTENT (title/motivation/description) is the only reliable signal.
# ---------------------------------------------------------------------------
_SCOPE_ASP_TITLE = "asp_title"
_SCOPE_ASP_MOTIVATION = "asp_motivation"
_SCOPE_GOAL_TITLE = "goal_title"
_SCOPE_GOAL_DESC = "goal_desc"

FIXTURE_SIGNATURES = [
    # --- test_asp_id_auto_allocation.py (g-328-29) — the canonical incident ---
    # Motivation "g-328-29 fixture" is the single strongest marker (a real
    # aspiration would never carry it as its whole motivation).
    {"scope": _SCOPE_ASP_MOTIVATION, "kind": "exact", "value": "g-328-29 fixture",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_ASP_MOTIVATION, "kind": "exact", "value": "occupies the live max",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "regex", "value": r"^parallel filer \d+$",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "exact", "value": "via auto literal",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "exact", "value": "via empty string",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "exact", "value": "auto-minted",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "exact", "value": "existing live aspiration",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "exact", "value": "dup live",
     "source": "test_asp_id_auto_allocation.py", "confidence": "medium"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "exact", "value": "dup archived",
     "source": "test_asp_id_auto_allocation.py", "confidence": "medium"},
    {"scope": _SCOPE_ASP_TITLE, "kind": "exact", "value": "after explicit",
     "source": "test_asp_id_auto_allocation.py", "confidence": "medium"},
    {"scope": _SCOPE_GOAL_DESC, "kind": "exact", "value": "auto-minted goal",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_GOAL_TITLE, "kind": "exact", "value": "carries an id",
     "source": "test_asp_id_auto_allocation.py", "confidence": "high"},
    {"scope": _SCOPE_GOAL_TITLE, "kind": "exact", "value": "first goal",
     "source": "test_asp_id_auto_allocation.py", "confidence": "medium"},
    {"scope": _SCOPE_GOAL_TITLE, "kind": "exact", "value": "second goal",
     "source": "test_asp_id_auto_allocation.py", "confidence": "medium"},
    {"scope": _SCOPE_GOAL_TITLE, "kind": "exact", "value": "Seed goal",
     "source": "test_asp_id_auto_allocation.py", "confidence": "medium"},
]

# Pre-compile regex signatures once.
for _sig in FIXTURE_SIGNATURES:
    if _sig["kind"] == "regex":
        _sig["_re"] = re.compile(_sig["value"])


def _match(scope: str, value: str) -> list[dict]:
    """Return the signatures matched by `value` for the given field `scope`."""
    if value is None:
        return []
    v = str(value)
    hits = []
    for sig in FIXTURE_SIGNATURES:
        if sig["scope"] != scope:
            continue
        if sig["kind"] == "exact":
            if v == sig["value"]:
                hits.append(sig)
        elif sig["kind"] == "regex":
            if sig["_re"].search(v):
                hits.append(sig)
    return hits


def _snapshot(path: Path) -> list[dict]:
    """Authoritative read: force-fresh-from-backend then parse with the SAME
    recovery reader jsonl_hygiene / the daemon use. On own-cloud, a raw local
    read can trail the S3 copy by seconds (rb-3205), so a hygiene scan must read
    through the backend to avoid scanning a stale mirror. Best-effort refresh
    (skips per-machine stores that refresh would clobber). Empty list if absent."""
    try:
        from storage_backend import get_backend
        import owncloud_sync
        be = get_backend()
        if not owncloud_sync.refresh_would_clobber(be, path):
            be.refresh(path)
    except Exception as e:  # noqa: BLE001 - refresh is best-effort
        print(f"[fixture-leak-scan] (refresh skipped for {path.name}: {e})",
              file=sys.stderr)
    if not path.exists():
        return []
    try:
        from _fileops import read_jsonl_with_recovery
        return read_jsonl_with_recovery(path)
    except Exception:  # noqa: BLE001 - fall back to a plain skip-malformed parse
        out = []
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


def _scan_aspirations(path: Path, store_label: str) -> list[dict]:
    """Scan an aspirations JSONL: each line is an aspiration with nested goals."""
    suspects: list[dict] = []
    for asp in _snapshot(path):
        if not isinstance(asp, dict):
            continue
        asp_id = asp.get("id", "?")
        # g-115-2723: skip governed tombstones. A retired/archived aspiration is
        # a leak that was ALREADY handled via aspirations status (the g-328-29
        # asp-338..349 batch, retired 2026-07-14 per g-115-2056/2063/2145) —
        # re-flagging its title/motivation/goals every 24h run is pure re-triage
        # noise a reader must dismiss each cycle. Live-leak detection is
        # PRESERVED: a FRESH leak never carries retired/archived status (it lands
        # with whatever status the leaking test wrote — active/completed), so a
        # real leak in either store stays in scope. Aspiration-level status is
        # the reliable marker (goals under a retired asp can still read
        # `pending`, so goal.status is NOT a tombstone signal — skip at the
        # aspiration level, which also drops its nested goals).
        if str(asp.get("status", "")).strip().lower() in ("retired", "archived"):
            continue
        for scope, field in ((_SCOPE_ASP_TITLE, "title"),
                             (_SCOPE_ASP_MOTIVATION, "motivation")):
            for sig in _match(scope, asp.get(field)):
                suspects.append({
                    "store": store_label, "record_id": asp_id,
                    "field": field, "value": str(asp.get(field))[:80],
                    "matched": sig["value"], "confidence": sig["confidence"],
                    "source": sig["source"],
                })
        for g in asp.get("goals", []) or []:
            if not isinstance(g, dict):
                continue
            gid = g.get("id", "?")
            for scope, field in ((_SCOPE_GOAL_TITLE, "title"),
                                 (_SCOPE_GOAL_DESC, "description")):
                for sig in _match(scope, g.get(field)):
                    suspects.append({
                        "store": store_label, "record_id": f"{asp_id}/{gid}",
                        "field": f"goal.{field}", "value": str(g.get(field))[:80],
                        "matched": sig["value"], "confidence": sig["confidence"],
                        "source": sig["source"],
                    })
    return suspects


def _scan_pipeline(path: Path, store_label: str) -> list[dict]:
    """Scan a pipeline JSONL: each line is a hypothesis record. Fixture leaks
    into the pipeline share the same content signatures (title/description); the
    pipeline is checked against the SAME signature set (any-field text match)."""
    suspects: list[dict] = []
    for rec in _snapshot(path):
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id", "?")
        # Pipeline records have no goals/motivation; check their text fields
        # against the title/desc-scoped signatures (a leaked fixture title would
        # match regardless of which store it landed in).
        for field in ("title", "hypothesis", "description", "summary"):
            val = rec.get(field)
            if val is None:
                continue
            for scope in (_SCOPE_ASP_TITLE, _SCOPE_GOAL_TITLE, _SCOPE_GOAL_DESC):
                for sig in _match(scope, val):
                    suspects.append({
                        "store": store_label, "record_id": rid,
                        "field": field, "value": str(val)[:80],
                        "matched": sig["value"], "confidence": sig["confidence"],
                        "source": sig["source"],
                    })
    return suspects


def scan(world_dir: str | None = None) -> list[dict]:
    """Scan the production aspiration + pipeline stores for leaked test fixtures.
    Returns a list of suspect dicts. `world_dir` overrides WORLD_DIR (tests)."""
    base = Path(world_dir) if world_dir else (Path(WORLD_DIR) if WORLD_DIR else None)
    if base is None:
        return []
    suspects: list[dict] = []
    suspects += _scan_aspirations(base / "aspirations.jsonl", "world/aspirations.jsonl")
    # The archive can also carry a leaked fixture (a resurrected/archived one).
    suspects += _scan_aspirations(base / "aspirations-archive.jsonl",
                                  "world/aspirations-archive.jsonl")
    suspects += _scan_pipeline(base / "pipeline.jsonl", "world/pipeline.jsonl")
    return suspects


def main() -> int:
    ap = argparse.ArgumentParser(
        description="guard-955 fixture-leak scanner (advisory detective).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--exit-on-hits", action="store_true",
                    help="return exit 1 when any leaked fixture is found "
                         "(recurring-goal / CI use)")
    ap.add_argument("--world-dir", default=None,
                    help="scan a specific world dir instead of WORLD_DIR (tests)")
    args = ap.parse_args()

    suspects = scan(world_dir=args.world_dir)

    if args.json:
        print(json.dumps({
            "scanned": ["world/aspirations.jsonl", "world/aspirations-archive.jsonl",
                        "world/pipeline.jsonl"],
            "suspect_count": len(suspects),
            "suspects": suspects,
        }, indent=2))
    else:
        if not suspects:
            print("[fixture-leak-scan] PASS -- 0 leaked test fixtures in production "
                  "stores (guard-955 clean).")
        else:
            print(f"[fixture-leak-scan] {len(suspects)} suspected leaked fixture(s) "
                  "-- a TEST fixture appears in a PRODUCTION store (guard-955 leak):")
            for s in suspects:
                print(f"  {s['store']}  {s['record_id']}  {s['field']}="
                      f"{s['value']!r}  [matched {s['matched']!r}, "
                      f"{s['confidence']} conf, from {s['source']}]")
            print("  Remediation: do NOT bulk-delete. Follow archive-before-delete "
                  "+ governed ops (retire via aspirations status; re-close any "
                  "resurrected archive). Investigate the leaking run: which box, "
                  "was the STORAGE_BACKEND=local conftest pin present (guard-955)?")

    if args.exit_on_hits and suspects:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
