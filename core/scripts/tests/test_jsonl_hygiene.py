"""Tests for core/scripts/jsonl_hygiene.py ( KEYSTONE).

Covers both modes (cap / rotate) x both policies (lines / age), dry-run
semantics, no-op-below-bound, absent file, malformed-line handling, unknown
mode, and the registry sweep (including that the shipped all-disabled
store-hygiene.yaml is a safe no-op even with --apply)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/scripts
import jsonl_hygiene as jh  # noqa: E402


def _write(p, records):
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _read(p):
    return [json.loads(ln) for ln in
            Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── cap mode (atomic, no archive) ───────────────────────────────────────────
def test_cap_lines_over_threshold(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="cap", by="lines", max_lines=4, apply=True)
    assert rep["action"] == "capped"
    assert rep["dropped"] == 6
    assert rep["kept"] == 4
    assert [r["i"] for r in _read(p)] == [6, 7, 8, 9]      # newest 4 kept
    assert not (tmp_path / "log-archive.jsonl").exists()   # cap never archives


def test_cap_lines_under_threshold_noop(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(3)])
    rep = jh.hygiene_one(p, mode="cap", by="lines", max_lines=10, apply=True)
    assert rep["action"] == "within-bound"
    assert rep["dropped"] == 0
    assert [r["i"] for r in _read(p)] == [0, 1, 2]


# ── rotate mode (archive-first, then drop) ──────────────────────────────────
def test_rotate_lines_archives_oldest(tmp_path):
    p = tmp_path / "journal.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="rotate", by="lines", max_lines=4, apply=True)
    assert rep["action"] == "rotated"
    assert rep["dropped"] == 6
    assert [r["i"] for r in _read(p)] == [6, 7, 8, 9]
    assert [r["i"] for r in _read(tmp_path / "journal-archive.jsonl")] == \
        [0, 1, 2, 3, 4, 5]


def test_rotate_appends_to_existing_archive(tmp_path):
    p = tmp_path / "j.jsonl"
    arch = tmp_path / "j-archive.jsonl"
    _write(arch, [{"old": True}])
    _write(p, [{"i": i} for i in range(6)])
    jh.hygiene_one(p, mode="rotate", by="lines", max_lines=2, apply=True)
    archived = _read(arch)
    assert archived[0] == {"old": True}                    # pre-existing kept
    assert [r.get("i") for r in archived[1:]] == [0, 1, 2, 3]   # appended oldest
    assert [r["i"] for r in _read(p)] == [4, 5]


def test_rotate_custom_archive_path(tmp_path):
    p = tmp_path / "log.jsonl"
    custom = tmp_path / "sink.jsonl"
    _write(p, [{"i": i} for i in range(5)])
    jh.hygiene_one(p, mode="rotate", by="lines", max_lines=2,
                   archive_path=str(custom), apply=True)
    assert custom.exists()
    assert not (tmp_path / "log-archive.jsonl").exists()
    assert [r["i"] for r in _read(custom)] == [0, 1, 2]


# ── dry-run (no writes) ─────────────────────────────────────────────────────
def test_dry_run_no_writes(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="rotate", by="lines", max_lines=4, apply=False)
    assert rep["action"] == "would-rotate"
    assert rep["dropped"] == 6 and rep["applied"] is False
    assert len(_read(p)) == 10                             # unchanged
    assert not (tmp_path / "log-archive.jsonl").exists()


def test_dry_run_cap(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="cap", by="lines", max_lines=4, apply=False)
    assert rep["action"] == "would-cap"
    assert len(_read(p)) == 10


# ── age policy ──────────────────────────────────────────────────────────────
_OLD = "2020-01-01T00:00:00"
_NEW = "2099-01-01T00:00:00"


def test_cap_age(tmp_path):
    p = tmp_path / "ts.jsonl"
    _write(p, [{"t": _OLD}, {"t": _OLD}, {"t": _NEW}, {"t": _NEW}])
    rep = jh.hygiene_one(p, mode="cap", by="age", retention_days=30,
                         ts_field="t", apply=True)
    assert rep["dropped"] == 2
    assert [r["t"] for r in _read(p)] == [_NEW, _NEW]


def test_rotate_age_archives_old(tmp_path):
    p = tmp_path / "ts.jsonl"
    _write(p, [{"t": _OLD}, {"t": _OLD}, {"t": _NEW}])
    rep = jh.hygiene_one(p, mode="rotate", by="age", retention_days=30,
                         ts_field="t", apply=True)
    assert rep["dropped"] == 2
    assert [r["t"] for r in _read(p)] == [_NEW]
    assert [r["t"] for r in _read(tmp_path / "ts-archive.jsonl")] == [_OLD, _OLD]


def test_age_keeps_undateable_leading_record(tmp_path):
    # by=age stops at the first record that is not parseably-old, so an
    # undateable leading record conservatively halts the drop (never dropped).
    p = tmp_path / "ts.jsonl"
    _write(p, [{"t": "not-a-date"}, {"t": _OLD}])
    rep = jh.hygiene_one(p, mode="cap", by="age", retention_days=30,
                         ts_field="t", apply=True)
    assert rep["dropped"] == 0
    assert len(_read(p)) == 2


# ── edge cases ──────────────────────────────────────────────────────────────
def test_absent_file_noop(tmp_path):
    rep = jh.hygiene_one(tmp_path / "missing.jsonl", mode="cap", by="lines",
                         max_lines=5, apply=True)
    assert rep["action"] == "absent-or-empty"
    assert rep["dropped"] == 0


def test_rotate_skips_malformed_line(tmp_path):
    # A malformed line is skipped by the canonical reader (consistent on both
    # the snapshot and the in-lock read), so rotation operates on the parseable
    # subset and does not abort. The malformed line is dropped on rewrite
    # (framework behavior; original bytes survive in .history).
    p = tmp_path / "log.jsonl"
    p.write_text('{"i": 0}\nNOT_JSON\n{"i": 2}\n{"i": 3}\n', encoding="utf-8")
    rep = jh.hygiene_one(p, mode="rotate", by="lines", max_lines=2, apply=True)
    assert rep["dropped"] == 1
    assert [r["i"] for r in _read(p)] == [2, 3]
    assert [r["i"] for r in _read(tmp_path / "log-archive.jsonl")] == [0]
    assert "NOT_JSON" not in p.read_text(encoding="utf-8")


def test_unknown_mode_returns_error(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": 0}])
    rep = jh.hygiene_one(p, mode="bogus", by="lines", max_lines=1, apply=True)
    assert "error" in rep and "unknown mode" in rep["error"]
    assert len(_read(p)) == 1                              # untouched


# ── registry sweep ──────────────────────────────────────────────────────────
def test_sweep_shipped_registry_all_disabled_is_safe(tmp_path):
    # The shipped store-hygiene.yaml ships every entry disabled. Sweeping it
    # (even with --apply) must touch nothing -> every report is "disabled".
    result = jh.sweep(apply=True)
    assert result["swept"] >= 1
    actions = {r.get("action") for r in result["reports"]}
    assert actions <= {"disabled"}, f"unexpected sweep actions: {actions}"


def test_sweep_enabled_entry_processed(tmp_path, monkeypatch):
    store = tmp_path / "s.jsonl"
    _write(store, [{"i": i} for i in range(8)])
    reg = {
        "version": 1,
        "defaults": {"enabled": False, "mode": "cap", "by": "lines"},
        "stores": [
            {"path": str(store), "enabled": True, "mode": "cap", "by": "lines",
             "max_lines": 3, "owner_goal": "g-test"},
            {"path": str(tmp_path / "other.jsonl"), "enabled": False,
             "mode": "cap", "by": "lines", "max_lines": 3},
        ],
    }
    monkeypatch.setattr(jh, "_load_registry", lambda: reg)
    result = jh.sweep(apply=True)
    assert result["swept"] == 2
    by_action = [r.get("action") for r in result["reports"]]
    assert "capped" in by_action and "disabled" in by_action
    assert [r["i"] for r in _read(store)] == [5, 6, 7]
