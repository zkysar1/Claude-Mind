"""knowledge_debt UPSERT-BY-node_key —  (2026-08-06).

The reflect tree-lint (reflect/SKILL.md, "stale-high-retrieval") re-flags the
TOP 5 tree nodes by retrieval_count on every maintain pass. That set is stable
by construction — a high-retrieval node stays high-retrieval — so a blind
`arr.append` re-records the same nodes each scan. Measured live before the fix:
10 entries for 5 distinct node_keys across 3 scans, against an array_limit of
15. Once saturated, the FIFO eviction (sort by _item_ts, pop(0)) drops the
OLDEST entries, which under that duplication are re-records of nodes still
present — so the slot can never hold more than ~5 distinct nodes and genuine
debt from the OTHER 7 writers is evicted within ~3 scans. Silent data loss, in
the one slot whose owning goal exists to prevent it.

TWO tests, and the second is the one that matters most:

  test_upsert_replaces_in_place  — behavior, on the CLI implementation.
  test_daemon_twin_has_upsert    — PARITY. `wm-append.sh` is daemon-routed, so
                                   a fix applied only to core/scripts/wm.py
                                   changes NOTHING at runtime (guard-742). That
                                   is not hypothetical: it is exactly what
                                   happened while fixing this. The CLI edit was
                                   correct, compiled, and read correctly on
                                   review; the live slot still grew 10 -> 11
                                   with a third duplicate, because the daemon
                                   serves this endpoint from its own copy. A
                                   behavior test against the CLI alone would
                                   have gone green over a completely inert fix.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "core" / "scripts" / "wm.py"
DAEMON = REPO / "mind_api" / "src" / "endpoints" / "wm_write.py"


def _entry(node_key, retrieval_count):
    return {
        "node_key": node_key,
        "reason": "stale-high-retrieval",
        "retrieval_count": retrieval_count,
        "days_since_update": 40,
        "total_stale_at_scan": 860,
        "priority": "MEDIUM",
    }


def _upsert(arr, item):
    """Mirror of the upsert block shared by both twins.

    Kept as a local mirror rather than imported: wm.py's cmd_append is welded
    to stdin + a file lock + a real working-memory tree, and standing all that
    up would test the plumbing rather than the rule. The parity test below is
    what keeps this mirror honest about the real implementations.
    """
    upserted = False
    node_key = item.get("node_key")
    if node_key:
        matches = [i for i, e in enumerate(arr)
                   if isinstance(e, dict) and e.get("node_key") == node_key]
        if matches:
            oldest = min((arr[i].get("_item_ts") for i in matches
                          if arr[i].get("_item_ts")),
                         default=item.get("_item_ts"))
            item["_item_ts"] = oldest
            arr[matches[0]] = item
            for i in reversed(matches[1:]):
                arr.pop(i)
            upserted = True
    if not upserted:
        arr.append(item)
    return arr


def test_converges_a_slot_that_already_holds_duplicates():
    """The upsert must HEAL pre-existing duplicates, not just prevent new ones.

    Caught by fresh-eyes review of the original fix, which replaced the FIRST
    match and broke. That version is correct on a clean slot and inert on a
    dirty one: it refreshes one twin and leaves the other pinning an eviction
    slot forever, so any slot already duplicated when the fix ships stays that
    way permanently. The author's own slot converged only because it had been
    cleaned BY HAND — which is exactly what hid the gap.
    """
    legacy = [
        {"node_key": "a", "retrieval_count": 210, "_item_ts": "2026-08-01"},
        {"node_key": "a", "retrieval_count": 220, "_item_ts": "2026-08-02"},
        {"node_key": "b", "retrieval_count": 100, "_item_ts": "2026-08-01"},
    ]
    _upsert(legacy, {"node_key": "a", "retrieval_count": 260, "_item_ts": "2026-08-03"})

    a_entries = [e for e in legacy if e["node_key"] == "a"]
    assert len(a_entries) == 1, "pre-existing duplicates must collapse on write"
    assert a_entries[0]["retrieval_count"] == 260, "freshest measurement wins"
    assert a_entries[0]["_item_ts"] == "2026-08-01", (
        "the OLDEST twin's _item_ts must survive — a serially re-flagged node "
        "must not renew its own eviction priority over never-serviced debt"
    )
    assert len(legacy) == 2, "unrelated entries must be untouched"


def test_upsert_replaces_in_place():
    """Re-flagging a node refreshes it; the slot does not grow."""
    arr = []
    for i, key in enumerate(["a", "b", "c", "d", "e"]):
        item = _entry(key, 100 + i)
        item["_item_ts"] = f"2026-08-01T00:00:0{i}"
        _upsert(arr, item)
    assert len(arr) == 5

    # Second scan re-flags the identical top-5 — the real-world case.
    for i, key in enumerate(["a", "b", "c", "d", "e"]):
        item = _entry(key, 200 + i)
        item["_item_ts"] = "2026-08-02T00:00:00"
        _upsert(arr, item)

    assert len(arr) == 5, "re-flagging the same nodes must not grow the slot"
    assert len({e["node_key"] for e in arr}) == 5
    # Fresher measurement wins.
    assert [e["retrieval_count"] for e in arr] == [200, 201, 202, 203, 204]
    # ...but _item_ts is preserved, so a serially-re-flagged node cannot renew
    # its own eviction priority over older, never-serviced debt.
    assert all(e["_item_ts"].startswith("2026-08-01") for e in arr)


def test_distinct_nodes_still_append():
    """The upsert must not suppress genuinely new debt."""
    arr = [_entry("a", 100)]
    arr[0]["_item_ts"] = "2026-08-01T00:00:00"
    _upsert(arr, dict(_entry("b", 50), _item_ts="2026-08-02T00:00:00"))
    assert len(arr) == 2
    assert {e["node_key"] for e in arr} == {"a", "b"}


def test_non_knowledge_debt_slots_unaffected():
    """Entries without node_key append unconditionally (other slots, other writers)."""
    arr = [{"foo": 1, "_item_ts": "2026-08-01T00:00:00"}]
    _upsert(arr, {"foo": 1, "_item_ts": "2026-08-02T00:00:00"})
    assert len(arr) == 2, "no node_key -> plain append, never a silent dedup"


@pytest.mark.parametrize("path", [CLI, DAEMON], ids=["cli", "daemon"])
def test_twin_has_upsert(path):
    """BOTH implementations must carry the upsert.

    wm-append.sh is daemon-routed (.claude/rules/no-python-cli-fallback.md), so
    the daemon copy is the LIVE one and the CLI copy is what a reader reaches
    for first. A fix in only one is inert in exactly the direction that is
    hardest to notice — the code reads correctly and the runtime ignores it.
    """
    src = path.read_text(encoding="utf-8")
    assert 'root_slot_for_validation == "knowledge_debt"' in src, (
        f"{path.name}: no knowledge_debt branch in the append path"
    )
    assert re.search(r"_upserted\s*=\s*False", src), (
        f"{path.name}: missing the knowledge_debt upsert guard — a blind "
        f"arr.append here re-records the stable top-5 every scan and FIFO-evicts "
        f"genuine debt from the other writers (g-001-07)"
    )
    assert re.search(r"if not _upserted:\s*\n\s*arr\.append\(item\)", src), (
        f"{path.name}: append is not gated on the upsert result"
    )

    # ── The healing block: the only assertions here that the buggy version fails.
    #
    # Measured at b4da74a5 (): the historical single-match version
    # satisfies ALL THREE assertions above — it has the knowledge_debt branch,
    # the `_upserted = False` guard, AND the gated append — while breaking on
    # the first node_key match. So this test was green on the exact regression
    # its own docstring names, and the four behaviour tests earlier in this file
    # could not see it either, because they exercise the LOCAL MIRROR `_upsert`
    # rather than these files. Nothing in this module reads the real
    # implementations for behaviour at all; these tokens are the whole join.
    #
    # Token-pinned on purpose. A rename that breaks these SHOULD fail loudly —
    # the mirror is only honest for as long as something asserts the shape it
    # mirrors, and a silent un-pin returns this file to green-on-the-defect.
    assert re.search(r"_matches\s*=\s*\[", src), (
        f"{path.name}: the upsert stops at the FIRST node_key match. It must "
        f"collect ALL matches (_matches), or duplicates already present in the "
        f"slot are never collapsed and the entry is re-recorded every scan"
    )
    assert re.search(r"_oldest\s*=\s*min\(", src), (
        f"{path.name}: the upsert does not carry the OLDEST _item_ts forward "
        f"(_oldest = min(...)). Without it a surviving entry is stamped with "
        f"the newest write and the debt's real age is silently reset, which "
        f"defeats every age-ordered consumer of the slot"
    )


def test_both_twins_compile():
    for path in (CLI, DAEMON):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{path.name} does not compile: {r.stderr}"
