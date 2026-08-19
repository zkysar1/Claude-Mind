"""GET /v1/pipeline/read?unreflected=1 — live+archive union ().

WHY THESE ARE BUILT ON THE FAILING SIDE (rb-6208)
    The pre-fix branch read the live file only AND filtered stage=="resolved",
    so a pin asserting "a live resolved unreflected record appears" would have
    passed against the DEFECT — vacuous by construction. Every assertion below
    is instead about a record the old code could not return: archived stage,
    archive-file-only, or both.

WHY THE DEFECT MATTERED
    Archiving is AGE-driven (ARCHIVE_AGE_DAYS=3), not completion-driven. So a
    hypothesis resolved and left unreflected for three days fell permanently out
    of the very backlog that would have caused it to be reflected — the filter
    selected FOR the least-reflected records and hid exactly those. Measured
    2026-08-08: 8 returned of 63 genuinely unreflected, a 7.9x under-report
    inherited by every consumer (learning gate, consolidation-precheck,
    quiescence-gate, /reflect Phase A).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pytest


def _get(port: int, path: str, query: dict = None, *, agent: str = "alpha"):
    qs = urllib.parse.urlencode(query) if query else ""
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method="GET")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _rec(rid, stage, reflected=False, **extra):
    r = {"id": rid, "stage": stage, "hypothesis": f"h for {rid}",
         "reflected": reflected}
    r.update(extra)
    return r


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def seeded(running_daemon):
    project_root, port = running_daemon
    live = project_root / "world" / "pipeline.jsonl"
    archive = project_root / "world" / "pipeline-archive.jsonl"
    return project_root, port, live, archive


def _unreflected_ids(port):
    status, body = _get(port, "/v1/pipeline/read", {"unreflected": "1"})
    assert status == 200, body
    return {r["id"] for r in json.loads(body)}


def test_archived_in_live_unreflected_record_appears(seeded):
    """THE PRIMARY MUTATION TARGET — narrowing filter (2).

    A record swept to stage=archived is retained in the LIVE file as a full
    tombstone. The old `stage == "resolved"` predicate dropped it even though
    the file was being read, so this fails on the pre-fix code without touching
    the archive file at all.
    """
    _, port, live, archive = seeded
    _write(live, [_rec("live-resolved", "resolved"),
                  _rec("live-archived", "archived")])
    _write(archive, [])
    ids = _unreflected_ids(port)
    assert "live-archived" in ids, "an archived-in-live tombstone is still unreflected work"
    assert "live-resolved" in ids


def test_archive_file_only_record_appears(seeded):
    """THE SECOND MUTATION TARGET — narrowing filter (1).

    A record whose live tombstone has been pruned exists only in the archive
    file. The old branch never opened that file, so this fails on pre-fix code
    even if the stage predicate were widened.
    """
    _, port, live, archive = seeded
    _write(live, [])
    _write(archive, [_rec("archive-only", "archived")])
    assert "archive-only" in _unreflected_ids(port)


def test_reflected_records_are_still_excluded(seeded):
    """The widening must not swallow the predicate it was widening around."""
    _, port, live, archive = seeded
    _write(live, [_rec("live-done", "resolved", reflected=True)])
    _write(archive, [_rec("arch-done", "archived", reflected=True),
                     _rec("arch-todo", "archived")])
    ids = _unreflected_ids(port)
    assert ids == {"arch-todo"}


def test_unresolved_stages_never_enter_the_backlog(seeded):
    """The stage filter is KEPT, not dropped. Dropping it entirely would pull
    discovered / active / measurement-pending work into a REFLECTION backlog,
    which is a different defect in the opposite direction."""
    _, port, live, archive = seeded
    _write(live, [_rec("d", "discovered"), _rec("a", "active"),
                  _rec("mp", "measurement-pending"), _rec("r", "resolved")])
    _write(archive, [])
    assert _unreflected_ids(port) == {"r"}


def test_record_in_both_files_is_returned_once_and_live_wins(seeded):
    """Dedup is live-wins (): update_field probes live-first, so a
    post-archival `reflected` stamp lands on the LIVE copy while the archive
    copy stays frozen at first-archival. Archive-wins would resurrect a record
    that HAS been reflected — the union's own failure mode."""
    _, port, live, archive = seeded
    _write(live, [_rec("dup", "archived", reflected=True)])
    _write(archive, [_rec("dup", "archived", reflected=False)])
    status, body = _get(port, "/v1/pipeline/read", {"unreflected": "1"})
    rows = json.loads(body)
    assert [r["id"] for r in rows].count("dup") == 0, (
        "the live copy says reflected=True and must win")

    _write(live, [_rec("dup2", "archived", reflected=False)])
    _write(archive, [_rec("dup2", "archived", reflected=True)])
    status, body = _get(port, "/v1/pipeline/read", {"unreflected": "1"})
    rows = json.loads(body)
    assert [r["id"] for r in rows].count("dup2") == 1, "returned once, not twice"


def test_count_matches_a_direct_store_count(seeded):
    """The goal's outcome 3: what the endpoint reports must equal what the store
    holds, computed independently of the endpoint's own logic."""
    _, port, live, archive = seeded
    live_recs = [_rec(f"L{i}", "resolved") for i in range(3)] + \
                [_rec(f"LA{i}", "archived") for i in range(2)] + \
                [_rec("Ldone", "resolved", reflected=True)]
    arch_recs = [_rec(f"A{i}", "archived") for i in range(4)] + \
                [_rec("Adone", "archived", reflected=True)]
    _write(live, live_recs)
    _write(archive, arch_recs)

    by_id = {}
    for r in arch_recs + live_recs:
        by_id[r["id"]] = r
    expected = {r["id"] for r in by_id.values()
                if r["stage"] in ("resolved", "archived") and not r["reflected"]}
    assert _unreflected_ids(port) == expected
    assert len(expected) == 9
