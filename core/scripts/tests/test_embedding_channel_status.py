"""test_embedding_channel_status.py — unit tests for
retrieve.embedding_channel_status() (the per-request semantic-channel health
value surfaced as meta.embedding_channel) and for the --stats absent-index
channel verdict in embedding-index-build.py.

The load-bearing invariants:

  1. NEVER SILENT IN THE DEGRADED STATE — fleet flags ON with no per-box
     index is exactly how cc-13 served token-only retrieval unreported
     (2026-08-21: 5/7 known-target paraphrase misses). That state must
     return "DEAD: ..." naming the build command, and --stats on an absent
     index must carry channel="DEAD" rather than omitting the key.
  2. CHEAP — the status is a flag read + index-file stat. It must never
     load the model (a degraded sentence-transformers box pays ~28s per
     load, g-115-3577); proven with an exploding _get_model stub.
  3. "off" is reserved for deliberately-disabled flags — not degradation.

Same bootstrap as test_embedding_blend.py: retrieve.py imported via
importlib against a scratch MIND_WORLD.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="embedding-status-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_embstatus_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import _embedding_retrieval as er  # noqa: E402


def _cfg(blend=False, tree=False):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg["embedding_blend_enabled"] = blend
    cfg["embedding_tree_channel_enabled"] = tree
    return cfg


@pytest.fixture(autouse=True)
def _hermetic_index_dir(tmp_path, monkeypatch):
    # 2026-09-03: embedding_channel_status() also reads the per-box index's
    # model name (the index-vs-calibration drift check). Pin the index dir
    # to an empty tmp so these verdicts never depend on whatever index THIS
    # box happens to carry (test_embedding_model_drift.py owns the DRIFT case).
    monkeypatch.setenv(er._INDEX_DIR_ENV, str(tmp_path))
    er.clear_caches()
    yield
    er.clear_caches()


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


def test_off_when_both_flags_false():
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(blend=False, tree=False)
    assert _retrieve.embedding_channel_status() == "off"


def test_dead_when_flags_on_and_no_index(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(blend=True, tree=False)
    monkeypatch.setattr(er, "index_available", lambda *a, **k: False)
    v = _retrieve.embedding_channel_status()
    assert v.startswith("DEAD"), v
    # The message must name the remedy, not just the state.
    assert "embedding-index-build.py --build" in v


def test_dead_fires_for_either_flag(monkeypatch):
    monkeypatch.setattr(er, "index_available", lambda *a, **k: False)
    for kwargs in ({"blend": True}, {"tree": True}):
        _retrieve._RETRIEVAL_CFG_CACHE = _cfg(**kwargs)
        assert _retrieve.embedding_channel_status().startswith("DEAD")


def test_alive_when_index_present(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(blend=True, tree=True)
    monkeypatch.setattr(er, "index_available", lambda *a, **k: True)
    assert _retrieve.embedding_channel_status() == "alive"


def test_status_never_loads_model(monkeypatch):
    """Invariant 2: the status probe is index_available only — an exploding
    encoder must be unreachable from it."""
    def _boom(*a, **k):
        raise AssertionError("status probe must never load the model")
    monkeypatch.setattr(er, "_get_model", _boom)
    monkeypatch.setattr(er, "cosine_scores", _boom)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(blend=True, tree=True)
    monkeypatch.setattr(er, "index_available", lambda *a, **k: True)
    assert _retrieve.embedding_channel_status() == "alive"


def test_stats_absent_index_reports_dead_channel(tmp_path):
    """--stats on a box with no index must still carry the channel verdict
    (channel=DEAD + reason), never omit the key — the pre-2026-08-21 shape
    was {'op','exists','out'} only, silent in exactly the degraded state."""
    r = subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "embedding-index-build.py"),
         "--stats", "--out", str(tmp_path / "no-index-here")],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["exists"] is False
    assert d["channel"] == "DEAD"
    assert "index absent" in d.get("channel_reason", "")
