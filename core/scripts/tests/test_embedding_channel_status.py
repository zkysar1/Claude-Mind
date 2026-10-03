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
import _vendor_path as vp  # noqa: E402

_REAL_STACK_PROBE = vp.stack_absent_reason


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
def _stack_present(monkeypatch):
    # The verdicts in the first half of this file are about flags, the index and
    # drift. Pin the stack probe to "present" so they never depend on what THIS
    # box has installed; the  tests below put the real probe back or
    # replace it.
    monkeypatch.setattr(vp, "stack_absent_reason", lambda: None)


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


# --- : the verdict describes the SERVING PROCESS --------------------

def _flags_on_with_index(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(blend=True, tree=True)
    monkeypatch.setattr(er, "index_available", lambda *a, **k: True)


def test_dead_when_this_process_cannot_import_the_stack(monkeypatch):
    """Flags on plus an index file read 'alive' while a daemon that started
    before the stack was installed served every query token-only (ZDS cc-12,
    2026-09-30)."""
    _flags_on_with_index(monkeypatch)
    monkeypatch.setattr(vp, "stack_absent_reason", lambda: "numpy is not importable")
    v = _retrieve.embedding_channel_status()
    assert v.startswith("DEAD"), v
    assert "numpy is not importable" in v                    # the cause
    assert "pip install --target ~/.ayoai-vendor/py" in v    # the remedy


def test_stack_probe_runs_before_the_index_is_loaded(monkeypatch):
    """The drift read loads the index (numpy) inside a swallow-all, so for a
    process that cannot import numpy it answers 'no drift' and the verdict would
    fall through to alive. A recorder, not a raising stub: the swallow-all would
    eat the raise and the test would pass whatever the order."""
    _flags_on_with_index(monkeypatch)
    monkeypatch.setattr(vp, "stack_absent_reason", lambda: "numpy is not importable")
    loads = []
    monkeypatch.setattr(er, "_load_index", lambda d: loads.append(d) or (None, None, None))
    assert _retrieve.embedding_channel_status().startswith("DEAD")
    assert loads == []


def test_dead_when_the_last_scoring_attempt_degraded(monkeypatch):
    _flags_on_with_index(monkeypatch)
    monkeypatch.setattr(er, "_last_degradation",
                        {"reason": "encoder-or-runtime-error",
                         "detail": "OSError: model files absent"})
    v = _retrieve.embedding_channel_status()
    assert v.startswith("DEAD"), v
    assert "encoder-or-runtime-error" in v and "model files absent" in v


def test_an_empty_query_record_is_not_evidence(monkeypatch):
    """cosine_scores records an empty query (a no-op) without warning on it; the
    status must not call the box dead because one caller passed ''."""
    _flags_on_with_index(monkeypatch)
    monkeypatch.setattr(er, "_last_degradation", {"reason": "empty-query", "detail": ""})
    assert _retrieve.embedding_channel_status() == "alive"


def test_alive_again_once_a_scoring_call_serves(monkeypatch):
    """None is what a served cosine_scores leaves behind."""
    _flags_on_with_index(monkeypatch)
    monkeypatch.setattr(er, "_last_degradation", {"reason": "index-absent", "detail": "x"})
    assert _retrieve.embedding_channel_status().startswith("DEAD")
    monkeypatch.setattr(er, "_last_degradation", None)
    assert _retrieve.embedding_channel_status() == "alive"


def test_stack_installed_after_start_flips_dead_to_alive_without_restart(tmp_path, monkeypatch):
    """The goal's outcome B through the REAL probe: a process that started with
    no vendor dir reads DEAD; the stack appears; the next call reads alive, in
    the same process. Unique stand-in module names keep the real numpy (already
    loaded in this process) out of it."""
    _flags_on_with_index(monkeypatch)
    monkeypatch.setattr(vp, "stack_absent_reason", _REAL_STACK_PROBE)
    monkeypatch.setattr(vp, "_STACK_MODULES", ("_g306574_np",))
    monkeypatch.setattr(vp, "_ENCODER_BACKENDS", ("_g306574_enc",))
    monkeypatch.setattr(sys, "path", list(sys.path))   # ensure_vendor_path appends to it
    vendor = tmp_path / "py"                            # does not exist yet
    monkeypatch.setenv("MIND_VENDOR_DIR", str(vendor))
    assert _retrieve.embedding_channel_status().startswith("DEAD")
    for name in ("_g306574_np", "_g306574_enc"):
        (vendor / name).mkdir(parents=True)
        (vendor / name / "__init__.py").write_text("", encoding="utf-8")
    assert _retrieve.embedding_channel_status() == "alive"
