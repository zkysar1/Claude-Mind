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

STORE BOUNDARY (decided g-115-4371, 2026-08-01, zeta, cc-02 / Linux
6.8.0-136-generic -- measured, not inherited). The scanner covered 3 stores and
carried signatures from ONE leaking test (test_asp_id_auto_allocation.py). A
census of every candidate world JSONL store found 53 fixture-shaped records, 38
of them status=active, and the scanner reported PASS on all of them. Both
dimensions were stale, and the store dimension was the LESS important one:
pipeline.jsonl was ALREADY covered and still carried 7 undetected fixtures,
because the signature set had never been extended when new world-writing tests
landed. So the fix is signature-first (RECORD_FIELD_SIGNATURES below, applied to
every store including the pre-existing three) and store-second.

IN scope -- the retrieval-bearing governed stores an agent reasons FROM, where a
leaked row can silently become input to a decision:
  aspirations(+archive) . pipeline . reasoning-bank(+archive) . guardrails(+archive)
  . pattern-signatures(+archive)

OUT of scope, deliberately:
  * board channels -- measured clean at 14,069 rows across all 5. Board ids and
    bodies are timestamp+agent derived (msg-YYYYMMDD-HHMMSS-<agent>-NNNN), so a
    fixture cannot collide into one the way an auto-allocated rb-{max+1} can.
    An informative zero, not a vacuous one (rb-245).
  * the ~50 telemetry / metrics / ledger / override JSONL stores (*-metrics,
    *-log, *-overrides, changelog, retrieval-trace, ...). Append-only
    instrumentation with no retrieval surface: a leaked row there is never read
    back INTO the agent's reasoning, so it cannot mislead. Scanning them would
    trade the whole point of a curated high-confidence signal for noise.
  * meta/spark-questions.jsonl -- DOES have a retrieval surface (candidate
    promotion), so it fails the out-of-scope test above on the merits. It is out
    only on cost/benefit: measured 29 rows, 1 fixture row (sq-310, text "What
    happens when X?" from _spark_question_rec), and it is ALREADY status=retired
    -- 0 active. Covering it needs a structural change, because scan() is
    world_dir-scoped and META_DIR is an independently-configured external path
    (.claude/rules/path-resolution.md: never derive one from the other). Build
    that seam when a spark-question fixture is found ACTIVE, not before.
Re-decide this boundary if a store ever gains a retrieval surface -- the test
is "can a row here reach an agent's reasoning?", not "is it a JSONL file?".

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

# ---------------------------------------------------------------------------
# Store-AGNOSTIC record-field signatures (g-115-4371). Unlike FIXTURE_SIGNATURES
# above -- which is scoped to the aspiration/goal SHAPE -- each entry here is an
# exact match on one named field of ANY record in ANY scanned store. That is the
# right shape for the second leak family, whose record builders live in
# mind_api/tests and hardcode self-declaring literals:
#   test_runtime_store_rbguard.py     _rb_rec()     -> reasoning-bank.jsonl
#                                     _guard_rec()  -> guardrails.jsonl
#   test_runtime_store_patsig_spark.py / test_wrapper_patsig_spark.py
#                                     _patsig_rec() -> pattern-signatures.jsonl
#
# Why field-exact and not id-shape: a builder called WITHOUT an explicit id gets
# auto-allocated <prefix>-{max+1} against the PRODUCTION max, so the fixture
# takes a real next-in-sequence id. Measured g-115-4371: an id-collision probe
# saw 10 of 25 leaked reasoning-bank rows and 8 of 15 guardrails rows, and its
# hits were dominated by FALSE positives (tests legitimately name real ids like
# asp-115). Content is the only reliable signal -- the same conclusion this
# module's header already drew for asp-338..343, now confirmed on a second family.
_FIELD_SIGNATURES_SRC_RBGUARD = "mind_api/tests/test_runtime_store_rbguard.py"
_FIELD_SIGNATURES_SRC_PATSIG = "mind_api/tests/test_runtime_store_patsig_spark.py"
# `test-cat` / `test-guard` are SHARED house literals, not one file's: measured
# 2026-08-01 in 10 and 3 mind_api/tests files respectively (the pipeline family
# -- test_runtime_pipeline_writers, test_wrapper_pipeline, test_runtime_pipeline_
# archive, test_pipeline_surprise_derived -- all use test-cat too). `source` is
# what a triager opens to fix the leaking test, so naming a single file here
# would send them to the wrong one for most hits.
_FIELD_SIGNATURES_SRC_HOUSE = "mind_api/tests (shared house fixture literal)"

RECORD_FIELD_SIGNATURES = [
    # _rb_rec()
    {"field": "category", "value": "test-cat",
     "source": _FIELD_SIGNATURES_SRC_HOUSE, "confidence": "high"},
    {"field": "title", "value": "Test RB entry",
     "source": _FIELD_SIGNATURES_SRC_RBGUARD, "confidence": "high"},
    {"field": "content", "value": "A test reasoning-bank entry.",
     "source": _FIELD_SIGNATURES_SRC_RBGUARD, "confidence": "high"},
    # _guard_rec()
    {"field": "category", "value": "test-guard",
     "source": _FIELD_SIGNATURES_SRC_HOUSE, "confidence": "high"},
    {"field": "rule", "value": "always test before deploy",
     "source": _FIELD_SIGNATURES_SRC_RBGUARD, "confidence": "high"},
    {"field": "source", "value": "wave-2-test",
     "source": _FIELD_SIGNATURES_SRC_RBGUARD, "confidence": "high"},
    {"field": "trigger_condition", "value": "before any deploy",
     "source": _FIELD_SIGNATURES_SRC_RBGUARD, "confidence": "medium"},
    # _patsig_rec()
    {"field": "name", "value": "test pattern",
     "source": _FIELD_SIGNATURES_SRC_PATSIG, "confidence": "high"},
    {"field": "description", "value": "a test pattern signature",
     "source": _FIELD_SIGNATURES_SRC_PATSIG, "confidence": "high"},
    {"field": "expected_outcome", "value": "outcome-x",
     "source": _FIELD_SIGNATURES_SRC_PATSIG, "confidence": "medium"},
]

# Governed tombstone statuses. A record already retired/archived is a leak that
# was HANDLED; re-flagging it every 24h run is pure re-triage noise a reader must
# dismiss each cycle. Live detection is preserved because a FRESH leak lands with
# whatever status the leaking test wrote (active, or none at all). Same rule the
# aspiration scan applies at the aspiration level (g-115-2723).
TOMBSTONE_STATUSES = ("retired", "archived")

# Pre-compile regex signatures once.
for _sig in FIXTURE_SIGNATURES:
    if _sig["kind"] == "regex":
        _sig["_re"] = re.compile(_sig["value"])


def _is_tombstone(rec: dict) -> bool:
    return str(rec.get("status", "")).strip().lower() in TOMBSTONE_STATUSES


def _match_record_fields(rec: dict) -> list[dict]:
    """Return the RECORD_FIELD_SIGNATURES matched by this record's fields."""
    hits = []
    for sig in RECORD_FIELD_SIGNATURES:
        val = rec.get(sig["field"])
        if val is None:
            continue
        if str(val).strip() == sig["value"]:
            hits.append(sig)
    return hits


def _field_suspects(rec: dict, store_label: str, record_id: str) -> list[dict]:
    """Emit suspect rows for every RECORD_FIELD_SIGNATURES match on `rec`."""
    return [{
        "store": store_label, "record_id": record_id,
        "field": sig["field"], "value": str(rec.get(sig["field"]))[:80],
        "matched": sig["value"], "confidence": sig["confidence"],
        "source": sig["source"],
    } for sig in _match_record_fields(rec)]


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
        if _is_tombstone(asp):
            continue
        suspects += _field_suspects(asp, store_label, asp_id)
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
            suspects += _field_suspects(g, store_label, f"{asp_id}/{gid}")
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
        # NO _is_tombstone() skip here, deliberately: for the pipeline "archived"
        # is a NORMAL lifecycle status (discovered/active/resolved/archived), not
        # the governed remediation marker it is for an aspiration or an rb/guard
        # row. Skipping it would suppress detection of every fixture that reached
        # archived naturally -- and would make pipeline-archive.jsonl vacuous.
        # g-115-4371: the record-field pass is what actually catches this store's
        # live leak. pipeline.jsonl was in the covered set from day one and still
        # held 7 undetected fixtures (2026-05-14_test-hyp, 2026-07-29_census-a..d,
        # ...) carrying category="test-cat" -- invisible to the title/desc pass
        # below, which only knows the asp-338..343 family. Being SCANNED is not
        # being COVERED when the signature set is a generation behind.
        suspects += _field_suspects(rec, store_label, rid)
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


def _scan_flat(path: Path, store_label: str) -> list[dict]:
    """Scan a FLAT JSONL store (one self-contained record per line, no nesting):
    reasoning-bank, guardrails, pattern-signatures and their archives. These
    carry no aspiration/goal shape, so only the store-agnostic
    RECORD_FIELD_SIGNATURES apply."""
    suspects: list[dict] = []
    for rec in _snapshot(path):
        if not isinstance(rec, dict) or _is_tombstone(rec):
            continue
        suspects += _field_suspects(rec, store_label, rec.get("id", "?"))
    return suspects


# Single source of truth for coverage: scan() iterates this, and --json reports
# it. Keeping the reported list separate from the scanned list is how a scanner
# comes to under-report its own coverage (guard-1760) -- one constant, no drift.
SCANNED_STORES: list[tuple[str, str]] = [
    ("aspirations.jsonl", "aspirations"),
    # The archive can also carry a leaked fixture (a resurrected/archived one).
    ("aspirations-archive.jsonl", "aspirations"),
    ("pipeline.jsonl", "pipeline"),
    ("pipeline-archive.jsonl", "pipeline"),
    ("reasoning-bank.jsonl", "flat"),
    ("reasoning-bank-archive.jsonl", "flat"),
    ("guardrails.jsonl", "flat"),
    ("guardrails-archive.jsonl", "flat"),
    ("pattern-signatures.jsonl", "flat"),
    ("pattern-signatures-archive.jsonl", "flat"),
]

_SCANNERS = {
    "aspirations": _scan_aspirations,
    "pipeline": _scan_pipeline,
    "flat": _scan_flat,
}


def scan(world_dir: str | None = None) -> list[dict]:
    """Scan the production governed stores for leaked test fixtures.
    Returns a list of suspect dicts. `world_dir` overrides WORLD_DIR (tests)."""
    base = Path(world_dir) if world_dir else (Path(WORLD_DIR) if WORLD_DIR else None)
    if base is None:
        return []
    suspects: list[dict] = []
    for name, kind in SCANNED_STORES:
        suspects += _SCANNERS[kind](base / name, f"world/{name}")
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
            "scanned": [f"world/{n}" for n, _ in SCANNED_STORES],
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
