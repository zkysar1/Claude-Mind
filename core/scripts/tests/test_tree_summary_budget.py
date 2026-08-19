"""Byte-budget pins for the bounded tree-summary projection ().

THE PIN'S THRESHOLD IS THE LITERAL EXTERNAL CAP, 262144, and that is the single
most important property in this file. The motivating goal records that the
previous attempt's first draft asserted `size <= DEFAULT_BUDGET`, where the
budget is computed from BUDGET_FRACTION -- so mutating the fraction moved the
threshold with it, the test stayed green, and the file blew past the cap. A pin
whose threshold is derived from the constant it pins is not a pin. Hence
`_CAP = 262144` written out here as a literal, deliberately NOT imported from
the module under test, and `test_pin_threshold_is_independent_of_the_module`
which fails if anyone "tidies" that into an import.

The corpora are synthetic so the pins are hermetic and deterministic; one
opt-in test measures the live tree when it is present.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _tree_summary as ts  # noqa: E402

# The Read-tool cap, as an EXTERNAL fact about the tool. Never import this.
_CAP = 262144

_MODULE = Path(__file__).resolve().parents[1] / "_tree_summary.py"


def _corpus(n_nodes, summary_len=300, max_depth=10):
    """A synthetic generator payload shaped exactly like tree-read.sh --summary."""
    nodes = {}
    for i in range(n_nodes):
        depth = (i % max_depth) + 1
        nodes["node-%04d" % i] = {
            "file": "world/knowledge/tree/" + "/".join(
                "seg%d" % d for d in range(depth)) + "/node-%04d.md" % i,
            "summary": "s" * summary_len,
            "depth": depth,
            "capability_level": "CALIBRATE",
            "confidence": 0.7 if i % 3 == 0 else None,
            "last_updated": "2026-08-11",
            "article_count": i % 5,
            "children": ["c%d" % j for j in range(i % 4)],
        }
    return {"nodes": nodes, "total": n_nodes}


def _size(obj):
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------------------
# The cap pin, and the proof that it discriminates.
# ---------------------------------------------------------------------------

def test_bounded_projection_is_under_the_external_cap():
    raw = _corpus(1400)
    out, _ = ts.build_summary(raw)
    assert _size(out) < _CAP


def test_the_pin_FAILS_against_the_pre_fix_projection():
    """Outcome 4: the pin must be shown to fail on the unbounded projection.

    The pre-fix loader wrote the generator payload VERBATIM, so the raw corpus
    IS the pre-fix artifact. If this assertion ever stops holding, the corpus
    has shrunk below the cap and every other pin here has gone vacuous.
    """
    raw = _corpus(1400)
    assert _size(raw) > _CAP, (
        "the synthetic corpus no longer exceeds the cap, so these pins would "
        "pass even with the bound removed -- enlarge the corpus"
    )
    out, _ = ts.build_summary(raw)
    assert _size(out) < _CAP < _size(raw)


def test_pin_threshold_is_independent_of_the_module(monkeypatch):
    """Mutating BUDGET_FRACTION must NOT move this test's threshold.

    This is the exact failure the goal warns about. With a wildly inflated
    fraction the projection SHOULD breach the external cap and this test SHOULD
    notice -- proving the threshold is external rather than co-moving.
    """
    monkeypatch.setattr(ts, "BUDGET_FRACTION", 8.0)
    raw = _corpus(1400)
    out, _ = ts.build_summary(raw)
    assert _size(out) > _CAP, (
        "inflating BUDGET_FRACTION did not breach the external cap -- the "
        "budget is not actually driving the bound"
    )


def test_bound_is_a_fraction_of_the_cap_not_a_constant():
    assert ts.default_budget() == int(ts.READ_TOOL_CAP * ts.BUDGET_FRACTION)
    assert 0 < ts.BUDGET_FRACTION < 1


def test_bound_holds_as_the_corpus_grows():
    """Outcome 1: under the cap on a corpus AT LEAST as large as today's tree.

    Today's tree is ~1,375 nodes; 6,000 is >4x. A constant tuned to today's
    corpus passes the 1,400 case and fails here, which is the whole point of
    expressing the bound as a fraction of the cap.
    """
    for n in (1400, 3000, 6000):
        out, _ = ts.build_summary(_corpus(n))
        assert _size(out) < _CAP, "breached the cap at %d nodes" % n


# ---------------------------------------------------------------------------
# No silent truncation.
# ---------------------------------------------------------------------------

def test_omissions_are_accounted_exactly():
    raw = _corpus(1400)
    out, stats = ts.build_summary(raw)
    assert out["nodes_included"] + out["nodes_omitted"] == 1400
    assert out["nodes_included"] == len(out["nodes"])
    assert sum(out["omitted_by_depth"].values()) == out["nodes_omitted"]
    assert stats["nodes_omitted"] == out["nodes_omitted"]


def test_something_is_actually_omitted_on_an_oversized_corpus():
    """Guards the accounting tests above from passing vacuously."""
    out, _ = ts.build_summary(_corpus(1400))
    assert out["nodes_omitted"] > 0


def test_projection_note_names_the_full_fidelity_route():
    out, _ = ts.build_summary(_corpus(1400))
    note = out["projection_note"]
    assert "tree-read.sh" in note and "retrieve.sh" in note


def test_stderr_announces_omissions(tmp_path, capsys):
    src = tmp_path / "gen.json"
    src.write_text(json.dumps(_corpus(1400)), encoding="utf-8")
    rc = ts.main([str(src)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "omitted" in captured.err
    assert json.loads(captured.out)["nodes_omitted"] > 0


def test_small_corpus_omits_nothing_and_stays_quiet(tmp_path, capsys):
    src = tmp_path / "gen.json"
    src.write_text(json.dumps(_corpus(20)), encoding="utf-8")
    rc = ts.main([str(src)])
    captured = capsys.readouterr()
    assert rc == 0
    out = json.loads(captured.out)
    assert out["nodes_omitted"] == 0
    assert out["nodes_included"] == 20
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Shape + ordering contracts the one live consumer depends on.
# ---------------------------------------------------------------------------

def test_shape_matches_what_consumers_iterate():
    """`nodes` must stay a DICT keyed by node key, with `total` alongside.

    A prior incident (recorded against strategic-scan) had consumers iterating
    this payload as if it were a list, yielding top-level strings and silently
    selecting zero rows. Changing the shape here would re-create that class.
    """
    out, _ = ts.build_summary(_corpus(50))
    assert isinstance(out["nodes"], dict)
    assert out["total"] == 50
    row = next(iter(out["nodes"].values()))
    for field in ("path", "summary", "depth", "capability_level",
                  "article_count", "last_updated", "n_children"):
        assert field in row


def test_shallow_nodes_survive_when_deep_ones_are_cut():
    out, _ = ts.build_summary(_corpus(1400))
    kept_depths = {r["depth"] for r in out["nodes"].values()}
    omitted_depths = {int(d) for d in out["omitted_by_depth"]}
    assert min(kept_depths) <= min(omitted_depths, default=99)
    assert max(omitted_depths) > min(kept_depths)


def test_omission_set_is_deterministic():
    a, _ = ts.build_summary(_corpus(1400))
    b, _ = ts.build_summary(_corpus(1400))
    assert list(a["nodes"]) == list(b["nodes"])


def test_path_is_stripped_of_prefix_and_suffix():
    row = ts.project_node({"file": "world/knowledge/tree/a/b/c.md"})
    assert row["path"] == "a/b/c"


def test_children_become_a_count():
    row = ts.project_node({"children": ["x", "y", "z"]})
    assert row["n_children"] == 3
    assert "children" not in row


def test_confidence_omitted_when_absent_but_kept_when_present():
    assert "confidence" not in ts.project_node({"confidence": None})
    assert ts.project_node({"confidence": 0.4})["confidence"] == 0.4


def test_long_summary_truncation_is_visible():
    row = ts.project_node({"summary": "x" * 5000})
    assert row["summary"].endswith("...")
    assert len(row["summary"]) == ts.SUMMARY_MAX + 3


# ---------------------------------------------------------------------------
# Failure handling.
# ---------------------------------------------------------------------------

def test_unparseable_generator_output_refuses_loudly(tmp_path, capsys):
    """Emitting nothing is the point: a passed-through malformed payload would
    replace a usable cached summary with something that parses as nothing."""
    src = tmp_path / "bad.json"
    src.write_text("{not json", encoding="utf-8")
    rc = ts.main([str(src)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "refusing" in captured.err


def test_empty_corpus_is_not_an_error():
    out, stats = ts.build_summary({"nodes": {}, "total": 0})
    assert out["nodes"] == {} and out["nodes_omitted"] == 0
    assert stats["nodes_total"] == 0


def test_module_runs_as_a_subprocess_on_stdin(tmp_path):
    """The loader invokes it as a pipeline stage, so pin that shape too."""
    payload = json.dumps(_corpus(300))
    proc = subprocess.run(
        [sys.executable, str(_MODULE)], input=payload,
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert len(json.dumps(out).encode()) < _CAP


@pytest.mark.parametrize("frac", [0.5, 0.75, 0.9])
def test_any_sane_fraction_still_respects_the_external_cap(monkeypatch, frac):
    monkeypatch.setattr(ts, "BUDGET_FRACTION", frac)
    out, _ = ts.build_summary(_corpus(1400))
    assert _size(out) < _CAP
