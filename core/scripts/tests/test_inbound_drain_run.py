#!/usr/bin/env python3
"""Tests for core/scripts/inbound-drain-run.py — the `inbound-drain` hook-slot
audited entry point (g-369-150).

WHY THIS FILE EXISTS. The refactor that unified the two competing drains
(b67456bbb, "one inbound drain, wired through a hook slot") DELETED
test_sidecar_inbound_drain.py (247 lines) and shipped this runner with none.
The runner is the AUDIT LAYER — its own docstring says the sibling slot
"rotted (g-115-4879) precisely because a caller invoked the collector directly
and skipped the audit layer, leaving a healthy output concealing a dead hook."
An untested audit layer is exactly the thing that can stop auditing silently,
so the invariants below are the ones whose failure is INVISIBLE downstream.

ISOLATION (guard-2484): every test drives a STUB slot via `slot_override` /
`--slot`. None of them invokes the real world/scripts/inbound-drain.sh, reads
WORLD_PATH, or touches any agent dir, team-state row or live spool. The one
test that exercises the WORLD_PATH branch monkeypatches `_world_path`.
"""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "_inbound_drain_run_under_test", str(SCRIPT_DIR / "inbound-drain-run.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _stub_slot(tmp_path: Path, body: str, name: str = "slot.sh") -> Path:
    """A fake hook slot. Never the real one (guard-2484)."""
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _emit(payload: dict) -> str:
    """Body for a stub slot that prints one JSON object and exits 0."""
    return "cat <<'JSON'\n" + json.dumps(payload) + "\nJSON\n"


# ---------------------------------------------------------------- absence != zero

def test_missing_slot_file_is_no_slot_not_a_drain_of_zero(tmp_path):
    """Pattern B requirement 3: an unfilled slot is a supported configuration."""
    res = MOD.run(slot_override=tmp_path / "does-not-exist.sh")
    assert res["status"] == "no-slot"
    assert res["drained"] == 0
    assert res["failed"] == []


def test_unresolvable_world_path_is_no_slot(monkeypatch):
    monkeypatch.setattr(MOD, "_world_path", lambda: None)
    res = MOD.run()
    assert res["status"] == "no-slot"
    assert res["drained"] == 0


def test_not_a_vessel_is_reported_as_such_and_not_as_ok_zero(tmp_path):
    """THE load-bearing distinction: most boxes are not vessels, and a silent 0
    there is indistinguishable from a vessel whose spool is genuinely empty."""
    slot = _stub_slot(
        tmp_path,
        _emit({"not_a_vessel": True, "reason": "no MIND_ENV_ID/ENV_ID"}),
    )
    res = MOD.run(slot_override=slot)
    assert res["status"] == "not-a-vessel"
    assert res["status"] != "ok"
    assert res["drained"] == 0
    assert "MIND_ENV_ID" in res["note"]


def test_empty_environments_is_ok_zero_and_distinct_from_not_a_vessel(tmp_path):
    """A vessel whose spool is genuinely empty must NOT read as not-a-vessel."""
    slot = _stub_slot(tmp_path, _emit({"root": "/mnt/x", "environments": []}))
    res = MOD.run(slot_override=slot)
    assert res["status"] == "ok"
    assert res["drained"] == 0
    assert res["environments_seen"] == 0


# ---------------------------------------------------------------- malfunction != clean

def test_zero_bytes_from_the_slot_is_unparseable_never_clean(tmp_path):
    """guard-1091 shape: exit 0 with unreadable output must not reach the
    battery as clean. Every branch of a real slot emits JSON."""
    slot = _stub_slot(tmp_path, "exit 0\n")
    res = MOD.run(slot_override=slot)
    assert res["status"] == "unparseable"
    assert res["failed"], "a malfunction must surface as a finding, not be swallowed"
    assert "ZERO bytes" in res["failed"][0]["reason"]


def test_non_json_output_is_unparseable(tmp_path):
    slot = _stub_slot(tmp_path, "echo not-json-at-all\n")
    res = MOD.run(slot_override=slot)
    assert res["status"] == "unparseable"
    assert res["failed"]
    assert "not JSON" in res["failed"][0]["reason"]


def test_slot_that_cannot_be_executed_is_unparseable(tmp_path, monkeypatch):
    slot = _stub_slot(tmp_path, _emit({"environments": []}))

    def _boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(MOD.subprocess, "run", _boom)
    res = MOD.run(slot_override=slot)
    assert res["status"] == "unparseable"
    assert "slot did not run" in res["failed"][0]["reason"]


# ---------------------------------------------------------------- the flattener

def test_nested_per_environment_counts_are_summed_to_top_level(tmp_path):
    """The reason a flattener exists in core at all: the precheck battery reads
    findings from TOP-LEVEL keys (`_findings_for` does payload.get(k)), while the
    domain drain nests counts under environments[]. A lane wired straight to the
    domain drain would find no top-level key and report clean forever."""
    slot = _stub_slot(tmp_path, _emit({"root": "/mnt/x", "environments": [
        {"environment": "env-a", "processed": 3, "claimed": 4, "rejected": 1,
         "quarantined": 2, "skipped_tmp": 5, "failed": 0},
        {"environment": "env-b", "processed": 7, "claimed": 1, "rejected": 0,
         "quarantined": 1, "skipped_tmp": 2, "failed": 0},
    ]}))
    res = MOD.run(slot_override=slot)
    assert res["status"] == "ok"
    assert res["environments_seen"] == 2
    assert res["drained"] == 10      # 3 + 7 processed
    assert res["claimed"] == 5
    assert res["rejected"] == 1
    assert res["quarantined"] == 3
    assert res["skipped_tmp"] == 7
    assert res["root"] == "/mnt/x"


def test_a_per_environment_failure_becomes_a_list_entry_not_a_count(tmp_path):
    """`failed` is the battery's UNIVERSAL finding key, so a per-env failure
    COUNT has to become a list entry or it is invisible to _findings_for."""
    slot = _stub_slot(tmp_path, _emit({"environments": [
        {"environment": "env-a", "processed": 1, "failed": 0},
        {"environment": "env-b", "processed": 0, "failed": 2},
    ]}))
    res = MOD.run(slot_override=slot)
    assert isinstance(res["failed"], list)
    assert len(res["failed"]) == 1
    assert res["failed"][0]["file"] == "env-b"
    assert "2 record(s) failed" in res["failed"][0]["reason"]


def test_missing_count_keys_do_not_raise(tmp_path):
    """A domain drain that omits a key must degrade to 0, not explode the lane."""
    slot = _stub_slot(tmp_path, _emit({"environments": [{"environment": "e"}]}))
    res = MOD.run(slot_override=slot)
    assert res["status"] == "ok"
    assert res["drained"] == 0
    assert res["failed"] == []


# ---------------------------------------------------------------- apply plumbing

def test_apply_is_forwarded_to_the_slot(tmp_path):
    """--apply must reach the slot argv, and be reflected in the result."""
    slot = _stub_slot(tmp_path, _emit({"environments": []}))
    off = MOD.run(slot_override=slot, apply=False)
    on = MOD.run(slot_override=slot, apply=True)
    assert off["apply"] is False
    assert on["apply"] is True


def test_apply_appends_the_flag_to_argv(tmp_path, monkeypatch):
    seen = {}

    class _P:
        stdout = '{"environments": []}'
        stderr = ""
        returncode = 0

    def _capture(argv, **k):
        seen["argv"] = list(argv)
        return _P()

    slot = _stub_slot(tmp_path, _emit({"environments": []}))
    monkeypatch.setattr(MOD.subprocess, "run", _capture)
    MOD.run(slot_override=slot, apply=True)
    assert "--apply" in seen["argv"]
    assert "--json" in seen["argv"]
    MOD.run(slot_override=slot, apply=False)
    assert "--apply" not in seen["argv"]


# ---------------------------------------------------------------- fail-open

@pytest.mark.parametrize("body,expect", [
    ("exit 0\n", "unparseable"),
    ("echo nope\n", "unparseable"),
    ("exit 3\n", "unparseable"),
])
def test_main_always_exits_zero(tmp_path, body, expect, capsys):
    """guard-614: an always-run precheck lane may never block the loop."""
    slot = _stub_slot(tmp_path, body)
    rc = MOD.main(["--slot", str(slot), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == expect


def test_main_exits_zero_on_a_healthy_drain(tmp_path, capsys):
    slot = _stub_slot(tmp_path, _emit({"environments": [
        {"environment": "e", "processed": 2, "failed": 0}]}))
    rc = MOD.main(["--slot", str(slot), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["drained"] == 2
