"""Regression pins for drain-encode-probe.py ().

The probe gates /drain-temp's DISCARD branch. Two properties are load-bearing
and neither is visible from a passing happy-path test, so both are pinned here:

  1. FAIL-OPEN. `absent` blocks a discard. If any internal error could surface
     as `absent`, a probe bug would wedge the drain lane instead of merely
     degrading to today's LLM-judgement behaviour.

  2. THE FALSE-ABSENT GUARD. A batch key that merely shares a name with a
     front-matter field is not an observable effect. Measured over 1309 live
     nodes: `poignancy` is used by 55, while `utility_ratio` / `times_helpful`
     / `retrieval_count` are used by exactly 1 each. A boolean "is this a real
     key" test passes all four and reports `absent` for a distillation report
     whose effect is not in front matter at all -- blocking that discard
     forever. Only the threshold separates them.

Shapes here are taken from the live temp corpus (n=366 json + 27 md), not
invented: fixtures that construct the shape the checker expects are the exact
trap the checker-input-assumption-defects node warns about.
"""

import importlib.util
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_SPEC = importlib.util.spec_from_file_location(
    "drain_encode_probe", os.path.join(_SCRIPTS, "drain-encode-probe.py")
)
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["drain_encode_probe"] = probe
_SPEC.loader.exec_module(probe)


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


# --------------------------------------------------------------- classification


def test_classifies_real_payload_shapes(tmp_path):
    """Field-shape inference, using the shapes the live corpus actually holds."""
    cases = [
        ({"title": "t", "type": "success", "category": "c", "content": "x"},
         "reasoning_bank"),
        ({"rule": "always do x", "category": "c", "trigger_condition": "when"},
         "guardrail"),
        ({"title": "t", "priority": "MEDIUM", "participants": ["agent"]}, "goal"),
        ({"id": "exp-1", "content_path": "a/b.md"}, "experience"),
    ]
    for payload, expected in cases:
        f = _write(tmp_path, expected + ".json", payload)
        assert probe.classify(f)[0] == expected


def test_reasoning_bank_wins_over_guardrail_on_overlapping_keys(tmp_path):
    """An rb payload also carries `category`; the more specific shape must win."""
    f = _write(tmp_path, "rb.json", {
        "title": "t", "type": "failure", "category": "c",
        "content": "x", "rule": "incidental",
    })
    assert probe.classify(f)[0] == "reasoning_bank"


def test_bare_key_is_not_a_tree_batch(tmp_path):
    """`key` alone must not classify as a tree batch.

    Measured: 4 arrays in the live corpus carry a bare `key` and 3 of them are
    e-mail alert records where the token is coincidental. Keying on `key` alone
    misclassifies 75% of that lane.
    """
    alertish = [{"key": "abc", "bucket": "x", "date": "d", "is_alert": True}]
    assert probe.classify(_write(tmp_path, "a.json", alertish))[0] == "query_capture"

    treeish = [{"key": "n", "file": "world/knowledge/tree/x.md", "poignancy": 7}]
    assert probe.classify(_write(tmp_path, "b.json", treeish))[0] == "tree_batch"


def test_md_without_goal_id_is_not_a_trace(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("just some prose with no goal reference", encoding="utf-8")
    assert probe.classify(str(p))[0] == "unknown_shape"

    # A goal id found ONLY in the body is a CITATION until shown otherwise --
    # measured 4 of 4 (echo/cc-03, 2026-08-21, ): every first-token id
    # named a goal the file merely referenced, never its owner, and all four
    # files had in fact already landed. So this shape is `trace_md_weak`, whose
    # probe returns `unknown` rather than a definite `absent` ().
    p2 = tmp_path / "trace.md"
    p2.write_text("trace for g-115-3089 execution", encoding="utf-8")
    assert probe.classify(str(p2)) == ("trace_md_weak", "g-115-3089")
    assert probe._PROBES["trace_md_weak"]("g-115-3089")[0] == "unknown"


def test_trace_md_owner_resolution_precedence(tmp_path):
    """Owner comes from the FILENAME first, then front matter, then (weakly) body."""
    fn = tmp_path / "g-364-132-notes.md"
    fn.write_text("---\ngoal_id: g-999-1\n---\ncites g-777-2\n", encoding="utf-8")
    assert probe.classify(str(fn)) == ("trace_md", "g-364-132")

    fm = tmp_path / "analysis.md"
    fm.write_text("---\ngoal_id: g-888-3\n---\ncites g-777-2\n", encoding="utf-8")
    assert probe.classify(str(fm)) == ("trace_md", "g-888-3")

    body = tmp_path / "analysis2.md"
    body.write_text("cites g-777-2 only\n", encoding="utf-8")
    assert probe.classify(str(body)) == ("trace_md_weak", "g-777-2")


def test_front_matter_goal_id_is_scoped_to_the_fence_block(tmp_path):
    """A `goal_id:` line OUTSIDE front matter must not be promoted to owner.

    Regression, g-115-8496: `_FM_GOAL_ID_RE` carries re.M, so its `^` matches
    every line start. Run against the whole document it promoted any body line
    beginning `goal_id:` -- prose, or a line inside a fenced code block -- to
    front-matter provenance and a DEFINITE verdict, which is the exact
    citation-treated-as-owner defect this module was changed to eliminate.
    It was strictly WORSE than the prior behaviour for that input: a body id
    used to take the weak path unconditionally.
    """
    prose = tmp_path / "a1.md"
    prose.write_text(
        "# notes\n\nSome prose.\n\n"
        "goal_id: g-999-9 was mentioned in a code sample\n\ncites g-777-7\n",
        encoding="utf-8")
    assert probe.classify(str(prose))[0] == "trace_md_weak"

    fenced = tmp_path / "a2.md"
    fenced.write_text(
        "# doc\n\n```yaml\ngoal_id: g-888-8\n```\n\nreal cite g-777-7\n",
        encoding="utf-8")
    assert probe.classify(str(fenced))[0] == "trace_md_weak"

    # Positive control -- REAL front matter must still resolve strongly, or the
    # fix would have been a blanket downgrade rather than a scoping correction.
    real = tmp_path / "a3.md"
    real.write_text("---\ngoal_id: g-888-3\n---\ncites g-777-2\n", encoding="utf-8")
    assert probe.classify(str(real)) == ("trace_md", "g-888-3")

    # Malformed front matter resolves WEAK, never strongly: an unterminated
    # fence and an empty block both mean "no front matter established".
    for name, text in (
        ("e1.md", "---\ngoal_id: g-111-1\nno closing fence\ncites g-222-2\n"),
        ("e2.md", "---\n---\ncites g-333-3\n"),
    ):
        f = tmp_path / name
        f.write_text(text, encoding="utf-8")
        assert probe.classify(str(f))[0] == "trace_md_weak", name


def test_message_scratch_never_judged_by_experience_store(tmp_path):
    """Message scratch asks the wrong store, so it must never return `absent`.

    The durable artifact for these shapes is a sent message (submitted PR, a row
    in world/board/*.jsonl, outbound mail) -- not an experience record. Measured
    2026-08-12 (bravo/cc-05): 24 of 49 permanently DISCARD-blocked .md were this
    shape. guard-3616 forbids a definite verdict for the no-information case.
    """
    for name in ("notify-user-x.md", "pr-body-463.md", "g-115-1-pr-body.md",
                 "board-post-x.md", "reply-zach.md", "ack-alpha.md",
                 "stop-email.md", "forge-notice-skill.md", "coord-alpha.md"):
        f = tmp_path / name
        f.write_text("see g-115-9999 for context\n", encoding="utf-8")
        atype, payload = probe.classify(str(f))
        assert atype == "message_scratch", name
        assert probe._PROBES[atype](payload)[0] == "unknown", name

    # Negative control: an ordinary working doc must NOT be captured by the
    # filename shapes -- there the experience-store question is fair.
    ordinary = tmp_path / "prune-prep.md"
    ordinary.write_text("cites g-115-9999 here\n", encoding="utf-8")
    assert probe.classify(str(ordinary))[0] != "message_scratch"


# ------------------------------------------------------------------- fail-open


def test_probe_error_yields_unknown_never_absent(tmp_path, monkeypatch):
    """An exception inside a probe must degrade to `unknown`, never `absent`."""
    def boom(_payload):
        raise RuntimeError("simulated store failure")

    monkeypatch.setitem(probe._PROBES, "goal", boom)
    f = _write(tmp_path, "g.json", {
        "title": "t", "priority": "MEDIUM", "participants": ["agent"]})
    r = probe.probe_file(f)
    assert r["verdict"] == "unknown"
    assert r["verdict"] != "absent"
    assert "fail-open" in r["evidence"]


def test_unreadable_store_yields_unknown(tmp_path, monkeypatch):
    """A store read returning nothing is `unknown`, not `absent`."""
    monkeypatch.setattr(probe, "_run", lambda args: None)
    f = _write(tmp_path, "rb.json", {
        "title": "t", "type": "success", "category": "c", "content": "x"})
    assert probe.probe_file(f)["verdict"] == "unknown"


# Every payload shape that reaches a store, one per lane. Parametrised on
# purpose: the single-lane version of this test above covered only
# reasoning_bank -- the one lane that was already correct -- so probe_experience
# mapped an unreadable store to `absent` for its whole life and no test saw it.
# A per-lane fixture that exercises the lane the author had in mind is exactly
# how a contract violation stays invisible.
_OUTAGE_PAYLOADS = [
    ("reasoning_bank", {"title": "t", "type": "success",
                        "category": "c", "content": "x"}),
    ("guardrail", {"rule": "always do x", "category": "c"}),
    ("goal", {"title": "t", "priority": "MEDIUM", "participants": ["agent"]}),
    ("experience", {"id": "exp-x", "content_path": "a/b.md"}),
]


@pytest.mark.parametrize("lane,payload", _OUTAGE_PAYLOADS,
                         ids=[n for n, _ in _OUTAGE_PAYLOADS])
def test_store_outage_never_yields_absent_on_any_lane(
        tmp_path, monkeypatch, lane, payload):
    """FAIL-OPEN CONTRACT, enforced per lane.

    `absent` blocks a discard, so emitting it from an unreadable store would
    wedge every file of that shape in the drain queue during a daemon outage.
    `_run` returns None for a subprocess exception, a non-zero rc, empty stdout
    AND a JSON parse failure alike -- none of which is evidence of absence.
    """
    monkeypatch.setattr(probe, "_run", lambda args: None)
    r = probe.probe_file(_write(tmp_path, lane + ".json", payload))
    assert r["verdict"] == "unknown", (
        "lane %s returned %r under a store outage; the fail-open contract "
        "permits only 'unknown'" % (lane, r["verdict"])
    )


def test_store_outage_never_yields_absent_on_trace_md(tmp_path, monkeypatch):
    """The .md lane reaches the same wrapper and must obey the same contract."""
    monkeypatch.setattr(probe, "_run", lambda args: None)
    p = tmp_path / "trace.md"
    p.write_text("trace for g-115-3089", encoding="utf-8")
    assert probe.probe_file(str(p))["verdict"] == "unknown"


def test_empty_result_is_still_absent(tmp_path, monkeypatch):
    """The fix must not over-correct: a well-formed EMPTY result IS absence."""
    monkeypatch.setattr(probe, "_run", lambda args: [])
    r = probe.probe_file(_write(tmp_path, "e.json",
                                {"id": "exp-x", "content_path": "a/b.md"}))
    assert r["verdict"] == "absent"


def test_missing_file_is_unknown():
    r = probe.probe_file("/nonexistent/nope.json")
    assert r["verdict"] == "unknown"


def test_unparseable_json_is_unknown(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not valid json", encoding="utf-8")
    r = probe.probe_file(str(p))
    assert r["artifact_type"] == "unreadable"
    assert r["verdict"] == "unknown"


# -------------------------------------------------------- the false-absent guard


def test_batch_field_below_vocab_threshold_is_unknown_not_absent(monkeypatch):
    """The distill.json case: a telemetry INPUT is not an observable effect.

    Before the threshold this returned `absent` and would have blocked every
    distillation report's discard permanently.
    """
    monkeypatch.setattr(probe, "_tree_fm_vocabulary",
                        lambda: {"utility_ratio": 1, "times_helpful": 1})
    rows = [{"key": "n", "file": "world/knowledge/tree/x.md",
             "utility_ratio": 0.5, "times_helpful": 3}]
    verdict, evidence = probe.probe_tree_batch(rows)
    assert verdict == "unknown"
    assert "established tree front-matter key" in evidence


def test_batch_field_above_threshold_still_reports_absent(monkeypatch, tmp_path):
    """The poig_batch case must survive the guard: a real field, genuinely unapplied."""
    node = tmp_path / "n.md"
    node.write_text("---\ntopic: t\nlast_updated: '2026-08-01'\n---\nbody\n",
                    encoding="utf-8")
    monkeypatch.setattr(probe, "_tree_fm_vocabulary", lambda: {"poignancy": 55})
    monkeypatch.setattr(probe, "_resolve_node_path", lambda f: str(node))
    verdict, _ = probe.probe_tree_batch(
        [{"key": "n", "file": "world/knowledge/tree/n.md", "poignancy": 7}])
    assert verdict == "absent"


def test_batch_field_present_reports_encoded(monkeypatch, tmp_path):
    node = tmp_path / "n.md"
    node.write_text("---\ntopic: t\npoignancy: 7\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(probe, "_tree_fm_vocabulary", lambda: {"poignancy": 55})
    monkeypatch.setattr(probe, "_resolve_node_path", lambda f: str(node))
    verdict, _ = probe.probe_tree_batch(
        [{"key": "n", "file": "world/knowledge/tree/n.md", "poignancy": 7}])
    assert verdict == "encoded"


def test_partial_application_is_unknown(monkeypatch, tmp_path):
    """Some targets applied and some not is ambiguous -- must not claim either."""
    applied = tmp_path / "a.md"
    applied.write_text("---\npoignancy: 7\n---\n", encoding="utf-8")
    staged = tmp_path / "b.md"
    staged.write_text("---\ntopic: t\n---\n", encoding="utf-8")
    monkeypatch.setattr(probe, "_tree_fm_vocabulary", lambda: {"poignancy": 55})
    monkeypatch.setattr(probe, "_resolve_node_path",
                        lambda f: str(applied if f.endswith("a.md") else staged))
    verdict, evidence = probe.probe_tree_batch([
        {"key": "a", "file": "world/knowledge/tree/a.md", "poignancy": 7},
        {"key": "b", "file": "world/knowledge/tree/b.md", "poignancy": 7},
    ])
    assert verdict == "unknown"
    assert "partial" in evidence


def test_unreadable_targets_yield_unknown(monkeypatch):
    monkeypatch.setattr(probe, "_tree_fm_vocabulary", lambda: {"poignancy": 55})
    monkeypatch.setattr(probe, "_resolve_node_path", lambda f: None)
    verdict, _ = probe.probe_tree_batch(
        [{"key": "n", "file": "world/knowledge/tree/n.md", "poignancy": 7}])
    assert verdict == "unknown"


# ------------------------------------------------------------ path resolution


def test_world_prefix_is_not_joined_to_repo_root():
    """`world/` is EXTERNAL. Joining it to the repo root makes every node
    unreadable and silently degrades the whole lane to `unknown`."""
    resolved = probe._resolve_node_path("world/knowledge/tree/x.md")
    if resolved is not None:
        assert not resolved.startswith(os.path.join(probe.REPO, "world")), (
            "world/ must resolve through _paths.resolve_file_path, not repo-join"
        )


def test_absolute_path_passes_through(tmp_path):
    p = str(tmp_path / "x.md")
    assert probe._resolve_node_path(p) == p


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
