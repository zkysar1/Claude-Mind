"""Phase 5 (session-continuity redesign): knowledge-loss orphan classifier.

session_desync_check._classify_orphan frames each UNREGISTERED session/ file
around whether its content survives a machine-move under the own-cloud sweep:
  - data extension  -> sweep mirrors to S3 but NOT pulled on move -> info, register
  - signal-shaped   -> sweep keeps machine-local -> knowledge LOST on move -> warning
The data-extension set is the SSOT owncloud_sync._SESSION_DATA_EXTS.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import session_desync_check as _mod  # noqa: E402


def test_classify_orphan_data_ext_is_info_with_register_hint():
    sev, dclass, desc = _mod._classify_orphan("notes.jsonl", 4096)
    assert sev == "info" and dclass == "data"
    assert "continuity" in desc and "S3" in desc and "4096B" in desc


def test_classify_orphan_signal_extensionless_is_warning():
    sev, dclass, desc = _mod._classify_orphan("some-marker", 12)
    assert sev == "warning" and dclass == "signal"
    assert "MACHINE-LOCAL" in desc and "LOST" in desc


def test_classify_orphan_unknown_ext_is_warning():
    sev, dclass, desc = _mod._classify_orphan("blob.bin", 99)
    assert sev == "warning" and dclass == "signal"


def test_classify_orphan_size_unknown_renders_gracefully():
    sev, dclass, desc = _mod._classify_orphan("x.yaml", None)
    assert sev == "info" and "unknown size" in desc


def test_classify_orphan_fallback_when_exts_unavailable(monkeypatch):
    # If _SESSION_DATA_EXTS failed to import, every orphan -> higher-risk signal.
    monkeypatch.setattr(_mod, "_SESSION_DATA_EXTS", None)
    sev, dclass, desc = _mod._classify_orphan("notes.jsonl", 4096)
    assert sev == "warning" and dclass == "signal"


def test_session_data_exts_ssot_wired():
    # The SSOT import succeeded and carries the common session data extensions.
    assert _mod._SESSION_DATA_EXTS is not None
    for ext in (".yaml", ".yml", ".json", ".jsonl", ".txt", ".md", ".csv", ".tsv"):
        assert ext in _mod._SESSION_DATA_EXTS
    # SSOT contract: it must be the SAME object as owncloud_sync's, not a local
    # copy — so future additions there propagate without a second edit.
    import owncloud_sync  # noqa: E402
    assert _mod._SESSION_DATA_EXTS is owncloud_sync._SESSION_DATA_EXTS
