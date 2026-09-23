"""test_precompact_checkpoint_libyaml.py —  regression.

The PreCompact hook precompact-checkpoint.sh has a 10 s budget (.claude/settings.json)
and ran 12.9-13.5 s on zc-01 (2026-09-23, n=5, production env and payload), so zakcode
killed it at 13 of the alpha Body's 14 compactions that day and no checkpoint was
written. cProfile put all of it in pure-Python YAML over a 12.5 MB working memory:
read_wm(), the read of the previous checkpoint (a full WM copy), and the dump.

These tests pin the fix: both reads go through wm.read_yaml on libyaml's C loader, and
the dump uses the C dumper, with data identical to the pure-Python path.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

os.environ.setdefault("MIND_AGENT", "echo")

import wm  # noqa: E402

LIBYAML = bool(getattr(yaml, "__with_libyaml__", False))


def _load_pcc(name):
    spec = importlib.util.spec_from_file_location(name, CORE_SCRIPTS / "precompact-checkpoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wm_doc():
    return {
        "session_id": "sid-g37503",
        "encoding_queue": [{"id": "e1", "text": "héllo — ünïcode"}],
        "slots": {
            "active_context": {"retrieval_manifest": {"tree": ["a", "b"]}},
            "spark_capture": [{"goal_id": "g-1-1", "fact": "x" * 50, "at": "2026-09-23T10:00:00"}],
            "known_blockers": [],
        },
        "slot_meta": {"spark_capture": {"updated": "2026-09-23"}},
    }


@pytest.mark.skipif(not LIBYAML, reason="this PyYAML has no libyaml binding")
def test_wm_reads_with_the_c_loader_when_pyyaml_has_one():
    assert wm._SAFE_LOADER is yaml.CSafeLoader


def test_read_yaml_parses_through_the_module_loader(tmp_path, monkeypatch):
    """read_yaml must honour wm._SAFE_LOADER; a bare yaml.safe_load would bypass it."""
    used = []

    class Spy(wm._SAFE_LOADER):
        def __init__(self, *a, **k):
            used.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(wm, "_SAFE_LOADER", Spy)
    path = tmp_path / "wm.yaml"
    path.write_text(yaml.safe_dump(_wm_doc(), allow_unicode=True), encoding="utf-8")

    assert wm.read_yaml(path) == yaml.safe_load(path.read_text(encoding="utf-8"))
    assert used, "read_yaml did not parse through wm._SAFE_LOADER"


def test_checkpoint_dumps_safely_and_reads_back_the_same(tmp_path, monkeypatch):
    pcc = _load_pcc("pcc_g37503")
    # Patch the wm module pcc actually bound, never this file's collection-time
    # `wm` (guard-1415). The test_compact_restore_* files pop "wm" from
    # sys.modules and re-import it, so after them the two are different objects:
    # patching ours left read_wm on the real wm_path and the spy at 0 reads.
    bound = pcc.read_wm.__globals__
    wm_file = tmp_path / "working-memory.yaml"
    wm_file.write_text(yaml.safe_dump(_wm_doc(), allow_unicode=True), encoding="utf-8")
    ckpt = tmp_path / "compact-checkpoint.yaml"
    monkeypatch.setitem(bound, "wm_path", lambda: wm_file)
    monkeypatch.setattr(pcc, "WM_PATH", wm_file)
    monkeypatch.setattr(pcc, "CHECKPOINT_PATH", ckpt)

    dumpers = []
    real_dump = yaml.dump

    def spy_dump(data, stream=None, **kw):
        dumpers.append(kw.get("Dumper"))
        return real_dump(data, stream, **kw)

    monkeypatch.setattr(pcc.yaml, "dump", spy_dump)
    loads = []

    class SpyLoader(bound["_SAFE_LOADER"]):
        def __init__(self, *a, **k):
            loads.append(1)
            super().__init__(*a, **k)

    monkeypatch.setitem(bound, "_SAFE_LOADER", SpyLoader)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    pcc.main()
    first = yaml.safe_load(ckpt.read_text(encoding="utf-8"))
    pcc.main()
    second = yaml.safe_load(ckpt.read_text(encoding="utf-8"))

    expected = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
    assert dumpers == [expected, expected], dumpers
    # Run 1 read the WM (no checkpoint yet); run 2 read the WM and the checkpoint.
    assert len(loads) == 3, f"{len(loads)} reads through wm._SAFE_LOADER, expected 3"
    assert first["all_slots"] == _wm_doc()["slots"]
    assert first["encoding_queue"] == _wm_doc()["encoding_queue"]
    # The second run read the first checkpoint back through read_yaml.
    assert (first["compact_count"], second["compact_count"]) == (1, 2)
    assert second["prior_encoding_items"] == _wm_doc()["encoding_queue"]
