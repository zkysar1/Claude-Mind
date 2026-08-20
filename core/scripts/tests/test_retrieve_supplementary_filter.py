"""test_retrieve_supplementary_filter.py — regression tests for retrieve.py
supplementary-store filter (P0 #1, knowledge-system audit, 2026-05-09).

Pre-fix, load_reasoning_bank / load_guardrails / load_pattern_signatures
returned ALL active records regardless of category. Audit showed ~75% had
utilization_score=0. The fix filters by `_entry_matches_category`, sorts by
utility, and caps at SUPPLEMENTARY_CAPS[depth]. These tests verify the
contract.

Pure stdlib + PyYAML. Self-contained: never touches the live world directory.
Bootstraps retrieve.py via importlib (hyphenated module name not directly
importable; same pattern as test_retrieve_write_locking.py).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# retrieve.py imports `from _paths import ...` at module load — _paths reads
# MIND_WORLD env. Set it to a temp dir BEFORE the import so retrieve.py
# binds to our scratch paths, not the live world.
#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="retrieve-filter-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

import importlib.util  # noqa: E402

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _make_rb(rb_id, category, score=0.0, applies_to=None, created="2026-05-01",
             entry_type=None):
    """Build a reasoning-bank record with the standard utilization shape.

    entry_type (g-306-11): when given, tags the record so the load_reasoning_bank
    entry_type filter can target it; omitted => an ordinary (untagged) lesson."""
    rec = {
        "id": rb_id,
        "title": f"test entry {rb_id}",
        "content": "...",
        "category": category,
        "status": "active",
        "created": created,
        "utilization": {
            "retrieval_count": 10,
            "last_retrieved": "2026-05-09",
            "times_helpful": 0,
            "times_noise": 0,
            "utilization_score": score,
        },
    }
    if applies_to is not None:
        rec["applies_to"] = applies_to
    if entry_type is not None:
        rec["entry_type"] = entry_type
    return rec


def _make_guard(gid, category, score=0.0, created="2026-05-01"):
    return {
        "id": gid,
        "rule": f"test rule {gid}",
        "trigger_condition": "always",
        "category": category,
        "status": "active",
        "created": created,
        "utilization": {
            "retrieval_count": 10,
            "last_retrieved": "2026-05-09",
            "times_helpful": 0,
            "utilization_score": score,
        },
    }


def _seed_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

def test_entry_matches_category_bidirectional():
    """Bidirectional substring: query is a substring of entry OR vice versa."""
    rec = {"category": "npc-intelligence-evaluation"}
    assert _retrieve._entry_matches_category(rec, ["npc-intelligence"])  # query ⊂ entry
    assert _retrieve._entry_matches_category(rec, ["npc-intelligence-evaluation-deep"])  # entry ⊂ query
    assert not _retrieve._entry_matches_category(rec, ["framework-architecture"])


def test_entry_matches_category_fail_open():
    """Untagged entry and empty category list both pass through."""
    untagged = {"category": ""}
    tagged = {"category": "framework-architecture"}
    assert _retrieve._entry_matches_category(untagged, ["foo"])  # untagged passes
    assert _retrieve._entry_matches_category(tagged, [])  # empty list passes
    assert _retrieve._entry_matches_category(untagged, [])  # both empty passes


def test_sort_by_utility_score_desc_then_created_desc():
    """Higher utility_score first; tie-break by created date desc."""
    a = _make_rb("rb-a", "x", score=0.10, created="2026-04-01")
    b = _make_rb("rb-b", "x", score=0.20, created="2026-04-01")
    c = _make_rb("rb-c", "x", score=0.10, created="2026-05-01")
    out = _retrieve._sort_by_utility([a, b, c])
    assert [r["id"] for r in out] == ["rb-b", "rb-c", "rb-a"]


def test_sort_by_utility_handles_missing_utilization():
    """Records without utilization sub-object sort to the bottom."""
    bare = {"id": "rb-bare", "category": "x", "status": "active"}  # no utilization
    scored = _make_rb("rb-scored", "x", score=0.05)
    out = _retrieve._sort_by_utility([bare, scored])
    assert out[0]["id"] == "rb-scored"
    assert out[1]["id"] == "rb-bare"


# ---------------------------------------------------------------------------
# load_reasoning_bank
# ---------------------------------------------------------------------------

def test_load_rb_filters_by_category():
    """Domain entries not matching the query are dropped from the result."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    _seed_jsonl(p, [
        _make_rb("rb-001", "npc-intelligence", score=0.10),
        _make_rb("rb-002", "framework-architecture", score=0.10),  # universal — always
        _make_rb("rb-003", "ayoai-development-patterns", score=0.10),
        _make_rb("rb-004", "npc-intelligence-evaluation", score=0.10),
    ])
    _retrieve.RB_PATH = p

    domain, universal = _retrieve.load_reasoning_bank(["npc-intelligence"], read_only=True)
    domain_ids = {r["id"] for r in domain}
    assert domain_ids == {"rb-001", "rb-004"}, domain_ids
    universal_ids = {r["id"] for r in universal}
    assert "rb-002" in universal_ids  # framework-* ⇒ universal


def test_load_rb_universal_via_applies_to():
    """applies_to=any escalates a non-framework entry into universal pool."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    _seed_jsonl(p, [
        _make_rb("rb-001", "npc-intelligence", score=0.10),
        _make_rb("rb-005", "ayoai-development-patterns", score=0.10, applies_to="any"),
    ])
    _retrieve.RB_PATH = p

    domain, universal = _retrieve.load_reasoning_bank(["framework-coordination"], read_only=True)
    universal_ids = {r["id"] for r in universal}
    assert "rb-005" in universal_ids
    # rb-001 doesn't match framework-coordination and isn't universal — dropped
    assert all(r["id"] != "rb-001" for r in domain)


def test_load_rb_sorts_domain_by_utility():
    """Returned domain list is utility-sorted (high score first)."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    _seed_jsonl(p, [
        _make_rb("rb-low", "x", score=0.05),
        _make_rb("rb-high", "x", score=0.50),
        _make_rb("rb-mid", "x", score=0.20),
    ])
    _retrieve.RB_PATH = p

    domain, _ = _retrieve.load_reasoning_bank(["x"], read_only=True)
    assert [r["id"] for r in domain] == ["rb-high", "rb-mid", "rb-low"]


def test_load_rb_caps_at_depth_limit():
    """SUPPLEMENTARY_CAPS[depth] bounds the returned domain list."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    # Seed 60 matching records (more than shallow=20 cap)
    records = [_make_rb(f"rb-{i:03d}", "x", score=i * 0.001) for i in range(60)]
    _seed_jsonl(p, records)
    _retrieve.RB_PATH = p

    shallow_domain, _ = _retrieve.load_reasoning_bank(["x"], depth="shallow", read_only=True)
    medium_domain, _ = _retrieve.load_reasoning_bank(["x"], depth="medium", read_only=True)
    deep_domain, _ = _retrieve.load_reasoning_bank(["x"], depth="deep", read_only=True)

    assert len(shallow_domain) == _retrieve.SUPPLEMENTARY_CAPS["shallow"]
    assert len(medium_domain) == _retrieve.SUPPLEMENTARY_CAPS["medium"]
    # 60 records < deep cap (80), so all are returned
    assert len(deep_domain) == 60


def test_load_rb_default_depth_is_medium():
    """Calling without depth uses the medium cap."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    records = [_make_rb(f"rb-{i:03d}", "x", score=i * 0.001) for i in range(50)]
    _seed_jsonl(p, records)
    _retrieve.RB_PATH = p

    domain, _ = _retrieve.load_reasoning_bank(["x"], read_only=True)
    assert len(domain) == _retrieve.SUPPLEMENTARY_CAPS["medium"]


def test_load_rb_universal_cap_independent_of_supplementary():
    """The universal pool is capped at UNIVERSAL_RB_CAP (5) regardless of depth."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    records = [_make_rb(f"rb-fw-{i:03d}", f"framework-x", score=i * 0.01) for i in range(20)]
    _seed_jsonl(p, records)
    _retrieve.RB_PATH = p

    _, universal = _retrieve.load_reasoning_bank(["unrelated"], depth="deep", read_only=True)
    assert len(universal) == _retrieve.UNIVERSAL_RB_CAP


# ---------------------------------------------------------------------------
# load_reasoning_bank — entry_type filter ()
# ---------------------------------------------------------------------------

def test_load_rb_entry_type_filters_to_procedure():
    """entry_type='procedure' returns ONLY procedure-tagged entries; untagged
    (ordinary) entries are excluded from BOTH the domain and universal partitions."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    _seed_jsonl(p, [
        _make_rb("rb-proc1", "x", score=0.10, entry_type="procedure"),
        _make_rb("rb-ord1", "x", score=0.20),                       # ordinary (no entry_type)
        _make_rb("rb-proc2", "x", score=0.05, entry_type="procedure"),
        _make_rb("rb-fwproc", "framework-x", score=0.10, entry_type="procedure"),  # universal procedure
        _make_rb("rb-fword", "framework-x", score=0.10),            # universal ordinary
    ])
    _retrieve.RB_PATH = p

    domain, universal = _retrieve.load_reasoning_bank(
        ["x"], read_only=True, entry_type="procedure")
    assert {r["id"] for r in domain} == {"rb-proc1", "rb-proc2"}, {r["id"] for r in domain}
    # the universal partition is ALSO filtered to procedures
    assert {r["id"] for r in universal} == {"rb-fwproc"}, {r["id"] for r in universal}


def test_load_rb_entry_type_none_returns_all():
    """Default (entry_type=None) is byte-identical to prior behavior: every entry
    type returned, procedure and ordinary alike — no existing caller changes."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    _seed_jsonl(p, [
        _make_rb("rb-proc", "x", score=0.10, entry_type="procedure"),
        _make_rb("rb-ord", "x", score=0.20),
    ])
    _retrieve.RB_PATH = p

    domain, _ = _retrieve.load_reasoning_bank(["x"], read_only=True)  # no entry_type arg
    assert {r["id"] for r in domain} == {"rb-proc", "rb-ord"}


def test_load_rb_entry_type_no_matches_returns_empty():
    """Filtering for procedure when none exist returns empty (NOT all) — the
    filter must not silently fall open to the full set."""
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    _seed_jsonl(p, [
        _make_rb("rb-ord1", "x", score=0.10),
        _make_rb("rb-ord2", "x", score=0.20),
    ])
    _retrieve.RB_PATH = p

    domain, universal = _retrieve.load_reasoning_bank(
        ["x"], read_only=True, entry_type="procedure")
    assert domain == []
    assert universal == []


def test_bump_set_with_entry_type_filter_excludes_nonprocedure(monkeypatch):
    """The bump-set==return-set invariant holds UNDER the entry_type filter:
    counter-bump fires ONLY on returned (procedure) entries; ordinary entries —
    even category-matching ones — keep their pre-call retrieval_count (the filter
    runs BEFORE the bump, so non-procedure counters are never polluted)."""
    #  lane pin: this test verifies the invariant through the LEGACY
    # evidence channel (embedded store counters). With
    # UTILIZATION_COUNTERS_SPOOLED on, the bump spool-routes and the embedded
    # copy deliberately stays untouched — the spool-lane version of the same
    # invariant is pinned in test_utilization_spool.py
    # (test_retrieve_bump_only_matched_records_spool). The legacy lane stays
    # live for flag-off boxes and the failed-spool fallback.
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    procs = [_make_rb(f"rb-p-{i:02d}", "x", score=i * 0.001, entry_type="procedure")
             for i in range(5)]
    ords = [_make_rb(f"rb-o-{i:02d}", "x", score=i * 0.001) for i in range(5)]
    _seed_jsonl(p, procs + ords)
    _retrieve.RB_PATH = p

    pre_rc = {}
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            pre_rc[rec["id"]] = (rec.get("utilization") or {}).get("retrieval_count", 0)

    domain, universal = _retrieve.load_reasoning_bank(
        ["x"], depth="deep", read_only=False, entry_type="procedure")
    returned_ids = {r["id"] for r in domain} | {r["id"] for r in universal}
    assert returned_ids == {f"rb-p-{i:02d}" for i in range(5)}, returned_ids

    post_rc = {}
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            post_rc[rec["id"]] = (rec.get("utilization") or {}).get("retrieval_count", 0)

    bumped = {rid for rid in pre_rc if post_rc[rid] == pre_rc[rid] + 1}
    assert bumped == returned_ids, f"bump diverged from return: {bumped ^ returned_ids}"
    for i in range(5):
        oid = f"rb-o-{i:02d}"
        assert post_rc[oid] == pre_rc[oid], f"{oid} bumped but should be filtered out"


# ---------------------------------------------------------------------------
# load_guardrails
# ---------------------------------------------------------------------------

def test_load_guardrails_filters_by_category():
    p = Path(_TMPDIR) / "guardrails.jsonl"
    _seed_jsonl(p, [
        _make_guard("guard-001", "npc-intelligence"),
        _make_guard("guard-002", "framework-architecture"),
        _make_guard("guard-003", "ayoai-development-patterns"),
    ])
    _retrieve.GUARD_PATH = p

    out = _retrieve.load_guardrails(["framework-architecture"], read_only=True)
    ids = {r["id"] for r in out}
    assert ids == {"guard-002"}


def test_load_guardrails_sorts_by_utility():
    p = Path(_TMPDIR) / "guardrails.jsonl"
    _seed_jsonl(p, [
        _make_guard("guard-a", "x", score=0.05),
        _make_guard("guard-b", "x", score=0.30),
        _make_guard("guard-c", "x", score=0.15),
    ])
    _retrieve.GUARD_PATH = p

    out = _retrieve.load_guardrails(["x"], read_only=True)
    assert [r["id"] for r in out] == ["guard-b", "guard-c", "guard-a"]


def test_load_guardrails_caps_at_depth_limit():
    p = Path(_TMPDIR) / "guardrails.jsonl"
    records = [_make_guard(f"guard-{i:03d}", "x", score=i * 0.001) for i in range(30)]
    _seed_jsonl(p, records)
    _retrieve.GUARD_PATH = p

    shallow = _retrieve.load_guardrails(["x"], depth="shallow", read_only=True)
    medium = _retrieve.load_guardrails(["x"], depth="medium", read_only=True)
    assert len(shallow) == _retrieve.SUPPLEMENTARY_CAPS["shallow"]
    assert len(medium) == 30  # 30 < medium cap (40), all returned


# ---------------------------------------------------------------------------
# load_pattern_signatures
# ---------------------------------------------------------------------------

def test_load_pattern_signatures_filters_and_sorts():
    p = Path(_TMPDIR) / "pattern-signatures.jsonl"
    _seed_jsonl(p, [
        _make_guard("sig-001", "x", score=0.10),  # same shape works
        _make_guard("sig-002", "y", score=0.10),
        _make_guard("sig-003", "x", score=0.30),
    ])
    _retrieve.SIGS_PATH = p

    out = _retrieve.load_pattern_signatures(["x"], read_only=True)
    assert [r["id"] for r in out] == ["sig-003", "sig-001"]


# ---------------------------------------------------------------------------
# Counter-bump invariance — most important regression check
# ---------------------------------------------------------------------------

def test_bump_set_equals_return_set(monkeypatch):
    """Bumps fire ONLY on records actually returned (post-filter, post-sort,
    post-cap). This is the utility_ratio alignment invariant: helpful++ in
    utilization-feedback.py increment_supplementary targets
    `session.supplementary_items` (= the return set), so retrieval_count++ in
    retrieve.py must target the same set. If they diverge, helpful/rc
    underestimates true helpfulness for cap-rejected records, which then
    sink in ranking and never recover.

    Test: seed 60 matching records (varying scores) + 1 non-matching record,
    call load_reasoning_bank(depth="shallow", cap=20). Verify post-call
    on-disk state: rc bumped on exactly the 20 returned IDs. The 40
    matching-but-cap-rejected records keep their pre-call rc.
    """
    #  lane pin — see test_bump_set_with_entry_type_filter above.
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    p = Path(_TMPDIR) / "reasoning-bank.jsonl"
    # Score from 0.0 to 0.059 — top-20 by score will be rb-m-040 .. rb-m-059
    matching = [_make_rb(f"rb-m-{i:03d}", "x", score=i * 0.001) for i in range(60)]
    not_matching = [_make_rb("rb-other", "y")]
    _seed_jsonl(p, matching + not_matching)
    _retrieve.RB_PATH = p

    # Pre-call snapshot of every record's rc
    pre_rc = {}
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            pre_rc[rec["id"]] = (rec.get("utilization") or {}).get("retrieval_count", 0)

    cap = _retrieve.SUPPLEMENTARY_CAPS["shallow"]
    domain, universal = _retrieve.load_reasoning_bank(
        ["x"], depth="shallow", read_only=False
    )
    returned_ids = {r["id"] for r in domain} | {r["id"] for r in universal}
    assert len(domain) == cap, f"expected {cap} returned, got {len(domain)}"

    # Post-call on-disk rc
    post_rc = {}
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            post_rc[rec["id"]] = (rec.get("utilization") or {}).get("retrieval_count", 0)

    bumped_ids = {rid for rid in pre_rc if post_rc[rid] == pre_rc[rid] + 1}
    unchanged_ids = {rid for rid in pre_rc if post_rc[rid] == pre_rc[rid]}

    # The cornerstone: bump set IS the return set, exactly
    assert bumped_ids == returned_ids, (
        f"bump/return diverged. bumped-but-not-returned="
        f"{sorted(bumped_ids - returned_ids)}, "
        f"returned-but-not-bumped={sorted(returned_ids - bumped_ids)}"
    )
    # The 40 matching-but-cap-rejected records are NOT bumped
    rejected = {f"rb-m-{i:03d}" for i in range(60)} - returned_ids
    assert rejected.issubset(unchanged_ids), \
        "matching-but-cap-rejected records must NOT be bumped"
    # The non-matching record is also NOT bumped
    assert "rb-other" in unchanged_ids


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e)))
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
