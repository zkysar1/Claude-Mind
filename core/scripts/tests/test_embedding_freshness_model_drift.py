"""test_embedding_freshness_model_drift.py — the freshness tick treats a
MODEL-DRIFTED index as stale in its own right (cdb3288607, 2026-09-03).


WHY. `embedding-index-build.py --update` self-heals drift by rebuilding on
`tree.yaml embedding_model_name`, and `retrieve.py` freezes the embedding
lanes while an index names any other model. Both are useless on a QUIET box
if nothing ever spawns the update: the tick's only trigger was
"newest source mtime > meta.json mtime", which a drifted-but-fresh index
never satisfies. The freeze would then be permanent on exactly the boxes
that write least.

The pairing this file pins:
  1. drifted + fresh sources -> would_spawn, reason=model_drift
  2. undrifted + fresh sources -> silent (the negative control; without it
     test 1 passes on a tick that fires unconditionally)
  3. drift respects the debounce (a refusing update must not storm)
  4. every fail-quiet path returns "not drifted" — an unreadable meta, a
     meta with no model, a config with no embedding_model_name. A tick may
     never manufacture a rebuild out of a missing signal.
  5. `_blend_enabled` stays ZERO-ARG (14 sibling tests monkeypatch it as
     `lambda: True`; a signature change would break them at the call site).

COLLECTION-SAFETY: importlib-loads the hyphenated module, redirects the index
dir with EMBED_FRESHNESS_INDEX_DIR, forces EMBED_FRESHNESS_DRYRUN=1 so no
subprocess spawns, and monkeypatches the config + source-mtime readers — no
YAML read, no world access, no live index touched.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

_spec = importlib.util.spec_from_file_location(
    "embedding_index_freshness_drift", CORE_SCRIPTS / "embedding-index-freshness.py")
fresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fresh)

CONFIGURED = "all-MiniLM-L6-v2"
OTHER = "BAAI/bge-small-en-v1.5"


@pytest.fixture()
def tick(tmp_path, monkeypatch, capsys):
    """Dry-run tick over a redirected index dir. Returns (idx, run, set_cfg)."""
    idx = tmp_path / "index"
    idx.mkdir()
    monkeypatch.setenv("EMBED_FRESHNESS_DRYRUN", "1")
    monkeypatch.setenv("EMBED_FRESHNESS_INDEX_DIR", str(idx))
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    # Sources two hours OLDER than the index: the mtime trigger is OFF, so any
    # firing below is attributable to the drift check alone.
    monkeypatch.setattr(fresh, "_source_mtime", lambda: time.time() - 7200)

    def set_cfg(**kw):
        cfg = {"embedding_blend_enabled": True, "embedding_model_name": CONFIGURED}
        cfg.update(kw)
        monkeypatch.setattr(fresh, "_retrieval_cfg", lambda: cfg)
        return cfg

    set_cfg()

    def run():
        rc = fresh.main()
        out = capsys.readouterr().out.strip()
        return rc, ([json.loads(line) for line in out.splitlines()] if out else [])

    return idx, run, set_cfg


def _write_meta(idx, model, age_seconds=0):
    meta = idx / "meta.json"
    body = {} if model is None else {"model": model}
    meta.write_text(json.dumps(body), encoding="utf-8")
    t = time.time() - age_seconds
    os.utime(meta, (t, t))
    return meta


def test_drifted_index_fires_even_when_sources_are_older(tick):
    idx, run, _ = tick
    _write_meta(idx, OTHER)
    rc, out = run()
    assert rc == 0
    assert out and out[0]["would_spawn"] is True
    assert out[0]["reason"] == "model_drift"
    assert (idx / ".last-update-attempt").exists()


def test_matching_model_with_older_sources_stays_silent(tick):
    """Negative control for the test above — without it, a tick that fires
    unconditionally would pass."""
    idx, run, _ = tick
    _write_meta(idx, CONFIGURED)
    rc, out = run()
    assert rc == 0 and out == []
    assert not (idx / ".last-update-attempt").exists()


def test_stale_sources_still_report_their_own_reason(tick, monkeypatch):
    idx, run, _ = tick
    _write_meta(idx, CONFIGURED, age_seconds=7200)
    monkeypatch.setattr(fresh, "_source_mtime", time.time)
    rc, out = run()
    assert rc == 0
    assert out and out[0]["reason"] == "stale_sources"


def test_drift_respects_the_debounce(tick):
    """A configured model that cannot load makes --update refuse (rc=2) with
    the index untouched, so the drift stays true. The debounce is what keeps
    that from spawning on every iteration close."""
    idx, run, _ = tick
    _write_meta(idx, OTHER)
    rc, out = run()
    assert out and out[0]["reason"] == "model_drift"
    rc2, out2 = run()
    assert rc2 == 0 and out2 == [], "second fire inside the window must be suppressed"


@pytest.mark.parametrize("meta_body,cfg_kw", [
    ("not json at all", {}),                       # unreadable meta
    (None, {}),                                    # meta with no model key
    (OTHER, {"embedding_model_name": None}),       # config names no model
])
def test_missing_signals_never_manufacture_a_rebuild(tick, meta_body, cfg_kw):
    idx, run, set_cfg = tick
    set_cfg(**cfg_kw)
    if meta_body == "not json at all":
        meta = idx / "meta.json"
        meta.write_text("{{{ not json", encoding="utf-8")
        t = time.time()
        os.utime(meta, (t, t))
    else:
        _write_meta(idx, meta_body)
    rc, out = run()
    assert rc == 0 and out == [], "a missing signal must read as 'not drifted'"


def test_model_drifted_is_directly_callable_without_a_cfg(tick):
    """The helper resolves its own config when the caller passes none — the
    shape main() uses."""
    idx, _, set_cfg = tick
    set_cfg()
    meta = _write_meta(idx, OTHER)
    assert fresh._model_drifted(meta) is True
    meta = _write_meta(idx, CONFIGURED)
    assert fresh._model_drifted(meta) is False


def test_blend_enabled_stays_zero_arg():
    """14 sibling tests monkeypatch this as `lambda: True`. If it grows a
    required parameter, they fail at the call rather than at an assertion,
    which reads as an unrelated breakage."""
    params = inspect.signature(fresh._blend_enabled).parameters
    required = [p for p in params.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    assert required == [], f"_blend_enabled must stay callable with no args: {params}"


def test_config_read_is_memoized(monkeypatch):
    """One parse serves both the flag read and the model read."""
    fresh._CFG_CACHE.clear()
    calls = []
    real_read_text = Path.read_text

    def counting_read_text(self, *a, **kw):
        if self.name == "tree.yaml":
            calls.append(str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    try:
        fresh._retrieval_cfg()
        fresh._retrieval_cfg()
        fresh._retrieval_cfg()
        assert len(calls) <= 1, f"tree.yaml parsed {len(calls)} times, expected at most 1"
    finally:
        fresh._CFG_CACHE.clear()
