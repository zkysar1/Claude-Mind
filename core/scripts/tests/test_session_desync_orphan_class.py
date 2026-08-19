"""Phase 5 (session-continuity redesign): knowledge-loss orphan classifier.

session_desync_check._classify_orphan frames each UNREGISTERED session/ file
around whether its content survives a machine-move under the own-cloud sweep:
  - data extension  -> sweep MIRRORS to S3, but registration is what makes a
    session file eligible for the both-diverged LOCAL-WINS auto-resolve, so an
    unregistered one can wedge PERMANENTLY -> warning, register
  - signal-shaped   -> sweep keeps machine-local -> knowledge LOST on move -> warning
The data-extension set is the SSOT owncloud_sync._SESSION_DATA_EXTS.

SEVERITY CONTRACT (g-115-6352, 2026-08-16): both branches are now 'warning'.
The data branch was 'info' until the severities were found INVERTED relative to
consequence — the signal branch is recoverable (the file stays local and is
regenerated) while the data branch has no healing path below it. Severity
therefore no longer discriminates the two branches; `data_class` and the
message do, which is why the data test pins the wedge language explicitly.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import session_desync_check as _mod  # noqa: E402


def test_classify_orphan_data_ext_is_warning_with_register_hint():
    sev, dclass, desc = _mod._classify_orphan("notes.jsonl", 4096)
    assert sev == "warning" and dclass == "data"
    assert "continuity" in desc and "S3" in desc and "4096B" in desc
    # Severity alone no longer separates the branches, so pin the consequence
    # that motivated the promotion: an unregistered data file is INELIGIBLE for
    # the auto-resolve and therefore wedges permanently rather than transiently.
    assert "PERMANENT WEDGE" in desc
    assert "ephemeral" in desc and "machine_local" in desc


def test_classify_orphan_signal_extensionless_is_warning():
    sev, dclass, desc = _mod._classify_orphan("some-marker", 12)
    assert sev == "warning" and dclass == "signal"
    assert "MACHINE-LOCAL" in desc and "LOST" in desc


def test_classify_orphan_unknown_ext_is_warning():
    sev, dclass, desc = _mod._classify_orphan("blob.bin", 99)
    assert sev == "warning" and dclass == "signal"


def test_classify_orphan_size_unknown_renders_gracefully():
    sev, dclass, desc = _mod._classify_orphan("x.yaml", None)
    assert sev == "warning" and "unknown size" in desc


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
