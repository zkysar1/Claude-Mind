#!/usr/bin/env python3
"""test_override_ledger_consume.py —  regression test.

Pins the g-115-2790 fixes to override-ledger-consume.py:

  (1) The per-gate cross-store ratio is emitted as `ledger_to_firings_ratio`
      (renamed from the misleading `override_rate`). It is a ledger-vs-firings
      AUDIT-INTEGRITY signal, NOT a bounded fraction — it CAN exceed 1.0, and
      is None when the firings denominator is 0. The bounded override fraction
      that the g-115-603 tighten trigger uses lives in gate-retirement-eval.py /
      gate-stats.py; this analyzer must not re-expose a field that looks like it.

  (2) REASON_TAG_PATTERNS tags the real ledger justification clusters
      (insight-trigger-conversion, arc-planning, stale-claim-takeback,
      handoff-routing, test-fixture, tree-contradiction-audit) instead of
      dropping them to "untagged" (the live ledger was ~95% untagged because
      the dominant real reason had no pattern — not because justifications were
      absent).

Run (pytest-collected — picked up by the daemon-safe full suite):
    STORAGE_BACKEND=local python -m pytest \
        core/scripts/tests/test_override_ledger_consume.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_MODPATH = SCRIPT_DIR / "override-ledger-consume.py"


def _load_module():
    """Load the hyphen-named script as an importable module object."""
    spec = importlib.util.spec_from_file_location(
        "override_ledger_consume_under_test", _MODPATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_jsonl(path: Path, records):
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _recent_ts(days_ago: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def test_tag_record_covers_real_clusters():
    """The dominant live justification clusters must tag, not fall to untagged."""
    mod = _load_module()
    cases = {
        "insight-trigger conversion (sweep): msg-20260719-215837-alp": "insight-trigger-conversion",
        "insight-trigger conversion (manual sweep run)": "insight-trigger-conversion",
        "validated arc task/action boundary plan (draft)": "arc-planning",
        "stale-claim take-back: prior claim by alpha": "stale-claim-takeback",
        "zeta intended_agent stale 6d (last_active ...)": "stale-claim-takeback",
        "zeta dormant 203h >> reallocation 8h": "stale-claim-takeback",
        "handoff_to=foxtrot set by author bravo 54s ago": "handoff-routing",
        "Tree contradiction audit (g-115-399 mandate)": "tree-contradiction-audit",
        "test-fixture": "test-fixture",
        "test justification": "test-fixture",
    }
    for justification, expected_tag in cases.items():
        tags = mod._tag_record({"justification": justification})
        assert expected_tag in tags, f"{justification!r} -> {tags}, expected {expected_tag}"
        assert "untagged" not in tags, f"{justification!r} wrongly untagged: {tags}"


def test_tag_record_untagged_fallback_unchanged():
    """Empty/absent justification and genuinely-novel reasons still -> untagged."""
    mod = _load_module()
    assert mod._tag_record({}) == ["untagged"]
    assert mod._tag_record({"justification": ""}) == ["untagged"]
    assert mod._tag_record(
        {"justification": "an entirely novel reason matching no pattern"}
    ) == ["untagged"]


def test_ledger_to_firings_ratio_field_rename_and_unbounded(tmp_path, monkeypatch):
    """per_gate emits `ledger_to_firings_ratio` (not `override_rate`); ratio may
    exceed 1.0 and is None when the firings denominator is 0."""
    mod = _load_module()
    ledger = tmp_path / "override-bypass-ledger.jsonl"
    firings = tmp_path / "gate-firings.jsonl"
    ts = _recent_ts(1)
    # gate-A: 3 ledger records, 1 firing override -> ratio 3.0 (>1, must survive)
    # gate-B: 1 ledger record, 0 firing override -> ratio None
    _write_jsonl(ledger, [
        {"ts": ts, "gate": "gate-A", "agent": "zeta",
         "justification": "insight-trigger conversion (sweep): x"},
        {"ts": ts, "gate": "gate-A", "agent": "zeta",
         "justification": "insight-trigger conversion (sweep): y"},
        {"ts": ts, "gate": "gate-A", "agent": "zeta", "justification": "test-fixture"},
        {"ts": ts, "gate": "gate-B", "agent": "zeta",
         "justification": "handoff_to=foxtrot set by author"},
    ])
    _write_jsonl(firings, [
        {"ts": ts, "gate_id": "gate-A", "decision": "override"},
        {"ts": ts, "gate_id": "gate-A", "decision": "block"},
        {"ts": ts, "gate_id": "gate-B", "decision": "pass"},
    ])
    monkeypatch.setattr(mod, "LEDGER_JSONL", ledger)
    monkeypatch.setattr(mod, "FIRINGS_JSONL", firings)

    result = mod.analyze(days=30, gate_filter=None, top_k=10)
    pg = result["per_gate"]
    assert "override_rate" not in pg["gate-A"], "old field name must be gone"
    assert pg["gate-A"]["ledger_to_firings_ratio"] == 3.0, pg["gate-A"]
    assert pg["gate-B"]["ledger_to_firings_ratio"] is None, pg["gate-B"]

    # Tag coverage flows into top_reason_clusters (the point of fix #2).
    tags = {c["tag"] for c in result["top_reason_clusters"]}
    assert "insight-trigger-conversion" in tags
    assert "handoff-routing" in tags


def test_human_render_labels_ratio_not_percent(tmp_path, monkeypatch):
    """render_human shows a plain ratio ('ledger/firings ratio=3.00'), never a
    misleading percentage for a value that can exceed 100%."""
    mod = _load_module()
    ledger = tmp_path / "override-bypass-ledger.jsonl"
    firings = tmp_path / "gate-firings.jsonl"
    ts = _recent_ts(1)
    _write_jsonl(ledger, [
        {"ts": ts, "gate": "gate-A", "agent": "zeta", "justification": "test-fixture"},
        {"ts": ts, "gate": "gate-A", "agent": "zeta", "justification": "test-fixture"},
        {"ts": ts, "gate": "gate-A", "agent": "zeta", "justification": "test-fixture"},
    ])
    _write_jsonl(firings, [{"ts": ts, "gate_id": "gate-A", "decision": "override"}])
    monkeypatch.setattr(mod, "LEDGER_JSONL", ledger)
    monkeypatch.setattr(mod, "FIRINGS_JSONL", firings)

    text = mod.render_human(mod.analyze(days=30, gate_filter=None, top_k=10))
    assert "ledger/firings ratio=3.00" in text, text
    assert "rate=300" not in text and "300.0%" not in text, "must not render as a percent"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
