"""Tests for rb_undiagnosed_cluster ().

Each test pins a decision that was MEASURED against the live corpus during
implementation, not a decision that seemed reasonable. The three that matter
most — the dropped `not established` pattern, the ALL-not-ANY expansion, and
the fail-safe direction of `already_filed` — each encode a defect that was
actually observed and fixed, so a regression in any of them is silent.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rb_undiagnosed_cluster as rc  # noqa: E402


def _rec(rid, title, content="", category="framework"):
    return rc.Record(rid, title, content, category)


def _write_store(tmp_path, records):
    p = tmp_path / "reasoning-bank.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# admission detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "the root cause was not diagnosed",
    "Cause not root-caused at the time",
    "mechanism not established (observed behavior only)",
    "the cause is unknown",
    "root cause not found",
    "inferred, not verified",
    "a test-filter quirk on this project",
    "unclear why the retry succeeds",
])
def test_admission_phrases_fire(text):
    assert rc.admission_hits(text), text


def test_bare_not_established_does_NOT_fire():
    """The single most load-bearing calibration in the module.

    A bare `not established` matched 29 of 79 seeds on the live corpus — the
    largest single contributor — and inspection showed it firing on ordinary
    methodology prose rather than on mechanism admissions. Removing it dropped
    the seed set to 53. If someone re-adds the broad pattern "for coverage",
    the detector silently becomes a noise generator, and nothing else in this
    file would fail. Hence an explicit negative test rather than trusting the
    parametrized positives above to imply it.
    """
    assert rc.admission_hits(
        "the invariant is not established by a source pin") == []
    # ...while the PRECISE phrase, which carries the real signal, still fires.
    assert rc.admission_hits("mechanism not established here")


def test_admission_hits_deduplicates_and_is_empty_for_clean_text():
    assert rc.admission_hits("not diagnosed and still not diagnosed") == ["not diagnosed"]
    assert rc.admission_hits("a fully explained and verified mechanism") == []
    assert rc.admission_hits("") == []


# --------------------------------------------------------------------------
# clustering
# --------------------------------------------------------------------------

def _sim_from_map(pairs):
    """Deterministic similarity oracle, so clustering is tested independently
    of any embedding index being present on the box running the suite."""
    def sim(a, b):
        return pairs.get(frozenset((a.id, b.id)), 0.0)
    return sim


def test_expansion_requires_similarity_to_ALL_seeds_not_any():
    """Pins the chaining fix.

    MEASURED on the live corpus: an `any(...)` expansion grew a 2-seed group
    into a 70-member cluster whose shared-symptom set was EMPTY — the emptiness
    being the tell that its members no longer shared anything. `c` below is
    close to seed `a` and unrelated to seed `b`, which is exactly that shape.

    This test discriminates: under `any` it FAILS (c is admitted), under `all`
    it passes. That was verified by reverting the operator, not assumed.
    """
    a = _rec("rb-1", "alpha symptom not diagnosed")
    b = _rec("rb-2", "beta symptom not diagnosed")
    c = _rec("rb-3", "unrelated neighbour of a only")
    sim = _sim_from_map({
        frozenset(("rb-1", "rb-2")): 0.9,   # the two seeds cluster
        frozenset(("rb-1", "rb-3")): 0.9,   # c is close to a...
        frozenset(("rb-2", "rb-3")): 0.0,   # ...and unrelated to b
    })
    clusters = rc.build_clusters([a, b, c], threshold=0.55, min_admissions=2,
                                 min_cluster=2, sim=sim)
    assert len(clusters) == 1
    assert [r.id for r in clusters[0]] == ["rb-1", "rb-2"]


def test_cluster_needs_min_admissions_seeds():
    a = _rec("rb-1", "shared symptom not diagnosed")
    b = _rec("rb-2", "shared symptom fully explained")   # no admission
    sim = _sim_from_map({frozenset(("rb-1", "rb-2")): 0.9})
    assert rc.build_clusters([a, b], threshold=0.55, min_admissions=2,
                             min_cluster=2, sim=sim) == []


def test_min_cluster_size_is_enforced():
    a = _rec("rb-1", "shared symptom not diagnosed")
    b = _rec("rb-2", "shared symptom cause unknown")
    sim = _sim_from_map({frozenset(("rb-1", "rb-2")): 0.9})
    assert rc.build_clusters([a, b], threshold=0.55, min_admissions=2,
                             min_cluster=3, sim=sim) == []
    assert len(rc.build_clusters([a, b], threshold=0.55, min_admissions=2,
                                 min_cluster=2, sim=sim)) == 1


def test_below_threshold_does_not_cluster():
    a = _rec("rb-1", "one symptom not diagnosed")
    b = _rec("rb-2", "other symptom cause unknown")
    sim = _sim_from_map({frozenset(("rb-1", "rb-2")): 0.54})
    assert rc.build_clusters([a, b], threshold=0.55, min_admissions=2,
                             min_cluster=2, sim=sim) == []


def test_lexical_basis_is_the_default_when_no_sim_supplied():
    """The fallback must remain wired — a basis nobody can reach is not a
    fallback. Identical titles are jaccard 1.0, so they cluster on it."""
    a = _rec("rb-1", "identical title not diagnosed")
    b = _rec("rb-2", "identical title not diagnosed")
    clusters = rc.build_clusters([a, b], threshold=0.55, min_admissions=2,
                                 min_cluster=2)
    assert len(clusters) == 1


# --------------------------------------------------------------------------
# cause divergence
# --------------------------------------------------------------------------

def test_cause_tokens_read_only_causal_sentences():
    """Whole-record tokens are dominated by the shared symptom vocabulary that
    clustered the entries, so comparing them would make every cluster look
    internally consistent. Only causal sentences may contribute."""
    r = _rec("rb-1", "t", "Shared symptom words everywhere. "
                          "Root cause: a stale filesystem handle.")
    assert "stale" in r.cause_tokens
    assert "everywhere" not in r.cause_tokens


def test_title_does_NOT_leak_into_cause_tokens():
    """Regression for a bug this suite caught on its first run.

    A title has no sentence terminator, so `title + " " + content` merges it
    into the FIRST content sentence. When that sentence asserts a cause, every
    title token is absorbed into the causal claim — and those are exactly the
    shared-symptom tokens that clustered the entries, so cause-similarity is
    inflated across the board and divergence is silently suppressed. Measured
    on the pair below: 0.45 (not divergent) with the title, 0.10 (divergent)
    without. The failure mode is invisible — it makes the detector report
    agreement, never an error.
    """
    a = _rec("rb-1", "widget sync fails",
             "Root cause: a stale alpha handle.")
    b = _rec("rb-2", "widget sync fails",
             "Root cause: beta timeout entirely.")
    for tok in ("widget", "sync", "fails"):
        assert tok not in a.cause_tokens, "title token leaked into causal claim"
    assert rc.divergent_causes([a, b], divergence=0.30), (
        "disjoint causes under a SHARED title must still read as divergent")


def test_divergent_causes_flags_disjoint_claims_and_is_capped():
    # Deliberately disjoint vocabulary per entry. An earlier version of this
    # fixture reused "distinct reason" across all eight, which made them 0.67
    # similar — the fixture, not the code, was asserting the wrong thing.
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
             "hotel"]
    recs = [
        _rec("rb-%d" % i, "t%d" % i, "Root cause: %s%d %s%d" % (w, i, w, i * 7))
        for i, w in enumerate(words)
    ]
    pairs = rc.divergent_causes(recs, divergence=0.30)
    assert pairs, "disjoint causal claims must be flagged"
    assert len(pairs) <= rc.MAX_DIVERGENT_PAIRS
    assert pairs == sorted(pairs, key=lambda t: t[2]), "most-divergent first"


def test_agreeing_causes_are_not_flagged():
    a = _rec("rb-1", "t", "Root cause: the shared stale filesystem handle here")
    b = _rec("rb-2", "t", "Root cause: the shared stale filesystem handle here")
    assert rc.divergent_causes([a, b], divergence=0.30) == []


# --------------------------------------------------------------------------
# corpus loading
# --------------------------------------------------------------------------

def test_load_active_skips_inactive_and_malformed(tmp_path):
    store = _write_store(tmp_path, [])
    store.write_text("\n".join([
        json.dumps({"id": "rb-1", "title": "live one", "status": "active"}),
        json.dumps({"id": "rb-2", "title": "gone", "status": "retired"}),
        json.dumps({"id": "rb-3", "title": "gone too", "status": "superseded"}),
        "{ not json at all",
        json.dumps({"id": "rb-4", "title": "", "status": "active"}),   # no title
        json.dumps({"id": "rb-5", "title": "live two"}),               # no status
    ]), encoding="utf-8")
    ids = [r.id for r in rc.load_active(store)]
    assert ids == ["rb-1", "rb-5"], "one malformed line must not silence the scan"


def test_load_active_on_missing_file_returns_empty(tmp_path):
    assert rc.load_active(tmp_path / "nope.jsonl") == []


# --------------------------------------------------------------------------
# cluster identity + filing idempotency (outcome 2)
# --------------------------------------------------------------------------

def test_cluster_key_is_stable_and_order_independent():
    a, b = _rec("rb-1", "x"), _rec("rb-2", "y")
    assert rc.cluster_key([a, b]) == rc.cluster_key([b, a])
    assert rc.cluster_key([a, b]) != rc.cluster_key([a])


def test_already_filed_fails_SAFE_when_the_query_breaks(monkeypatch):
    """Fail-safe direction is 'assume filed'.

    A missed Investigate costs one cycle; a duplicate pollutes the shared queue
    and has to be closed by hand. So an unreadable query must NOT be treated as
    'nothing found' — that is the silent-failure class where a broken probe
    authorizes a write.
    """
    def boom(*a, **k):
        raise OSError("query unavailable")
    monkeypatch.setattr(subprocess, "run", boom)
    assert rc.already_filed("deadbeef") is True

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 2, "", "err"))
    assert rc.already_filed("deadbeef") is True


def test_already_filed_reads_empty_result_as_not_filed(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]", ""))
    assert rc.already_filed("deadbeef") is False


def test_file_investigate_is_idempotent(monkeypatch):
    """Outcome 2: at most one Investigate per cluster, across runs."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1].endswith("aspirations-query.sh"):
            # First pass: nothing filed. Second pass: the goal now exists.
            filed = any(c[1].endswith("aspirations-add-goal.sh") for c in calls)
            out = '[{"goal_id": "g-1"}]' if filed else "[]"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    finding = {"cluster_id": "abc123", "members": ["rb-1", "rb-2"], "size": 3,
               "admitting": [{"id": "rb-1", "phrases": ["not diagnosed"],
                              "title": "t"}],
               "shared_symptom_tokens": ["own", "cloud"],
               "divergent_cause_pairs": []}

    filed, _ = rc.file_investigate(finding)
    assert filed is True
    filed_again, detail = rc.file_investigate(finding)
    assert filed_again is False
    assert "already filed" in detail
    adds = [c for c in calls if c[1].endswith("aspirations-add-goal.sh")]
    assert len(adds) == 1, "a cluster must never be filed twice"


def test_filed_description_carries_the_dedup_marker(monkeypatch):
    """The dedup probe reads DESCRIPTIONS, because that is what the duplication
    gate reads. A marker present only in the title would make the probe
    narrower than the gate that creates the population — the predicate-mismatch
    class that makes a sweep report clean forever."""
    captured = {}

    def fake_run(cmd, **kwargs):
        if cmd[1].endswith("aspirations-query.sh"):
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        captured["body"] = json.loads(kwargs["input"])
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    finding = {"cluster_id": "abc123", "members": ["rb-1"], "size": 3,
               "admitting": [{"id": "rb-1", "phrases": ["quirk"], "title": "t"}],
               "shared_symptom_tokens": [], "divergent_cause_pairs": []}
    rc.file_investigate(finding)
    assert "[rb-cluster:abc123]" in captured["body"]["description"]
    assert captured["body"]["participants"] == ["agent"]


# --------------------------------------------------------------------------
# end-to-end
# --------------------------------------------------------------------------

def test_assess_reports_predicates_and_stays_report_only(tmp_path, monkeypatch):
    store = _write_store(tmp_path, [
        {"id": "rb-1", "title": "widget sync fails", "status": "active",
         "content": "Root cause: a stale alpha handle. Mechanism not established."},
        {"id": "rb-2", "title": "widget sync fails", "status": "active",
         "content": "Root cause: beta timeout entirely. cause is unknown"},
        {"id": "rb-3", "title": "widget sync fails", "status": "active",
         "content": "no claim here"},
    ])
    def explode(*a, **k):  # nothing may be filed without --apply
        raise AssertionError("assess must never file")
    monkeypatch.setattr(subprocess, "run", explode)

    findings = rc.assess(rc.load_active(store), threshold=0.55,
                         min_cluster=3, min_admissions=2)
    assert len(findings) == 1
    f = findings[0]
    assert set(f["members"]) == {"rb-1", "rb-2", "rb-3"}
    assert "a-undiagnosed-admissions" in f["predicates"]
    assert "b-divergent-cause-claims" in f["predicates"]
    assert f["divergent_pairs_total"] >= 1
