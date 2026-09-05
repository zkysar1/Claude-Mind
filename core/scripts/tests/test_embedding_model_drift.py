"""test_embedding_model_drift.py — the index-vs-CALIBRATION guard (2026-09-03).

tree.yaml's absolute cosine floors (embedding_min_cosine 0.35,
embedding_tree_min_cosine 0.32) are calibrated on the CONFIGURED model
(retrieval: embedding_model_name). A per-box index built with a different
model puts a different cosine distribution under those floors; measured
live, a bge-small index under MiniLM-calibrated floors admitted 98.9-100%
of the tree for ANY query ("how to bake sourdough bread" and a Kubernetes
TLS query returned an identical top-3), so ranking collapsed onto the
query-independent static bonuses and the same three nodes topped every
retrieval on the box. The raw cosine channel was healthy throughout.

Invariants pinned here:

  1. DRIFT IS DETECTED from the index's own meta.json vs the configured
     name, without loading an encoder (a stat + string compare).
  2. EVERY embedding lane FREEZES to the token baseline on drift — tree
     channel, framework channel, supplementary blend, universal-RB pull
     slots — and never calls cosine_scores (the exploding stub proves it).
  3. embedding_channel_status() reports "DRIFT: ..." (never "alive") so
     the state is visible per request, naming the self-heal command.
  4. The builder's --update REBUILDS a drifted index with the configured
     model, and REFUSES (rc=2, index untouched) only when that model
     cannot load on the box. The exit code reaches the shell. --stats
     carries configured_model + model_drift.
  5. The freeze warning prints ONCE per process (hot path).

Same bootstrap as test_embedding_channel_status.py: retrieve.py via
importlib against a scratch MIND_WORLD; the builder (hyphenated filename)
via importlib like test_embedding_index_freshness.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# numpy is OPTIONAL on this fleet and a bare top-level import here is not a
# local failure -- it raises at COLLECTION, and a collection error aborts the
# ENTIRE pytest invocation ("Interrupted: 1 error during collection"). So on a
# box without numpy this one file zeroes every other file in its chunk and
# run-full-suite reports VERDICT: INVALID, because the totals are structurally
# incomplete. Measured 2026-09-03 (echo, cc-03, Linux 6.8.0-138-generic): at
# --chunks 16, chunk 04 "produced no parseable test output" -- 0 passed, 1
# error, 85 files contributed nothing -- invalidating the whole run. Deterministic,
# so it invalidates EVERY run on such a box at ANY rung, not just this one.
# importorskip is the established idiom here (21 other test files use it) and
# degrades this to a clean skip.
np = pytest.importorskip("numpy")

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="embedding-drift-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_spec = importlib.util.spec_from_file_location(
    "retrieve_embdrift_mod", CORE_SCRIPTS / "retrieve.py")
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

_bspec = importlib.util.spec_from_file_location(
    "embedding_index_build_drift_mod", CORE_SCRIPTS / "embedding-index-build.py")
_builder = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(_builder)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import _embedding_retrieval as er  # noqa: E402

CONFIGURED = "all-MiniLM-L6-v2"
DRIFTED = "BAAI/bge-small-en-v1.5"


def _write_index(d: Path, model: str, ids=("guard-1", "rb-2", "tree:a.md")):
    d.mkdir(parents=True, exist_ok=True)
    arr = np.zeros((len(ids), 384), dtype="float16")
    arr[:, 0] = 1.0
    np.save(d / "embeddings.npy", arr)
    meta = {"model": model, "backend": "fastembed", "dim": 384,
            "count": len(ids),
            "docs": [{"id": i, "type": "guardrail", "hash": "h"} for i in ids]}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _cfg(model=CONFIGURED, **flags):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg.update({"embedding_blend_enabled": True,
                "embedding_tree_channel_enabled": True,
                "embedding_framework_channel_enabled": True})
    if model is None:
        cfg.pop("embedding_model_name", None)
    else:
        cfg["embedding_model_name"] = model
    cfg.update(flags)
    return cfg


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    monkeypatch.setenv(er._INDEX_DIR_ENV, str(tmp_path / "index"))
    er.clear_caches()
    _retrieve._MODEL_DRIFT_WARNED = False
    _retrieve._BLEND_STATS.clear()
    yield tmp_path / "index"
    _retrieve._RETRIEVAL_CFG_CACHE = saved
    _retrieve._MODEL_DRIFT_WARNED = False
    er.clear_caches()


# ---------------------------------------------------------------- detection

def test_no_drift_when_index_model_matches_configured(_isolate):
    _write_index(_isolate, CONFIGURED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    assert _retrieve._embedding_model_drift() is None
    assert _retrieve._freeze_on_model_drift() is False


def test_drift_detected_from_index_meta(_isolate):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    assert _retrieve._embedding_model_drift() == (DRIFTED, CONFIGURED)
    assert _retrieve._freeze_on_model_drift() is True


def test_no_drift_without_an_index(_isolate):
    # absent index is the DEAD state, handled elsewhere — never "drift"
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    assert _retrieve._embedding_model_drift() is None


def test_no_drift_without_configured_model_key(_isolate):
    # no calibration anchor in config → nothing to drift from
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(model=None)
    assert _retrieve._embedding_model_drift() is None


def test_detection_never_loads_an_encoder(_isolate, monkeypatch):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()

    def _boom(*a, **k):
        raise AssertionError("drift detection must not load a model")
    monkeypatch.setattr(er, "_get_model", _boom)
    assert _retrieve._embedding_model_drift() == (DRIFTED, CONFIGURED)


# ------------------------------------------------------------- lane freeze

def _exploding_cosine(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a frozen lane must never call cosine_scores")
    monkeypatch.setattr(er, "cosine_scores", _boom)


def test_tree_channel_freezes_on_drift(_isolate, monkeypatch):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    _exploding_cosine(monkeypatch)
    nodes = {"a": {"key": "a", "path": "a.md", "summary": "x"}}
    assert _retrieve._tree_embedding_scores(["sourdough"], nodes) == {}


def test_framework_channel_freezes_on_drift(_isolate, monkeypatch):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    _exploding_cosine(monkeypatch)
    entries = [{"path": ".claude/rules/x.md", "summary": "x"}]
    assert _retrieve._framework_embedding_scores(["sourdough"], entries) == {}


def test_supplementary_blend_freezes_on_drift(_isolate, monkeypatch):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    _exploding_cosine(monkeypatch)
    matched = [{"id": "guard-1", "rule": "already token-matched"}]
    active = matched + [{"id": "guard-9", "rule": "candidate"}]
    out = _retrieve._embedding_blend(matched, active, ["sourdough"])
    assert out is matched
    assert _retrieve._BLEND_STATS["supplementary_blend_status"] == "model_drift"


def test_universal_pull_slots_freeze_on_drift(_isolate, monkeypatch):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(universal_relevance_slots=2)
    _exploding_cosine(monkeypatch)
    universal = [{"id": "rb-%d" % i, "content": "u%d" % i} for i in range(9)]
    stats = {}
    out = _retrieve._universal_relevance_split(universal, ["sourdough"], stats)
    assert out == universal[:_retrieve.UNIVERSAL_RB_CAP]
    assert "model_drift" in json.dumps(stats), stats


def test_lanes_run_when_models_agree(_isolate, monkeypatch):
    # positive control for the freeze tests: same shape, no drift → the
    # lane reaches cosine_scores (stubbed to a recorder, not an explosion)
    _write_index(_isolate, CONFIGURED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    calls = []
    monkeypatch.setattr(er, "cosine_scores",
                        lambda q, index_dir=None: calls.append(q) or {})
    _retrieve._tree_embedding_scores(["sourdough"], {"a": {"key": "a"}})
    assert calls == ["sourdough"]


# ------------------------------------------------------------------ status

def test_status_reports_drift_not_alive(_isolate, monkeypatch):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    monkeypatch.setattr(er, "index_available", lambda *a, **k: True)
    v = _retrieve.embedding_channel_status()
    assert v.startswith("DRIFT:"), v
    assert DRIFTED in v and CONFIGURED in v
    assert "--update" in v


def test_status_alive_when_models_agree(_isolate, monkeypatch):
    _write_index(_isolate, CONFIGURED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    monkeypatch.setattr(er, "index_available", lambda *a, **k: True)
    assert _retrieve.embedding_channel_status() == "alive"


def test_freeze_warns_once_per_process(_isolate, capsys):
    _write_index(_isolate, DRIFTED)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    assert _retrieve._freeze_on_model_drift() is True
    assert _retrieve._freeze_on_model_drift() is True
    err = capsys.readouterr().err
    assert err.count("FROZEN") == 1, err


# ----------------------------------------------------------------- builder

def test_update_rebuilds_drifted_index_with_configured_model(tmp_path, monkeypatch):
    out = tmp_path / "idx"
    _write_index(out, DRIFTED)
    monkeypatch.setattr(_builder, "resolve_model_name", lambda *a, **k: CONFIGURED)
    monkeypatch.setattr(_builder, "_load_encoder", lambda name: "enc")
    built = []
    monkeypatch.setattr(_builder, "cmd_build",
                        lambda o, limit=None, model_override=None: built.append(o))
    rc = _builder.cmd_update(out)
    assert built == [out]
    assert not rc


def test_update_refuses_when_configured_model_cannot_load(tmp_path, monkeypatch, capsys):
    out = tmp_path / "idx"
    _write_index(out, DRIFTED)
    before = ((out / "embeddings.npy").read_bytes(), (out / "meta.json").read_bytes())
    monkeypatch.setattr(_builder, "resolve_model_name", lambda *a, **k: CONFIGURED)

    def _no_model(name):
        raise RuntimeError("model files absent on this box")
    monkeypatch.setattr(_builder, "_load_encoder", _no_model)
    monkeypatch.setattr(_builder, "cmd_build",
                        lambda *a, **k: pytest.fail("must not rebuild without the model"))
    rc = _builder.cmd_update(out)
    assert rc == 2
    after = ((out / "embeddings.npy").read_bytes(), (out / "meta.json").read_bytes())
    assert after == before, "a refused heal must leave the index untouched"
    err = capsys.readouterr().err
    assert "model_drift_unhealable" in err and CONFIGURED in err


def test_main_propagates_update_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(_builder, "cmd_update", lambda out: 2)
    monkeypatch.setattr(sys, "argv",
                        ["embedding-index-build.py", "--update", "--out", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        _builder.main()
    assert e.value.code == 2


def test_stats_reports_configured_model_and_drift(tmp_path, monkeypatch, capsys):
    out = tmp_path / "idx"
    _write_index(out, DRIFTED)
    monkeypatch.setattr(_builder, "resolve_model_name", lambda *a, **k: CONFIGURED)
    monkeypatch.setattr(_builder, "load_corpus", lambda *a, **k: [])
    import _embedding_model
    monkeypatch.setattr(_embedding_model, "load_encoder", lambda name: ("enc", "stub"))
    _builder.cmd_stats(out)
    rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rec["model"] == DRIFTED
    assert rec["configured_model"] == CONFIGURED
    assert rec["model_drift"] is True
