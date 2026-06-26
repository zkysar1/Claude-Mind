"""test_guardrail_retire.py -  (asp-303, D1 Phase 2).

Pins the D1 guardrail cluster-retirement engine (core/scripts/guardrail_retire.py),
the guardrail instantiation of the shared cluster-refresh pattern whose reference
is tree_archive.py (D2).

  PURE (no WORLD_DIR - import the engine directly):
   - compute_cluster edge (i): empty-tags + unparseable-source guard in a LARGE
     category clusters to ITSELF only (signal A not standalone in a >floor
     category; no B; no C).
   - compute_cluster edge (ii): signal C uses the Jaccard FLOOR, not
     intersection-non-empty (the `ops-gotcha`-tag mega-cluster regression guard).
   - compute_cluster edge (iii): signal B parses the source-incident TOKEN, so two
     guards from `session-bravo-2026-04-17:...` cluster even when their full
     `source` strings differ after the prefix.
   - compute_cluster edge (iv): status != active guards are NEVER members.
   - category cohesion floor: signal A IS a standalone edge in a small category.
   - MAX_CLUSTER_SIZE fallback to B-only.
   - effective_relevance / staleness_days default to `created`; last_active_at /
     last_retrieved win when more recent.
   - _parse_source_incident_token (session prefix / g-/rb-/asp- ids / None).
   - _verdict_mutations keep|refresh|retire|revise mapping.

  guard-707 (item 6): doc_referenced EXCLUDES .history/ (the ~78x inflation).

  GATING (records= + repo_root= passed directly - no subprocess, no daemon):
   - apply retire on a doc-referenced guard refuses (HARD keep).
   - apply retire while DORMANT (retires_per_pass=0) refuses without --force.
   - apply retire on an ALLOWLISTED guard refuses even with --force.
   - apply retire --force emits a status->retired mutation; restore un-retires.

  INTEGRATION (subprocess + tmp world guardrails.jsonl, mirrors
  test_cluster_archival):
   - scan emits a stale ACTIVE candidate + its cluster + refresh-eligibility;
     skips the fresh sibling and the retired guard; dormant=True.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # core/scripts/tests
CORE_SCRIPTS = SCRIPT_DIR.parent                       # core/scripts
PROJECT_ROOT = CORE_SCRIPTS.parent.parent              # repo root
for p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import guardrail_retire as gr  # noqa: E402 (pure helpers import nothing from _paths)

ENGINE_PY = CORE_SCRIPTS / "guardrail_retire.py"
PYTHON = sys.executable
TODAY_ISO = "2026-06-25"


def _g(gid, category, source="", tags=None, status="active",
       created="2024-01-01", last_retrieved=None, last_active_at=None,
       last_relevant_at=None):
    """Build a guardrail record fixture."""
    util = {"times_active": 0}
    if last_retrieved is not None:
        util["last_retrieved"] = last_retrieved
    if last_active_at is not None:
        util["last_active_at"] = last_active_at
    rec = {"id": gid, "category": category, "source": source,
           "tags": tags or [], "status": status, "created": created,
           "utilization": util}
    if last_relevant_at is not None:
        rec["last_relevant_at"] = last_relevant_at
    return rec


# ---------------------------------------------------------------------------
# PURE - source-incident token parsing (signal B)
# ---------------------------------------------------------------------------

def test_parse_source_incident_token():
    p = gr._parse_source_incident_token
    assert p("session-bravo-2026-04-17: capability-gate.py ...") == "session-bravo-2026-04-17"
    assert p("g-115-348") == "g-115-348"
    assert p("g-115-348: some trailing text") == "g-115-348"
    assert p("rb-774 follow-up") == "rb-774"
    assert p("asp-303 design") == "asp-303"
    assert p("free text with no token") is None
    assert p("") is None
    assert p(None) is None


# ---------------------------------------------------------------------------
# PURE - compute_cluster edge cases (the Section 1 testable contract)
# ---------------------------------------------------------------------------

def test_cluster_edge_i_large_category_lonely_guard():
    """Empty tags + unparseable source in a LARGE category -> cluster = itself.

    45 guards in 'framework-architecture' (> cohesion floor 40) all with empty
    tags + free-text source => signal A is NOT a standalone edge, no B, no C."""
    records = {f"guard-{i}": _g(f"guard-{i}", "framework-architecture",
                                source="free text", tags=[])
               for i in range(45)}
    cluster = gr._compute_cluster_pure("guard-0", records,
                                       category_cohesion_floor=40,
                                       tag_jaccard_floor=0.5, max_cluster_size=30)
    assert cluster == {"guard-0"}


def test_cluster_edge_ii_jaccard_floor_not_intersection():
    """Signal C requires Jaccard >= floor, NOT a non-empty intersection.

    `b` shares only the ubiquitous `ops-gotcha` tag (Jaccard 1/7 < 0.5) -> excluded.
    `d` has full tag overlap (Jaccard 1.0) -> included. Different categories so A
    contributes nothing; sources unparseable so B contributes nothing."""
    records = {
        "a": _g("a", "cat-A", tags=["ops-gotcha", "deploy", "ci", "retry"]),
        "b": _g("b", "cat-B", tags=["ops-gotcha", "tree", "memory", "x"]),
        "d": _g("d", "cat-D", tags=["ops-gotcha", "deploy", "ci", "retry"]),
    }
    cluster = gr._compute_cluster_pure("a", records,
                                       category_cohesion_floor=40,
                                       tag_jaccard_floor=0.5, max_cluster_size=30)
    assert cluster == {"a", "d"}   # b below floor, d at Jaccard 1.0


def test_cluster_edge_iii_signal_b_incident_token():
    """Signal B parses the incident token: two guards from the same
    session-prefix cluster even when full source strings differ after it."""
    records = {
        "a": _g("a", "cat-A", source="session-bravo-2026-04-17: gate fail-open"),
        "b": _g("b", "cat-B", source="session-bravo-2026-04-17: different tail text"),
        "c": _g("c", "cat-C", source="session-zeta-2026-05-01: unrelated"),
    }
    cluster = gr._compute_cluster_pure("a", records,
                                       category_cohesion_floor=40,
                                       tag_jaccard_floor=0.5, max_cluster_size=30)
    assert cluster == {"a", "b"}   # c has a different incident token


def test_cluster_edge_iv_inactive_never_member():
    """status != active guards are excluded from membership (callers pre-filter,
    and the pure function operates only on the records dict it is handed)."""
    records = {
        "a": _g("a", "small-cat"),
        "b": _g("b", "small-cat"),
    }  # an inactive guard is simply not in the active-filtered dict
    cluster = gr._compute_cluster_pure("a", records,
                                       category_cohesion_floor=40)
    assert cluster == {"a", "b"}   # both active, small category -> signal A standalone
    # And a guard absent from the dict (e.g. filtered out as retired) is never added.
    assert "retired-guard" not in cluster


def test_cluster_cohesion_floor_small_category_signal_a_standalone():
    """In a SMALL category (< floor) signal A clusters siblings standalone."""
    records = {
        "a": _g("a", "deploy"),
        "b": _g("b", "deploy"),
        "c": _g("c", "other"),
    }
    cluster = gr._compute_cluster_pure("a", records, category_cohesion_floor=40)
    assert cluster == {"a", "b"}   # c is a different category


def test_cluster_max_size_fallback_to_b_only():
    """A cluster blowing past MAX_CLUSTER_SIZE falls back to B-only.

    50 guards share a small category (signal A standalone would cluster all 50 >
    cap 30). Fallback to B-only: only guards sharing the candidate's incident
    token remain. `a` and `b` share token g-1-1; the rest have none."""
    records = {f"n{i}": _g(f"n{i}", "deploy", source="free text") for i in range(50)}
    records["a"] = _g("a", "deploy", source="g-1-1: x")
    records["b"] = _g("b", "deploy", source="g-1-1: y")
    cluster = gr._compute_cluster_pure("a", records, category_cohesion_floor=40,
                                       max_cluster_size=30)
    assert cluster == {"a", "b"}


# ---------------------------------------------------------------------------
# PURE - relevance / staleness
# ---------------------------------------------------------------------------

def test_effective_relevance_defaults_to_created():
    today = date(2026, 6, 25)
    # No last_retrieved / last_active_at / last_relevant_at -> falls back to created.
    n1 = _g("g1", "cat", created="2025-01-01")
    assert gr.staleness_days(n1, today) == (today - date(2025, 1, 1)).days
    # last_retrieved wins when more recent than created.
    n2 = _g("g2", "cat", created="2024-01-01", last_retrieved="2026-06-20")
    assert gr.staleness_days(n2, today) == 5
    # last_active_at counts as demonstrated relevance.
    n3 = _g("g3", "cat", created="2024-01-01", last_active_at="2026-06-24")
    assert gr.staleness_days(n3, today) == 1
    # last_relevant_at (explicit) wins over created.
    n4 = _g("g4", "cat", created="2024-01-01", last_relevant_at="2026-06-01")
    assert gr.staleness_days(n4, today) == 24


def test_effective_relevance_none_when_no_signal():
    today = date(2026, 6, 25)
    n = {"id": "g", "category": "c", "status": "active", "utilization": {}}
    # No created, no dates at all -> None (default-to-keep, never eligible).
    assert gr.effective_relevance(n) is None
    assert gr.staleness_days(n, today) is None


# ---------------------------------------------------------------------------
# PURE - verdict -> mutation plan
# ---------------------------------------------------------------------------

def test_verdict_mutations_keep_refresh_retire_revise():
    muts, acts = gr._verdict_mutations("g1", "keep", ["g1"], TODAY_ISO)
    assert muts == [{"id": "g1", "field": "last_relevant_at", "value": TODAY_ISO}]
    assert acts == []

    muts, acts = gr._verdict_mutations("g1", "refresh", ["g1", "g2", "g3"], TODAY_ISO)
    assert {m["id"] for m in muts} == {"g1", "g2", "g3"}
    assert all(m["field"] == "last_relevant_at" and m["value"] == TODAY_ISO for m in muts)

    muts, acts = gr._verdict_mutations("g1", "retire", ["g1"], TODAY_ISO)
    assert muts == [{"id": "g1", "field": "status", "value": "retired"}]

    muts, acts = gr._verdict_mutations("g1", "revise", ["g1"], TODAY_ISO, reason="renamed script")
    assert muts == [{"id": "g1", "field": "last_relevant_at", "value": TODAY_ISO}]
    assert acts and acts[0]["type"] == "file_revise_goal" and acts[0]["guard_id"] == "g1"


# ---------------------------------------------------------------------------
# guard-707 doc-reference pre-gate - .history/ EXCLUSION (item 6)
# ---------------------------------------------------------------------------

def test_doc_referenced_excludes_history(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude" / "rules").mkdir(parents=True)
    (repo / "core" / "config").mkdir(parents=True)
    (repo / "world" / ".history").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("references guard-999 here", encoding="utf-8")
    # guard-888 lives ONLY in a .history snapshot -> must NOT count (the ~78x guard).
    (repo / "world" / ".history" / "snap.md").write_text("old copy citing guard-888",
                                                         encoding="utf-8")

    ref, files = gr.doc_referenced("guard-999", repo_root=repo)
    assert ref is True and any("CLAUDE.md" in f for f in files)

    ref2, files2 = gr.doc_referenced("guard-888", repo_root=repo)
    assert ref2 is False and files2 == []   # excluded because only in .history/

    ref3, _ = gr.doc_referenced("guard-777", repo_root=repo)
    assert ref3 is False                      # not present anywhere


def test_doc_referenced_word_boundary_prefix_collision(tmp_path):
    """: a low-ID guard must NOT substring-match a prefix-colliding
    higher-ID guard. Before the word-boundary fix, doc_referenced("guard-14")
    returned True against a corpus citing only guard-147 (raw `needle in text`),
    permanently shielding low-ID guards (guard-1..99) from retirement even when
    genuinely dormant. The fix uses a word-boundary regex; this test fails on the
    old substring match and passes on the new one."""
    repo = tmp_path / "repo"
    (repo / ".claude" / "rules").mkdir(parents=True)
    (repo / "core" / "config").mkdir(parents=True)
    (repo / ".claude" / "rules" / "probe.md").write_text(
        "cited: guard-147 only", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("see guard-22.", encoding="utf-8")

    # The genuinely-cited IDs still match across boundary types (space, '.').
    assert gr.doc_referenced("guard-147", repo_root=repo)[0] is True
    assert gr.doc_referenced("guard-22", repo_root=repo)[0] is True

    # The prefix-colliding low IDs must NOT match (the regression the fix closes).
    ref14, files14 = gr.doc_referenced("guard-14", repo_root=repo)
    assert ref14 is False and files14 == []   # guard-14 vs guard-147: (?![0-9]) blocks it
    assert gr.doc_referenced("guard-1", repo_root=repo)[0] is False   # vs guard-147
    assert gr.doc_referenced("guard-2", repo_root=repo)[0] is False   # vs guard-22


# ---------------------------------------------------------------------------
# GATING - apply / restore (records= + repo_root= passed; no subprocess)
# ---------------------------------------------------------------------------

_DORMANT_CFG = dict(gr._DEFAULTS)  # retires_per_pass=0 -> dormant


def _empty_repo(tmp_path):
    """A repo root with the doc-ref targets present but EMPTY (no guard cited)."""
    repo = tmp_path / "repo"
    (repo / ".claude" / "rules").mkdir(parents=True)
    (repo / "core" / "config").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("no guard ids here", encoding="utf-8")
    return repo


def test_apply_retire_dormant_refused(tmp_path):
    records = {"g1": _g("g1", "cat")}
    out = gr.apply("g1", "retire", records=records, cfg=dict(_DORMANT_CFG),
                   repo_root=_empty_repo(tmp_path), today=date(2026, 6, 25))
    assert out["ok"] is False and out["error"] == "dormant"


def test_apply_retire_force_emits_status_flip(tmp_path):
    records = {"g1": _g("g1", "cat")}
    out = gr.apply("g1", "retire", records=records, cfg=dict(_DORMANT_CFG),
                   repo_root=_empty_repo(tmp_path), force=True, today=date(2026, 6, 25))
    assert out["ok"] is True
    assert out["mutations"] == [{"id": "g1", "field": "status", "value": "retired"}]


def test_apply_retire_doc_referenced_hard_keep(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude" / "rules").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("load-bearing guard-keepme", encoding="utf-8")
    records = {"guard-keepme": _g("guard-keepme", "cat")}
    out = gr.apply("guard-keepme", "retire", records=records, cfg=dict(_DORMANT_CFG),
                   repo_root=repo, force=True, today=date(2026, 6, 25))
    # Even with --force, a doc-referenced guard is HARD-kept (guard-707).
    assert out["ok"] is False and out["error"] == "doc_referenced_hard_keep"


def test_apply_retire_allowlisted_refused(tmp_path):
    cfg = dict(_DORMANT_CFG)
    cfg["allowlist"] = ["g1"]
    records = {"g1": _g("g1", "cat")}
    out = gr.apply("g1", "retire", records=records, cfg=cfg,
                   repo_root=_empty_repo(tmp_path), force=True, today=date(2026, 6, 25))
    assert out["ok"] is False and out["error"] == "allowlisted"


def test_apply_keep_and_refresh_not_gated(tmp_path):
    # keep/refresh are non-destructive: they run even while dormant, no doc-gate.
    records = {"g1": _g("g1", "deploy"), "g2": _g("g2", "deploy")}
    keep = gr.apply("g1", "keep", records=records, cfg=dict(_DORMANT_CFG),
                    today=date(2026, 6, 25))
    assert keep["ok"] is True and keep["mutations"][0]["field"] == "last_relevant_at"
    refresh = gr.apply("g1", "refresh", records=records, cfg=dict(_DORMANT_CFG),
                       today=date(2026, 6, 25))
    # small category -> cluster {g1,g2} both refreshed
    assert refresh["ok"] is True and {m["id"] for m in refresh["mutations"]} == {"g1", "g2"}


def test_restore_un_retires():
    records = {"g1": _g("g1", "cat", status="retired")}
    out = gr.restore("g1", records=records)
    assert out["ok"] is True
    assert out["mutations"] == [{"id": "g1", "field": "status", "value": "active"}]
    # restore on an active guard refuses.
    records2 = {"g1": _g("g1", "cat", status="active")}
    out2 = gr.restore("g1", records=records2)
    assert out2["ok"] is False and out2["error"] == "not_retired"


# ---------------------------------------------------------------------------
# INTEGRATION - subprocess + tmp world guardrails.jsonl
# ---------------------------------------------------------------------------

def _seed_guardrails(world: Path):
    world.mkdir(parents=True, exist_ok=True)
    recs = [
        # stale active candidate (created 2024, no recent retrieval).
        _g("guard-stale-x", "test-cat", created="2024-01-01"),
        # fresh sibling (same small category -> clusters via signal A; retrieved
        # within lookback -> makes the candidate refresh-eligible).
        _g("guard-fresh-x", "test-cat", created="2024-01-01",
           last_retrieved="2026-06-20"),
        # retired guard: never a candidate, never a member.
        _g("guard-retired-x", "test-cat", created="2024-01-01", status="retired"),
    ]
    path = world / "guardrails.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return path


def _run(args, world: Path, meta: Path):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env.pop("MIND_AGENT", None)
    return subprocess.run([PYTHON, str(ENGINE_PY)] + args,
                          text=True, capture_output=True, env=env, timeout=60)


def test_scan_emits_stale_active_candidate(tmp_path):
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_guardrails(world)
    r = _run(["scan", "--today", "2026-06-25"], world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    out = json.loads(r.stdout)
    ids = {c["id"] for c in out["candidates"]}
    assert "guard-stale-x" in ids
    assert "guard-fresh-x" not in ids       # not stale (retrieved within window)
    assert "guard-retired-x" not in ids     # not active
    stale = next(c for c in out["candidates"] if c["id"] == "guard-stale-x")
    assert "guard-fresh-x" in stale["cluster"]   # signal A (shared small category)
    assert stale["refresh_eligible"] is True     # fresh sibling retrieved within 60d
    assert stale["doc_referenced"] is False      # tmp id not in real framework files
    assert out["dormant"] is True                # real config retires_per_pass=0
