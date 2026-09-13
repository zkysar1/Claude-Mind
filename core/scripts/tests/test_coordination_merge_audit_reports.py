"""Pins the world/audit-reports/*.jsonl union-merge registration ().

Before this branch existed `merge_handler_for` returned None for every
audit-report, so the own-cloud sweep saw both-diverged and SKIPPED the file on
every pass. Measured on one live file: local 61 lines vs store 60 with only 8
SHARED — whichever side pushed next would have destroyed ~52 rows of
repo-hygiene findings silently. That sweep is what a PR-merge readiness check
reads across 61 repos, so the loss makes the gate report clean on stale data.

The governing invariant is BYTE commutativity, not content commutativity
(guard-4641): set-equality, sorted-line equality and parsed-record equality all
PASS on an output whose order depends on argument order, and such a handler
converges in every content test while still re-diverging in production.
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import coordination_merge as cm  # noqa: E402

WORLD_REL = "world/audit-reports/repo-hygiene-2026-09-11.jsonl"
MIND_DATA = "/opt/ayoai-mind/.mind-data/world/audit-reports/repo-hygiene-2026-09-11.jsonl"


def _lines(blob):
    return [l for l in blob.decode().splitlines() if l.strip()]


# --- outcome 1: registered, via the one-lookup classifier (never a dict grep) ---

def test_audit_report_jsonl_is_registered_in_both_path_forms():
    """The goal's own measurement checked BOTH spellings, so both are pinned."""
    for path in (WORLD_REL, MIND_DATA):
        handler = cm.merge_handler_for(path)
        assert handler is not None, f"{path} is unregistered — the wedge is back"
        assert handler is cm.merge_append_only_jsonl


def test_every_audit_report_basename_shape_is_registered():
    """Basenames here are DYNAMIC (date-stamped), which is why this is a
    path-pattern branch and not a _HANDLERS entry: a dict could only cover the
    days someone thought to enumerate."""
    for name in ("repo-hygiene-2026-09-11.jsonl",      # date-stamped
                 "repo-hygiene-2099-12-31.jsonl",      # a future day nobody enumerated
                 "worker-closure-audit.jsonl",         # static, append-only
                 "s3-churn-alarm-ledger.jsonl",
                 "source-live-asset-refs.jsonl"):
        assert cm.merge_handler_for(f"world/audit-reports/{name}") is not None, name


# --- outcome 2: a synthetic both-diverged file reconciles to the UNION ---

def test_both_diverged_audit_report_reconciles_to_the_union():
    """Two boxes sweep the same date. Rows are keyed by an absolute `repo`
    path, so the two sides' rows never collide; the shared baseline row is
    byte-identical and must collapse to ONE."""
    shared = {"repo": "/opt/ayoai-mind", "behind": 0}
    local = cm._dump_jsonl([shared, {"repo": "/opt/GitHub/Ayoai/X", "behind": 3}])
    remote = cm._dump_jsonl([shared, {"repo": "/home/zkysa/Y", "behind": 9}])

    merged = cm.merge_handler_for(WORLD_REL)(local, remote)
    repos = sorted(json.loads(l)["repo"] for l in _lines(merged))

    assert repos == ["/home/zkysa/Y", "/opt/GitHub/Ayoai/X", "/opt/ayoai-mind"]
    assert len(_lines(merged)) == 3, "the shared baseline row must collapse to one"


# --- the invariant that actually binds: BYTE commutativity (guard-4641) ---

def test_union_is_byte_commutative_not_merely_content_commutative():
    """md5(merge(a,b)) == md5(merge(b,a)). Asserting set-equality here instead
    would pass on an order-dependent handler and let the wedge re-form."""
    a = cm._dump_jsonl([{"repo": "/opt/a", "behind": 1}, {"repo": "/opt/shared"}])
    b = cm._dump_jsonl([{"repo": "/home/b", "behind": 2}, {"repo": "/opt/shared"}])
    handler = cm.merge_handler_for(WORLD_REL)

    ab = handler(a, b)
    ba = handler(b, a)
    assert hashlib.md5(ab).hexdigest() == hashlib.md5(ba).hexdigest()
    assert ab == ba, "byte-identical, not merely equal as sets"


def test_rows_without_a_timestamp_still_order_deterministically():
    """repo-hygiene rows carry NO timestamp field, so the whole ordering falls
    to the canonical-JSON tiebreak. If that ever stops being a total order the
    handler silently loses byte-commutativity for exactly this store."""
    a = cm._dump_jsonl([{"repo": "/z"}, {"repo": "/a"}])
    b = cm._dump_jsonl([{"repo": "/m"}])
    handler = cm.merge_handler_for(WORLD_REL)
    assert handler(a, b) == handler(b, a)


# --- negative controls: the branch must not widen ---

def test_non_jsonl_files_in_the_directory_are_untouched():
    """The directory also holds README.md, dated .txt disposition logs, .md
    classifications and a .json backlog."""
    for name in ("README.md",
                 "alert-sweep-seen-backlog.json",
                 "agent-inbox-dispositions-2026-09-11-run251.txt",
                 "doc-pin-classification-2026-08-17.md"):
        assert cm.merge_handler_for(f"world/audit-reports/{name}") is None, name


def test_close_reviews_subdir_stays_fence_only():
    """close-reviews/*.json is a class (b) fence-only goal-keyed store. Matching
    the IMMEDIATE parent (not a recursive prefix) is what keeps it out even if a
    .jsonl ever lands there."""
    assert cm.merge_handler_for("world/audit-reports/close-reviews/g-357-41.json") is None
    assert cm.merge_handler_for("world/audit-reports/close-reviews/g-357-41.jsonl") is None


def test_history_snapshots_are_excluded():
    """`.history/snapshots/audit-reports/` mirrors these basenames AND the
    parent dir name. A snapshot is an immutable point-in-time copy; unioning two
    boxes' snapshots corrupts the artifact the history store exists to preserve.

    PROVENANCE, and it is sharper than "a stale copy": this path is REAL and is a
    DIRECTORY whose NAME ends in .jsonl, holding timestamped snapshot .yaml files.
    Measured for g-115-9645 on cc-07 2026-09-11 by walking WORLD_DIR.rglob(
    "*.jsonl"), which matches directories as readily as files: 24 hits carry
    `audit-reports` in .parts — the 22 live files plus exactly these 2
    .jsonl-NAMED DIRS (alert-sweep-seen.jsonl, repo-hygiene-2026-09-06.jsonl).
    That is the whole of the 24-vs-22 gap the first classification run reported.
    Reproduce (g-115-9645):
        py -3 -c "import pathlib; w=pathlib.Path(WORLD_DIR); \
          h=[p for p in w.rglob('*.jsonl') if 'audit-reports' in p.parts]; \
          print(len(h), sum(1 for p in h if p.is_dir()))"
    -> 24 2

    So the hazard this guard closes is not duplication, it is a TYPE confusion:
    merge_handler_for is a pure string function, so a .jsonl-named DIRECTORY is
    indistinguishable from a store to every predicate that reasons about the path
    alone. Registering one would hand a union merge handler to a path that can
    never be read as JSONL. A `find -type f` rooted in the live directory surfaces
    neither — wrong tree, and wrong type test."""
    assert cm.merge_handler_for(
        "world/.history/snapshots/audit-reports/repo-hygiene-2026-09-06.jsonl") is None


def test_branch_does_not_reclassify_paths_outside_the_directory():
    """outcome 4: no OTHER world JSONL class changed classification.

    PROVENANCE: `audit-reports-archive/` and `reports/` are SYNTHETIC paths that
    deliberately do not exist on any box — they are inputs chosen to probe the
    matcher's edges (a prefix-match and a same-basename-different-parent), not
    claims that such directories exist. `core/config/skill-gaps.yaml` IS real and
    is the exclusion branch's own documented collision case. merge_handler_for is
    a pure string function, so a non-existent path is a legitimate input."""
    # a similarly-named sibling directory must NOT match
    assert cm.merge_handler_for("world/audit-reports-archive/repo-hygiene-2026-09-06.jsonl") is None
    assert cm.merge_handler_for("world/reports/repo-hygiene-2026-09-06.jsonl") is None
    # the core/config exclusion still wins over every basename
    assert cm.merge_handler_for("core/config/skill-gaps.yaml") is None
